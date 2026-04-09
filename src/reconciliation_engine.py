"""Core payment reconciliation logic."""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    from src.ai_forensics import analyze_discrepancy
    from src.supabase_client import (
        get_supabase_client,
        run_with_retry,
    )
except ImportError:  # pragma: no cover - supports Lambda packaging styles.
    from ai_forensics import analyze_discrepancy
    from supabase_client import get_supabase_client, run_with_retry

LOGGER = logging.getLogger(__name__)


def process_transaction(event_body: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a single gateway transaction against an internal order record.

    The function is intentionally strict:
    - Duplicate provider transaction ids are treated as successful no-ops so SQS
      can safely delete the message.
    - Database and application failures are raised so Lambda fails the invocation,
      which instructs SQS to retry or route the message to the DLQ.
    """

    provider_txn_id = _require_string(event_body, "provider_txn_id")
    order_reference = _require_string(event_body, "order_reference")
    event_currency = _require_string(event_body, "currency").upper()
    captured_amount = _to_decimal(event_body.get("amount_captured"), field_name="amount_captured")
    metadata = _normalize_metadata(event_body.get("metadata"))

    LOGGER.info(
        "Starting reconciliation for provider_txn_id=%s order_reference=%s.",
        provider_txn_id,
        order_reference,
    )

    existing_transaction = _fetch_gateway_transaction(provider_txn_id)
    if existing_transaction:
        LOGGER.info(
            "Skipping duplicate provider_txn_id=%s because it has already been processed.",
            provider_txn_id,
        )
        return {
            "status": "duplicate",
            "provider_txn_id": provider_txn_id,
        }

    order_record = _fetch_order_by_reference(order_reference)
    internal_order_for_ai = order_record or {
        "id": None,
        "order_reference": order_reference,
        "amount": None,
        "currency": None,
        "status": "NOT_FOUND",
    }

    amounts_match = False
    currencies_match = False

    if order_record:
        order_amount = _to_decimal(order_record.get("amount"), field_name="orders.amount")
        order_currency = _require_string(order_record, "currency").upper()
        amounts_match = order_amount == captured_amount
        currencies_match = order_currency == event_currency

    is_reconciled = bool(order_record and amounts_match and currencies_match)

    if is_reconciled:
        # Update the order first and persist the idempotency marker second. If the
        # insert fails, a retry will repeat a safe status update and try the insert
        # again instead of permanently masking a partially-completed reconciliation.
        _update_order_status(order_record["id"], "RECONCILED")
        _insert_gateway_transaction(provider_txn_id, captured_amount, metadata)

        LOGGER.info(
            "Successfully reconciled provider_txn_id=%s to order_id=%s.",
            provider_txn_id,
            order_record["id"],
        )
        return {
            "status": "reconciled",
            "provider_txn_id": provider_txn_id,
            "order_id": order_record["id"],
        }

    LOGGER.warning(
        "Detected reconciliation discrepancy for provider_txn_id=%s order_reference=%s.",
        provider_txn_id,
        order_reference,
    )

    ai_report = analyze_discrepancy(internal_order_for_ai, event_body)

    if order_record:
        _update_order_status(order_record["id"], "FLAGGED")

    # On the flagged path we write the audit trail before the gateway transaction
    # idempotency record. Combined with the audit-log existence check below, this
    # prevents retries from losing the AI explanation if the final insert fails.
    _upsert_discrepancy_audit_log(
        order_id=order_record["id"] if order_record else None,
        provider_txn_id=provider_txn_id,
        ai_report=ai_report,
    )
    _insert_gateway_transaction(provider_txn_id, captured_amount, metadata)

    return {
        "status": "flagged",
        "provider_txn_id": provider_txn_id,
        "order_id": order_record["id"] if order_record else None,
        "ai_report": ai_report,
    }


def _fetch_gateway_transaction(provider_txn_id: str) -> dict[str, Any] | None:
    client = get_supabase_client()

    def operation() -> Any:
        return (
            client.table("gateway_transactions")
            .select("*")
            .eq("provider_txn_id", provider_txn_id)
            .limit(1)
            .execute()
        )

    response = run_with_retry(operation, "fetch_gateway_transaction")
    data = getattr(response, "data", None) or []
    return data[0] if data else None


def _fetch_order_by_reference(order_reference: str) -> dict[str, Any] | None:
    client = get_supabase_client()

    def operation() -> Any:
        return (
            client.table("orders")
            .select("*")
            .eq("order_reference", order_reference)
            .limit(1)
            .execute()
        )

    response = run_with_retry(operation, "fetch_order_by_reference")
    data = getattr(response, "data", None) or []
    return data[0] if data else None


def _update_order_status(order_id: str, status: str) -> None:
    client = get_supabase_client()

    def operation() -> Any:
        return client.table("orders").update({"status": status}).eq("id", order_id).execute()

    run_with_retry(operation, f"update_order_status:{order_id}:{status}")


def _insert_gateway_transaction(
    provider_txn_id: str,
    amount_captured: Decimal,
    metadata: dict[str, Any],
) -> None:
    client = get_supabase_client()
    payload = {
        "provider_txn_id": provider_txn_id,
        # PostgREST accepts strings for numerics, which avoids any float conversion.
        "amount_captured": str(amount_captured),
        "metadata": metadata,
    }

    def operation() -> Any:
        return client.table("gateway_transactions").insert(payload).execute()

    run_with_retry(operation, f"insert_gateway_transaction:{provider_txn_id}")


def _upsert_discrepancy_audit_log(
    order_id: str | None,
    provider_txn_id: str,
    ai_report: dict[str, Any],
) -> None:
    """Create at most one audit log per provider transaction id.

    The schema does not expose a dedicated external transaction id on audit_logs,
    so the idempotency token is embedded into action_taken. This avoids duplicate
    audit rows if a retry occurs before the gateway transaction insert succeeds.
    """

    client = get_supabase_client()
    action_taken = f"FLAGGED_DISCREPANCY:{provider_txn_id}"

    def fetch_existing() -> Any:
        return (
            client.table("audit_logs")
            .select("*")
            .eq("action_taken", action_taken)
            .limit(1)
            .execute()
        )

    existing_response = run_with_retry(fetch_existing, f"fetch_audit_log:{provider_txn_id}")
    existing_data = getattr(existing_response, "data", None) or []
    if existing_data:
        LOGGER.info(
            "Audit log already exists for provider_txn_id=%s; skipping duplicate audit insert.",
            provider_txn_id,
        )
        return

    reasoning = json.dumps(ai_report, ensure_ascii=True)
    payload = {
        # For orphaned gateway events there is no matching order row. This assumes
        # audit_logs.order_id is nullable so we can still preserve a forensic trail.
        "order_id": order_id,
        "action_taken": action_taken,
        "reasoning_by_ai": reasoning,
    }

    def insert_log() -> Any:
        return client.table("audit_logs").insert(payload).execute()

    run_with_retry(insert_log, f"insert_audit_log:{provider_txn_id}")


def _require_string(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"Missing required string field '{key}'.")
    return str(value)


def _to_decimal(value: Any, *, field_name: str) -> Decimal:
    if value is None:
        raise ValueError(f"Missing required decimal field '{field_name}'.")

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid decimal value for '{field_name}': {value}") from exc


def _normalize_metadata(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return metadata
    return {"raw_metadata": metadata}
