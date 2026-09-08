from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import ConfigurationError
from .state import RecorderLedgerReconciliationRequired, StateStoreError


class _BodyTooLarge(RuntimeError):
    pass


class _InvalidBodyLength(RuntimeError):
    pass


class AbuseControlMiddleware:
    """Bound request memory and apply durable backend-wide rate limits."""

    def __init__(self, app: ASGIApp, *, runtime_provider: Callable[[], Any]) -> None:
        self.app = app
        self.runtime_provider = runtime_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            active = self.runtime_provider()
        except RecorderLedgerReconciliationRequired:
            await self._error(
                scope,
                receive,
                send,
                503,
                "PinPoint recorder ownership requires operator reconciliation.",
            )
            return
        except (ConfigurationError, StateStoreError):
            await self._error(scope, receive, send, 503, "PinPoint is not configured.")
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", "")).upper()
        client_digest = _client_ip_digest(scope, active.settings.session_secret)
        global_digest = _private_digest(active.settings.session_secret, "global", "all")

        if path != "/healthz":
            if not await self._consume(
                scope,
                receive,
                send,
                active,
                bucket="global_request",
                subject_digest=global_digest,
                limit=active.settings.global_request_limit,
                window_seconds=active.settings.global_request_window_seconds,
            ):
                return

        if method == "POST" and path == "/v1/session/nonce":
            if not await self._consume(
                scope,
                receive,
                send,
                active,
                bucket="nonce_ip",
                subject_digest=client_digest,
                limit=active.settings.nonce_ip_limit,
                window_seconds=active.settings.nonce_ip_window_seconds,
            ):
                return
        elif method == "POST" and path == "/v1/session/apple":
            if not await self._consume(
                scope,
                receive,
                send,
                active,
                bucket="apple_signin_ip",
                subject_digest=client_digest,
                limit=active.settings.apple_signin_ip_limit,
                window_seconds=active.settings.apple_signin_ip_window_seconds,
            ):
                return

        try:
            declared_length = _declared_content_length(scope)
        except _InvalidBodyLength:
            await self._error(scope, receive, send, 400, "Invalid Content-Length header.")
            return

        maximum = active.settings.max_request_body_bytes
        if declared_length is not None and declared_length > maximum:
            await self._error(scope, receive, send, 413, "Request body is too large.")
            return

        try:
            body = await _read_limited_body(receive, maximum)
        except _BodyTooLarge:
            await self._error(scope, receive, send, 413, "Request body is too large.")
            return
        if body is None:
            return
        if declared_length is not None and declared_length != len(body):
            await self._error(scope, receive, send, 400, "Content-Length did not match the request body.")
            return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _consume(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        active: Any,
        *,
        bucket: str,
        subject_digest: str,
        limit: int,
        window_seconds: int,
    ) -> bool:
        try:
            decision = await asyncio.to_thread(
                active.state.consume_fixed_window,
                bucket=bucket,
                subject_digest=subject_digest,
                limit=limit,
                window_seconds=window_seconds,
            )
        except StateStoreError:
            await self._error(
                scope,
                receive,
                send,
                503,
                "PinPoint could not enforce its request policy.",
            )
            return False
        if decision.allowed:
            return True
        await self._error(
            scope,
            receive,
            send,
            429,
            "Too many requests. Try again shortly.",
            retry_after=decision.retry_after_seconds,
        )
        return False

    @staticmethod
    async def _error(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        headers = None if retry_after is None else {"Retry-After": str(retry_after)}
        response = JSONResponse({"detail": detail}, status_code=status_code, headers=headers)
        await response(scope, receive, send)


def _declared_content_length(scope: Scope) -> int | None:
    values = [
        value.strip()
        for name, value in scope.get("headers", [])
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if (
        len(values) != 1
        or not values[0]
        or len(values[0]) > 20
        or not values[0].isdigit()
    ):
        raise _InvalidBodyLength
    try:
        return int(values[0])
    except (ValueError, OverflowError) as exc:
        raise _InvalidBodyLength from exc


async def _read_limited_body(receive: Receive, maximum: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > maximum:
            raise _BodyTooLarge
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _client_ip_digest(scope: Scope, secret: str) -> str:
    client = scope.get("client")
    host = client[0] if isinstance(client, (tuple, list)) and client else None
    normalized = "unknown"
    if isinstance(host, str) and len(host) <= 255:
        try:
            address = ipaddress.ip_address(host)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            normalized = address.compressed
        except ValueError:
            pass
    return _private_digest(secret, "client-ip", normalized)


def _private_digest(secret: str, namespace: str, value: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"v1:{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
