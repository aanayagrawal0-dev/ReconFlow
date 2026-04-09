"""Webhook receiver Lambda that persists pending orders and enqueues events."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from supabase import create_client

LOGGER = logging.getLogger()
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO)
LOGGER.setLevel(logging.INFO)


sqs = boto3.client("sqs")
QUEUE_URL = os.environ.get("QUEUE_URL")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be configured.")
if not QUEUE_URL:
    raise ValueError("QUEUE_URL must be configured.")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        raw_body = event.get("body", "{}")
        data = json.loads(raw_body)

        order_reference = data.get("order_id") or data.get("order_reference")
        if not order_reference:
            raise ValueError("Incoming webhook is missing order_id/order_reference.")

        order_payload = {
            "order_reference": order_reference,
            "amount": data.get("amount") or data.get("amount_captured"),
            "currency": data.get("currency", "INR"),
            "status": "PENDING",
        }
        supabase.table("orders").upsert(
            order_payload,
            on_conflict="order_reference",
        ).execute()

        # Normalize the payload for the downstream reconciliation engine.
        queue_payload = dict(data)
        queue_payload.setdefault("order_reference", order_reference)
        if "amount_captured" not in queue_payload and "amount" in queue_payload:
            queue_payload["amount_captured"] = queue_payload["amount"]

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(queue_payload),
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "status": "received",
                    "order_id": order_reference,
                }
            ),
        }
    except Exception as exc:  # noqa: BLE001 - Lambda should return a clear failure body.
        LOGGER.exception("Fatal receiver error: %s", exc)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "System failure"}),
        }
