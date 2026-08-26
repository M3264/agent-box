"""Away-from-browser notifications over the Web Push standard.

The gap this closes: notifications were browser-only and tab-bound. ``announce()`` in
the frontend fires a ``Notification`` only while an Agent Hub tab is open and
backgrounded, so a job that parks on a question or an approval overnight — every tab
closed, which is the normal state — reached nobody. Web Push fixes that: a service
worker the browser keeps alive in the background receives an encrypted message the
server sends to the browser's own push service (FCM/Mozilla/Apple), and shows it on
the device even with the site shut.

Shape of the thing:

- **Fan-out is global.** Every browser that opted in is a row in ``push_subscriptions``
  and gets pinged on any blocked job. There is no user model to target, and "is
  anything waiting for me" is a question every operator of this box shares.
- **It fires only on a *rise* in blocking** — a new pending (non-auto) approval or a
  new question. That is the one server-side signal that means "a job needs you", the
  same thing ``counts.blocking`` counts. The hooks live in ``approvals.request`` and
  ``questions.request``.
- **Sending never blocks the engine.** ``notify()`` schedules a fire-and-forget
  fan-out and returns; the phase that raised the gate does not wait on a round-trip to
  Apple. Each individual send runs in a worker thread because ``pywebpush`` is blocking.

Secrets: the VAPID *private* key signs each push and is a secret like any other. It is
generated once and persisted to ``settings.vapid_file`` at mode 0600 (or pinned via an
env var), and it never enters the database, an API response, or a log line. Only the
*public* key is handed to the browser, as its ``applicationServerKey``.

``pywebpush``/``py_vapid`` are imported lazily inside ``_send_one`` — the one place
that talks to the network. Tests monkeypatch the sender, so those packages never load
during the test run, which keeps their DeprecationWarnings clear of pytest's
``error::DeprecationWarning`` filter.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.config import settings
from app.db import db
from app.logging_setup import get_logger

log = get_logger("agent_hub.push")

#: The notification's collapse tag. The in-tab ``announce()`` uses the same string, so
#: a push and an in-tab toast for one event coalesce into a single notification rather
#: than double-buzzing a device that has a tab open too.
TAG = "agent-hub-attention"

#: In-flight fan-out tasks, held so a fire-and-forget task is not garbage-collected
#: mid-send and so shutdown can drain them.
_tasks: set[asyncio.Task[Any]] = set()

#: The resolved keypair. ``_public_key`` is base64url of the uncompressed EC point (what
#: the browser wants as ``applicationServerKey``); ``_private_pem`` is the PKCS8 PEM the
#: sender signs with. Both stay None until ``setup()`` runs — which is what
#: ``configured()`` reports.
_public_key: str | None = None
_private_pem: str | None = None


class _Gone(Exception):
    """The push service says a subscription is dead (404/410) — drop its row."""


def configured() -> bool:
    """True once a VAPID keypair is resolved and pushes can actually be sent."""
    return bool(_public_key and _private_pem)


def public_key() -> str | None:
    """The public ``applicationServerKey`` for the frontend. Never the private key."""
    return _public_key


async def setup(vapid_file: Path | None = None, *, enabled: bool | None = None) -> None:
    """Resolve the VAPID keypair: env override, else keyfile, else generate + persist.

    Idempotent and safe to call on every startup. Called from the app lifespan after
    migrations; tests call it with an explicit ``vapid_file`` and ``enabled=True`` to
    opt in, since the suite pins ``AGENT_HUB_PUSH=0`` so 60-odd unrelated tests never
    spin up a fan-out.
    """
    global _public_key, _private_pem
    if enabled is None:
        enabled = settings.push_enabled
    if not enabled:
        _public_key = _private_pem = None
        log.info("push disabled")
        return

    if settings.vapid_public_key and settings.vapid_private_key:
        _public_key = settings.vapid_public_key
        _private_pem = settings.vapid_private_key
        log.info("push configured from environment")
        return

    path = vapid_file or settings.vapid_file
    loaded = _load(path)
    if loaded is None:
        loaded = _generate()
        _persist(path, loaded)
        log.info("push keypair generated", extra={"file": str(path)})
    else:
        log.info("push keypair loaded", extra={"file": str(path)})
    _public_key, _private_pem = loaded


def reset() -> None:
    """Drop the in-memory keypair. For the test that asserts the unconfigured path."""
    global _public_key, _private_pem
    _public_key = _private_pem = None


# --------------------------------------------------------------------- key material


def _generate() -> tuple[str, str]:
    """A fresh EC P-256 keypair as (public base64url point, private PKCS8 PEM).

    ``cryptography`` is a stable, always-installed dependency, so importing it on the
    always-run startup path is safe — unlike ``pywebpush``, whose import is deferred.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    point = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public = base64.urlsafe_b64encode(point).rstrip(b"=").decode()
    return public, pem


def _persist(path: Path, keys: tuple[str, str]) -> None:
    """Write the keypair as ``{public, private}`` JSON at mode 0600.

    Created 0600 from the start rather than written-then-chmod'd, so the private key is
    never briefly world-readable; the explicit chmod covers the case of overwriting a
    pre-existing file whose mode we did not choose.
    """
    public, private = keys
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"public": public, "private": private}, handle)
    os.chmod(path, 0o600)


def _load(path: Path) -> tuple[str, str] | None:
    """The persisted keypair, or None if the file is absent or unreadable."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    public = raw.get("public")
    private = raw.get("private")
    if isinstance(public, str) and public and isinstance(private, str) and private:
        return public, private
    return None


# ------------------------------------------------------------------------ fan-out


def notify(job_id: str, *, kind: str, agent: str | None, summary: str) -> None:
    """Schedule a fan-out for a job that just became blocked, then return at once.

    A clean no-op when push is unconfigured or off, so the gate paths can call it
    unconditionally. Fire-and-forget on purpose: the fan-out is a round-trip per
    subscription to a browser push service and must never sit in front of the engine
    advancing a phase.
    """
    if not configured():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - notify is only ever called on the loop
        return
    task = loop.create_task(_fanout(job_id, kind=kind, agent=agent, summary=summary))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain(timeout: float = 5.0) -> None:
    """Await outstanding fan-outs so shutdown does not drop a send mid-flight."""
    if _tasks:
        await asyncio.wait(set(_tasks), timeout=timeout)


async def send_test() -> int:
    """Send a test push to every subscription and return how many were attempted.

    Awaited rather than scheduled, so the HTTP response can report that the switch
    actually reached the wire. Reuses ``_send`` so a dead subscription is pruned here
    too — the test button doubles as a cleanup.
    """
    if not configured():
        return 0
    subs = await db.fetch_all(
        "select id,endpoint,p256dh,auth from push_subscriptions order by created_at"
    )
    payload = {
        "title": "Agent Hub",
        "body": "Notifications are on — this is a test.",
        "hash": "#/settings",
        "tag": TAG,
    }
    for sub in subs:
        await _send(dict(sub), payload)
    return len(subs)


async def subscriber_count() -> int:
    return int(await db.fetch_value("select count(*) from push_subscriptions", default=0))


async def _fanout(job_id: str, *, kind: str, agent: str | None, summary: str) -> None:
    try:
        subs = await db.fetch_all(
            "select id,endpoint,p256dh,auth from push_subscriptions order by created_at"
        )
        if not subs:
            return
        task = await db.fetch_value("select task from jobs where id=?", (job_id,), default="")
        payload = _compose(job_id, kind=kind, agent=agent, summary=summary, task=str(task or ""))
        for sub in subs:
            await _send(dict(sub), payload)
    except Exception:  # noqa: BLE001 - a best-effort background task must never crash the loop
        log.warning("push fan-out failed", extra={"job_id": job_id}, exc_info=True)


async def _send(sub: dict[str, Any], payload: dict[str, Any]) -> None:
    """Send one push, and reconcile the row with what the push service said.

    A dead endpoint (404/410) is deleted outright; a transient error bumps ``failures``
    and keeps the row, because one flaky send is not grounds to unsubscribe a browser.
    """
    try:
        await asyncio.to_thread(_send_one, sub, payload)
    except _Gone:
        await db.execute("delete from push_subscriptions where id=?", (sub["id"],))
        log.info("push subscription pruned", extra={"host": _host(sub["endpoint"])})
    except Exception:  # noqa: BLE001 - one bad send must not abort the fan-out
        await db.execute(
            "update push_subscriptions set failures=failures+1 where id=?", (sub["id"],)
        )
        log.warning("push send failed", extra={"host": _host(sub["endpoint"])}, exc_info=True)
    else:
        await db.execute(
            "update push_subscriptions set last_ok=unixepoch('subsec'),failures=0 where id=?",
            (sub["id"],),
        )


def _send_one(sub: dict[str, Any], payload: dict[str, Any]) -> None:
    """The one blocking, network-touching call. Runs in a worker thread.

    Lazy-imports ``pywebpush``/``py_vapid`` so they never load during the test run
    (which monkeypatches this function), keeping their DeprecationWarnings out of
    pytest's ``error`` filter. Raises ``_Gone`` on a 404/410 so ``_send`` can prune.
    """
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=_vapid_key(),
            vapid_claims={"sub": settings.vapid_subject},
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            raise _Gone from exc
        raise


def _vapid_key() -> Any:
    """A ``py_vapid`` signer built from the private PEM. Rebuilt per send (sends are
    rare); the PEM never leaves this process."""
    from py_vapid import Vapid02

    return Vapid02.from_pem((_private_pem or "").encode())


# ------------------------------------------------------------------------ payload


def _compose(
    job_id: str, *, kind: str, agent: str | None, summary: str, task: str
) -> dict[str, str]:
    """Title/body mirroring the in-tab ``headline()``: name the event and the task, and
    carry the hash that opens that job when the notification is clicked."""
    who = agent or "an agent"
    summary = _clip(summary, 100)
    task = _clip(task, 80)
    if kind == "question":
        title = f"{who} is asking"
        body = summary
    else:
        title = "Approval needed"
        body = f"{who}: {summary}"
    if task:
        body = f"{body} — {task}"
    return {"title": title, "body": body, "hash": f"#/jobs/{job_id}", "tag": TAG}


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _host(endpoint: str) -> str:
    """The push service's host, for a log line that identifies a subscription without
    logging the full endpoint (which is effectively a bearer token for that browser)."""
    try:
        return urlsplit(endpoint).netloc or "?"
    except ValueError:
        return "?"
