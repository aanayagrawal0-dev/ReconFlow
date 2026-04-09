"""Reusable Supabase client and retry helpers for Lambda-safe database access."""

from __future__ import annotations

import logging
import os
import time
from threading import Lock
from typing import Any, Callable, TypeVar

from supabase import Client, create_client

LOGGER = logging.getLogger(__name__)

_CLIENT: Client | None = None
_CLIENT_LOCK = Lock()
_T = TypeVar("_T")


class SupabaseOperationError(RuntimeError):
    """Raised when a Supabase call fails after retrying transient failures."""


def get_supabase_client() -> Client:
    """Return a lazily initialized, process-wide Supabase client.

    AWS Lambda may reuse the same execution environment for multiple invocations.
    Reusing the client keeps cold-start work low and avoids creating a new HTTP
    stack for every SQS record batch.
    """

    global _CLIENT

    if _CLIENT is not None:
        return _CLIENT

    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")

        if not supabase_url or not supabase_key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_KEY must be configured in the environment."
            )

        _CLIENT = create_client(supabase_url, supabase_key)
        LOGGER.info("Initialized Supabase client.")
        return _CLIENT


def run_with_retry(
    operation: Callable[[], _T],
    operation_name: str,
    *,
    max_attempts: int = 3,
    initial_delay_seconds: float = 0.5,
) -> _T:
    """Execute a Supabase operation with retries for transient failures.

    The Supabase Python client ultimately performs HTTP requests to PostgREST.
    Temporary timeouts, connection resets, or upstream 5xx errors are usually
    worth retrying. Persistent failures are surfaced so Lambda can fail the batch
    and allow SQS redelivery or DLQ routing.
    """

    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - third-party client raises broad exceptions.
            last_error = exc
            transient = _is_transient_error(exc)

            if not transient or attempt == max_attempts:
                LOGGER.exception(
                    "Supabase operation '%s' failed on attempt %s/%s.",
                    operation_name,
                    attempt,
                    max_attempts,
                )
                raise SupabaseOperationError(
                    f"Supabase operation '{operation_name}' failed."
                ) from exc

            sleep_seconds = initial_delay_seconds * (2 ** (attempt - 1))
            LOGGER.warning(
                "Transient Supabase failure during '%s' on attempt %s/%s; retrying in %.2fs. Error: %s",
                operation_name,
                attempt,
                max_attempts,
                sleep_seconds,
                exc,
            )
            time.sleep(sleep_seconds)

    raise SupabaseOperationError(
        f"Supabase operation '{operation_name}' failed."
    ) from last_error


def _is_transient_error(error: Exception) -> bool:
    """Heuristic detection for retryable network and upstream availability errors."""

    message = str(error).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "connection refused",
        "temporar",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "too many requests",
        "502",
        "503",
        "504",
        "429",
    )
    type_name = error.__class__.__name__.lower()

    return "timeout" in type_name or any(marker in message for marker in transient_markers)
