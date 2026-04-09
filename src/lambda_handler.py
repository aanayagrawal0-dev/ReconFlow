"""AWS Lambda entry point for SQS-driven payment reconciliation."""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from src.reconciliation_engine import process_transaction
except ImportError:  # pragma: no cover - supports Lambda packaging styles.
    from reconciliation_engine import process_transaction

LOGGER = logging.getLogger()
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO)
LOGGER.setLevel(logging.INFO)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process each SQS record and intentionally fail the batch on hard errors.

    Important SQS/Lambda contract:
    - If a record processing attempt raises, the Lambda invocation must fail so
      AWS can retry delivery or route to the Dead Letter Queue.
    - We therefore do not swallow database, parsing, or application exceptions.
    """

    records = event.get("Records", [])
    LOGGER.info("Received %s SQS records for reconciliation.", len(records))

    for record in records:
        body = record.get("body")
        if body is None:
            raise ValueError("SQS record is missing body.")

        try:
            event_body = json.loads(body)
        except json.JSONDecodeError as exc:
            LOGGER.exception("SQS record body is not valid JSON.")
            raise ValueError("SQS record body is not valid JSON.") from exc

        LOGGER.info(
            "Processing SQS messageId=%s provider_txn_id=%s.",
            record.get("messageId"),
            event_body.get("provider_txn_id"),
        )
        process_transaction(event_body)

    return {
        "statusCode": 200,
        "body": json.dumps({"processed_records": len(records)}),
    }
