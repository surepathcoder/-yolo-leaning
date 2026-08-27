"""Aggregate router for API v1."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import alerts, auth, cameras, events, inventory, stores, system, zones

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(stores.router)
api_router.include_router(cameras.router)
api_router.include_router(zones.router)
api_router.include_router(events.router)
api_router.include_router(inventory.router)
api_router.include_router(alerts.router)
api_router.include_router(system.router)
