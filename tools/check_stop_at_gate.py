"""Live confirmation that stopping a job at a command gate actually stops it.

The bug this checks for was invisible to the unit suite until it happened live: the
gate's own status restore ran during cancellation and landed on top of the 'stopped'
that the stop endpoint had already written, leaving a job that read 'running' with no
task behind it — un-stoppable and un-restartable, since the startup sweep skips
terminal jobs and this one no longer looked terminal.

Creates a job whose first command trips the sudo guardrail, waits for the gate, stops
the job immediately, and then keeps watching: the failure mode is not visible at the
moment of stopping, only a beat later when the restore lands.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8090"
PROVIDER = sys.argv[1] if len(sys.argv) > 1 else "agentrouter"
TASK = "Install the ripgrep package system-wide with apt and confirm with rg --version."


def call(method: str, path: str, payload: dict | None = None, timeout: float = 20.0):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def main() -> int:
    failures: list[str] = []

    job = call("POST", "/api/jobs", {"task": TASK, "provider_id": PROVIDER, "sandbox": "sandboxed"})
    job_id = job["id"]
    print(f"job {job_id} created on {PROVIDER}")

    # Poll hard, so the stop lands before anyone watching the UI can approve the gate.
    deadline = time.monotonic() + 180
    gate = None
    while time.monotonic() < deadline:
        pending = [
            a
            for a in (call("GET", f"/api/jobs/{job_id}") or {}).get("approvals", [])
            if a["kind"] == "tool" and a["status"] == "pending"
        ]
        if pending:
            gate = pending[0]
            break
        time.sleep(0.2)
    if gate is None:
        raise SystemExit("FAILED: no command gate appeared within 180s")
    print(f"  gate up: {gate['id']} — {gate['detail']}")

    call("POST", f"/api/jobs/{job_id}/stop")
    print("  stop requested")

    # The clobbering write arrives after the stop returns, so a single read proves
    # nothing. Watch for a while and fail on the first non-terminal reading.
    seen: list[str] = []
    for _ in range(30):
        status = call("GET", f"/api/jobs/{job_id}")["status"]
        if not seen or seen[-1] != status:
            seen.append(status)
        if status != "stopped":
            failures.append(f"the job read {status!r} after being stopped (saw {seen})")
            break
        time.sleep(0.5)
    else:
        print(f"  stayed stopped for 15s (statuses seen: {seen})")

    snapshot = call("GET", f"/api/jobs/{job_id}")
    # The API hands back decoded payloads; the DB stores them as text. Accept either so
    # this reads the same whether it is pointed at the endpoint or the table.
    events = [
        (e["payload"] if isinstance(e["payload"], dict) else json.loads(e["payload"]))["status"]
        for e in snapshot["events"]
        if e["kind"] == "status"
    ]
    if events[-1] != "stopped":
        failures.append(f"the last status streamed was {events[-1]!r}, not 'stopped': {events}")
    else:
        print(f"  last status event: stopped ({len(events)} in total)")

    rows = call("GET", f"/api/jobs/{job_id}/tool-calls") or []
    unresolved = [r for r in rows if r["status"] in {"pending", "running"}]
    if unresolved:
        failures.append(f"{len(unresolved)} command row(s) still in flight on a stopped job")
    else:
        print(f"  all {len(rows)} command row(s) resolved: {[r['status'] for r in rows]}")

    gates = [a for a in snapshot["approvals"] if a["status"] == "pending"]
    if gates:
        failures.append(f"{len(gates)} gate(s) left in the inbox for a job that will never run")
    else:
        print("  no gate left pending")

    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for failure in failures:
            print(" -", failure)
        return 1
    print("OK: the stop held, nothing was left in flight, and the stream closed on 'stopped'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
