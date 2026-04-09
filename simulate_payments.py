import os
from decimal import Decimal

import requests


# Set this before running, for example:
# export API_URL="https://your-api-id.execute-api.region.amazonaws.com/webhook"
API_URL = os.getenv(
    "API_URL",
    "https://your-api-id.execute-api.region.amazonaws.com/webhook",
)


def send_payment(order_ref, amount, txn_id, currency="INR"):
    payload = {
        "order_reference": order_ref,
        "amount_captured": str(Decimal(amount)),
        "provider_txn_id": txn_id,
        "currency": currency,
    }
    response = requests.post(API_URL, json=payload, timeout=15)
    print(f"Sent {order_ref} ({txn_id}): Status {response.status_code}")
    print(response.text)


# 1. THE PERFECT MATCH (ORD-001 is 500.00)
send_payment("ORD-001", "500.00", "TXN_GOOD_001")

# 2. THE DISCREPANCY (ORD-002 is 1200.00, sending 1150.00)
# This should trigger OpenAI forensics later.
send_payment("ORD-002", "1150.00", "TXN_MISMATCH_002")

# 3. THE IDEMPOTENCY TEST (Resending the same ID)
# The engine should ignore this second call.
send_payment("ORD-001", "500.00", "TXN_GOOD_001")
