"""Process entry point: serve the app on the configured host and port."""

import uvicorn

from app.core.config import Settings


def run() -> None:
    settings = Settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )
