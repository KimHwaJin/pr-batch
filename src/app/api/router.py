"""Versioned API router."""

from fastapi import APIRouter

from app.api.v1 import batches, notebooks, runs

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(notebooks.router)
api_router.include_router(batches.router)
api_router.include_router(runs.router)
