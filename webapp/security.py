"""Защита локального HTTP API от CSRF и небезопасных браузерных запросов."""
from __future__ import annotations

import hmac
import ipaddress
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_HEADER = "X-Dorama-CSRF"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TOKEN = secrets.token_urlsafe(32)


class RequestBodyLimitMiddleware:
    """Остановить oversized upload до multipart-парсера и записи temp-файла."""

    def __init__(self, app: ASGIApp, max_upload_bytes: int) -> None:
        self.app = app
        self.max_upload_bytes = max_upload_bytes
        self.max_request_bytes = max_upload_bytes + 1024 * 1024  # multipart headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/api/uploads":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_length = headers.get(b"content-length", b"")
        try:
            content_length = int(raw_length) if raw_length else None
        except ValueError:
            await self._reject(send, 400, "Некорректный Content-Length")
            return
        if content_length is not None and content_length > self.max_request_bytes:
            await self._reject(send, 413, "Загружаемый файл превышает допустимый размер")
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_request_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send, 413, "Загружаемый файл превышает допустимый размер")

    @staticmethod
    async def _reject(send: Send, status: int, detail: str) -> None:
        body = ('{"detail":"' + detail + '"}').encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


def session_payload() -> dict[str, str]:
    return {"csrf_token": _TOKEN}


def _is_loopback(host: str | None) -> bool:
    if host in {"testclient", "localhost"}:
        return True
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


async def local_api_guard(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Разрешать API только локальному клиенту и требовать CSRF-токен для мутаций."""
    if request.url.path.startswith("/api/"):
        client_host = request.client.host if request.client else None
        if not _is_loopback(client_host):
            return JSONResponse(
                {"detail": "API доступен только с этого компьютера"}, status_code=403
            )
        if request.method in UNSAFE_METHODS:
            supplied = request.headers.get(CSRF_HEADER, "")
            if not supplied or not hmac.compare_digest(supplied, _TOKEN):
                return JSONResponse(
                    {"detail": "Недействительный CSRF-токен"}, status_code=403
                )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; media-src 'self' blob:"
    )
    if request.url.path == "/api/session":
        response.headers["Cache-Control"] = "no-store"
    return response
