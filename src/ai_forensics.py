"""Gemini REST integration for discrepancy analysis."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a Financial Integrity Agent. Analyze the internal order and gateway "
    "event. Detect discrepancies in taxes, currency, or fraud. Return ONLY a raw "
    "JSON object with keys: `discrepancy_type`, `explanation`, and "
    "`suggested_action`."
)

DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"


def analyze_discrepancy(internal_order: dict[str, Any], gateway_event: dict[str, Any]) -> dict[str, Any]:
    """Ask Gemini to explain why a payment event does not reconcile cleanly.

    The REST API is used intentionally to keep the deployment artifact small.
    The function validates and normalizes the model response so the reconciliation
    engine always receives a predictable dictionary shape.
    """

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY must be configured in the environment.")

    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )

    user_payload = {
        "internal_order": internal_order,
        "gateway_event": gateway_event,
    }

    request_payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": json.dumps(user_payload, default=str),
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = requests.post(url, json=request_payload, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.exception("Gemini REST request failed.")
        raise RuntimeError("Failed to call Gemini REST API.") from exc

    response_json = response.json()
    model_text = _extract_response_text(response_json)
    parsed = _parse_json_object(model_text)

    # Normalize the shape so downstream writes to audit_logs are predictable.
    return {
        "discrepancy_type": str(parsed.get("discrepancy_type", "UNKNOWN")),
        "explanation": str(parsed.get("explanation", "No explanation returned by Gemini.")),
        "suggested_action": str(parsed.get("suggested_action", "MANUAL_REVIEW")),
    }


def _extract_response_text(response_json: dict[str, Any]) -> str:
    """Extract the primary text block from Gemini's REST response."""

    candidates = response_json.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini response did not include candidates: {response_json}")

    first_candidate = candidates[0]
    content = first_candidate.get("content") or {}
    parts = content.get("parts") or []
    if not parts:
        raise RuntimeError(f"Gemini response did not include content parts: {response_json}")

    text = parts[0].get("text")
    if not text:
        raise RuntimeError(f"Gemini response did not include text content: {response_json}")

    return text.strip()


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    """Parse a JSON object even if the model wraps it in markdown fences.

    Models occasionally surround JSON with ```json fences despite being told not
    to. This helper strips common wrappers and then locates the outermost object.
    """

    cleaned = raw_text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"Gemini response was not valid JSON: {raw_text}")

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini response contained malformed JSON: {raw_text}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Gemini response JSON was not an object: {raw_text}")

    return parsed
