"""OpenAI-powered discrepancy analysis for payment reconciliation."""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import OpenAI

LOGGER = logging.getLogger(__name__)

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = (
    "You are a financial audit assistant. Analyze payment discrepancies with "
    "currency consistency treated as a high-priority alert. Return ONLY a raw "
    "JSON object with keys: discrepancy_type, explanation, suggested_action."
)

_CLIENT: OpenAI | None = None


def analyze_discrepancy(
    internal_order: dict[str, Any],
    gateway_event: dict[str, Any],
) -> dict[str, Any]:
    """Generate a short forensic assessment for a mismatched payment event."""

    client = _get_openai_client()
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)

    order_amount = _safe_decimal(internal_order.get("amount"))
    gateway_amount = _safe_decimal(gateway_event.get("amount_captured"))
    discrepancy = None
    if order_amount is not None and gateway_amount is not None:
        discrepancy = abs(order_amount - gateway_amount)

    prompt = (
        "Analyze this payment discrepancy.\n"
        f"- Internal Order Amount: {order_amount if order_amount is not None else internal_order.get('amount')} "
        f"{internal_order.get('currency')}\n"
        f"- Gateway Transaction Amount: {gateway_amount if gateway_amount is not None else gateway_event.get('amount_captured')} "
        f"{gateway_event.get('currency')}\n"
        f"- Order Reference: {internal_order.get('order_reference')}\n"
        f"- Provider Transaction ID: {gateway_event.get('provider_txn_id')}\n"
        f"- Discrepancy: {discrepancy if discrepancy is not None else 'UNKNOWN'}\n\n"
        "Check currency consistency first. If the currencies differ, treat that as a "
        "high-priority alert. Otherwise explain whether the discrepancy could be caused "
        "by tax, gateway fees, rounding, orphaned payment, or fraud review. Keep it "
        "professional and concise."
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=150,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 - SDK raises multiple exception types.
        LOGGER.exception("OpenAI API error while generating forensic report.")
        return {
            "discrepancy_type": "AI_ERROR",
            "explanation": "Audit Error: Could not generate AI report. Manual review required.",
            "suggested_action": "MANUAL_REVIEW",
        }

    content = response.choices[0].message.content or ""
    parsed = _parse_json_object(content)

    return {
        "discrepancy_type": str(parsed.get("discrepancy_type", "UNKNOWN")),
        "explanation": str(
            parsed.get(
                "explanation",
                "Mismatch detected. Manual review required.",
            )
        ),
        "suggested_action": str(parsed.get("suggested_action", "MANUAL_REVIEW")),
    }


def _get_openai_client() -> OpenAI:
    global _CLIENT

    if _CLIENT is not None:
        return _CLIENT

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY must be configured in the environment.")

    _CLIENT = OpenAI(api_key=api_key)
    return _CLIENT


def _safe_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            LOGGER.error("OpenAI response was not valid JSON: %s", raw_text)
            return {}
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            LOGGER.error("OpenAI response contained malformed JSON: %s", raw_text)
            return {}

    if not isinstance(parsed, dict):
        LOGGER.error("OpenAI response JSON was not an object: %s", raw_text)
        return {}

    return parsed
