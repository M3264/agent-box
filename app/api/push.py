"""HTTP surface for the Web Push opt-in: the public key, and subscribe/unsubscribe/test.

These sit behind the same nginx basic-auth as everything else, which is the intended
trust boundary — "anyone visiting the site" means anyone who got through that. No
secret crosses this surface: the public VAPID key is meant to be public, and a
subscription carries only the browser's own public keys.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request

from app import push
from app.deps import db
from app.logging_setup import get_logger
from app.models import PushSubscribe, PushUnsubscribe

log = get_logger("agent_hub.api.push")

router = APIRouter(prefix="/api", tags=["push"])


@router.get("/push")
async def push_info() -> dict[str, Any]:
    """What the frontend needs to subscribe, plus enough to show current state.

    ``key`` is the public ``applicationServerKey`` and nothing else — the private key
    is never served. ``configured`` being false is the frontend's cue to explain that
    push is switched off server-side rather than to offer a switch that cannot work.
    """
    return {
        "configured": push.configured(),
        "key": push.public_key(),
        "subscribers": await push.subscriber_count(),
    }


@router.post("/push/subscribe", status_code=201)
async def subscribe(sub: PushSubscribe, request: Request) -> dict[str, Any]:
    """Store a browser's subscription, upserting on endpoint.

    A browser silently rotates its subscription and re-posts; the unique endpoint plus
    this upsert is what keeps that one row rather than accumulating dead duplicates that
    would each fire a notification.
    """
    await db.execute(
        "insert into push_subscriptions(id,endpoint,p256dh,auth,ua,created_at)"
        " values(?,?,?,?,?,unixepoch('subsec'))"
        " on conflict(endpoint) do update set"
        " p256dh=excluded.p256dh,auth=excluded.auth,ua=excluded.ua",
        (
            uuid.uuid4().hex[:12],
            sub.endpoint,
            sub.keys.p256dh,
            sub.keys.auth,
            request.headers.get("user-agent"),
        ),
    )
    return {"ok": True, "subscribers": await push.subscriber_count()}


@router.post("/push/unsubscribe")
async def unsubscribe(sub: PushUnsubscribe) -> dict[str, Any]:
    """Drop a browser's subscription. Idempotent — unticking twice is not an error."""
    await db.execute("delete from push_subscriptions where endpoint=?", (sub.endpoint,))
    return {"ok": True, "subscribers": await push.subscriber_count()}


@router.post("/push/test")
async def test() -> dict[str, Any]:
    """Send a test push to every subscription, so the switch can prove itself without
    waiting for a real job to block."""
    sent = await push.send_test()
    return {"ok": True, "sent": sent}
