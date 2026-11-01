"""Request size limit and write-request audit log."""

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import detail, error_response

logger = logging.getLogger(__name__)


class RequestSizeLimit:
    """Bound chunked and multipart bodies before the form parser runs."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                response = error_response(
                    413,
                    "REQUEST_TOO_LARGE",
                    "Body too large.",
                    [detail(["body"], "too_large", limit=self.max_bytes)],
                )
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def buffered_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body)}

        await self.app(scope, buffered_receive, send)


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuditWrites:
    """One log line per authenticated write request: who, as which
    tenant and user, what, and the response status.

    The values are in the message itself (key=value), as the internal
    logger prints the message only. The caller and identity
    dependencies fill them in (app.core.security.audit); requests
    refused before a key matched are not logged. Never the body or key.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http" or scope["method"] not in WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        status = 500

        async def recording_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, recording_send)
        finally:
            client = state.get("audit_client")
            if client is not None:
                logger.info(
                    "Caller write request client=%s tenant=%s user=%s "
                    "method=%s path=%s status=%s",
                    client,
                    state.get("audit_tenant", "-"),
                    state.get("audit_user", "-"),
                    scope["method"],
                    scope["path"],
                    status,
                )
