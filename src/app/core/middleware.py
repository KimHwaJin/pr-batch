"""Bounded request ingestion before body parsers run."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import detail, error_response


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
