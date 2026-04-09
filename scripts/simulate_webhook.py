"""Generate mock payment webhook payloads for local testing."""

from __future__ import annotations

import argparse
import json
import uuid
from decimal import Decimal


def build_matching_payload() -> dict[str, object]:
    return {
        "provider_txn_id": f"txn_{uuid.uuid4().hex}",
        "order_reference": "ORDER-1001",
        "amount_captured": str(Decimal("149.99")),
        "currency": "USD",
        "metadata": {
            "provider": "stripe",
            "tax_amount": "0.00",
            "card_last4": "4242",
            "scenario": "matching",
        },
    }


def build_mismatched_payload() -> dict[str, object]:
    return {
        "provider_txn_id": f"txn_{uuid.uuid4().hex}",
        "order_reference": "ORDER-1002",
        "amount_captured": str(Decimal("159.99")),
        "currency": "EUR",
        "metadata": {
            "provider": "stripe",
            "tax_amount": "10.00",
            "card_last4": "1881",
            "scenario": "mismatched",
            "notes": "Amount and currency intentionally differ from internal order.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate mock webhook JSON for local reconciliation testing."
    )
    parser.add_argument(
        "--scenario",
        choices=("matching", "mismatched", "both"),
        default="both",
        help="Choose which payload shape to emit.",
    )
    args = parser.parse_args()

    payloads = []
    if args.scenario in {"matching", "both"}:
        payloads.append(build_matching_payload())
    if args.scenario in {"mismatched", "both"}:
        payloads.append(build_mismatched_payload())

    for payload in payloads:
        print(json.dumps(payload))


if __name__ == "__main__":
    main()
