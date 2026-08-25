"""The one durability claim the database cannot make on its own.

Every other phase output commits with its terminal status, so a restart resumes
instead of replaying. A shell command breaks that symmetry: its effects are in the
filesystem, and no transaction can roll them back. The handling is therefore
honest rather than perfect — the row is written ``running`` *before* the process
starts, startup flips it to ``interrupted``, and the re-run's prompt names what
was in flight.

That is three moving parts across a process boundary, which is exactly the kind of
thing that works in a unit test and not on a real restart. So this drives the live
service: it restarts the unit once up front so the code under test is the code on
disk, starts a job that runs a long ``sleep``, waits until the command is genuinely
in flight, restarts the unit underneath it, and then asserts the four things that
matter.

There is more than one way for a command to be interrupted, and they need different
handling. A hard crash leaves a ``running`` row for startup to sweep. A graceful
``systemctl restart`` does not: systemd SIGTERMs the whole cgroup, so the child dies
while the service is still alive to see it — which for a long time it recorded as an
ordinary non-zero exit, letting the phase complete on a command whose fate nobody
knew. This check is what caught that, and the reason it drives the real unit rather
than cancelling a task in-process.

    .venv/bin/python -m tools.check_tool_restart

Needs the service running on 127.0.0.1:8090 and permission to restart the unit
(override with ``AGENT_HUB_RESTART_CMD``). The provider is served in-process, so it
survives the restart and can record what the re-run was actually told — no live
provider call, no network, no key.

Exit 0 means all four claims held; exit 1 names the ones that did not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BASE = "http://127.0.0.1:8090"
PROVIDER_ID = "restart-check"
PORT = int(os.environ.get("AGENT_HUB_CHECK_PORT", "8791"))
RESTART_CMD = os.environ.get("AGENT_HUB_RESTART_CMD", "sudo -n systemctl restart agent-hub")

PLAN_MARKER = "Reply with JSON only"
#: Long enough that the restart lands mid-command, distinctive enough to pgrep for.
SLEEP_SECONDS = 3607
COMMAND = f"sleep {SLEEP_SECONDS}"

PLAN = {
    "phases": [
        {
            "name": "Wait on a long command",
            "owner": "coder",
            "acceptance": "the command was run and its outcome accounted for",
            "requires_approval": False,
        }
    ],
    "notes": "one phase, so the restart lands in a known place",
}

#: Shared between the HTTP thread and the main thread; the lock is what makes the
#: recorded prompts safe to read while a request may be in flight.
state: dict[str, Any] = {"prompts": [], "sleep_issued": False, "tool_turns": 0}
lock = threading.Lock()


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            request = json.loads(self.rfile.read(length) or b"{}")
            prompt = request["messages"][-1].get("content") or ""
            offered_tools = bool(request.get("tools"))

            message: dict[str, Any] = {"content": None}
            with lock:
                state["prompts"].append({"prompt": prompt, "tools": offered_tools})
                if PLAN_MARKER in prompt:
                    message["content"] = json.dumps(PLAN)
                elif offered_tools and not state["sleep_issued"]:
                    # Handed out exactly once: the second attempt at this phase must
                    # be free to say "I checked, I am not repeating it", which is the
                    # behaviour the notice is supposed to produce.
                    state["sleep_issued"] = True
                    state["tool_turns"] += 1
                    message["tool_calls"] = [
                        {
                            "id": "call_sleep",
                            "type": "function",
                            "function": {
                                "name": "run",
                                "arguments": json.dumps({"command": COMMAND}),
                            },
                        }
                    ]
                elif offered_tools:
                    state["tool_turns"] += 1
                    message["content"] = (
                        "The previous attempt was interrupted mid-command; the workspace "
                        "shows nothing to undo, so I am not repeating it."
                    )
                else:
                    message["content"] = "Done."

            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        }
                    ],
                    "model": request.get("model", "?"),
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    return Handler


# ------------------------------------------------------------------ tiny http client


def call(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 15.0):
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


def wait_for(label: str, predicate, *, timeout: float = 60.0, interval: float = 0.5):
    """Poll until ``predicate`` returns something truthy, or fail saying what it saw."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = f"unreachable: {exc}"
        else:
            if last:
                return last
        time.sleep(interval)
    raise SystemExit(f"FAILED: {label} did not happen within {timeout}s (last saw: {last!r})")


def pgrep(pattern: str) -> list[str]:
    result = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True, check=False
    )
    return result.stdout.split()


def sleep_rows(job_id: str) -> list[dict[str, Any]]:
    rows = call("GET", f"/api/jobs/{job_id}/tool-calls") or []
    return [row for row in rows if row["args"].get("command") == COMMAND]


def restart_service(reason: str) -> None:
    """Restart the unit and wait until it answers again. Fatal if it will not come back."""
    print(f"restarting ({reason}): {RESTART_CMD}")
    restart = subprocess.run(RESTART_CMD, shell=True, capture_output=True, text=True, check=False)
    if restart.returncode != 0:
        raise SystemExit(
            f"FAILED: restart command exited {restart.returncode}: "
            f"{(restart.stderr or restart.stdout).strip()}\n"
            "Set AGENT_HUB_RESTART_CMD if the unit is managed differently here."
        )
    wait_for("the service came back", lambda: call("GET", "/api/health", timeout=3.0), timeout=90.0)
    print("  service is back")


def main() -> int:
    failures: list[str] = []

    server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"scripted provider on 127.0.0.1:{PORT}")

    # --- 0. test the code on disk, not whatever the service started with ----
    #
    # The unit is long-lived and this check is run immediately after editing the exact
    # paths it exercises, so a stale process would answer for code nobody is looking at.
    # That is not hypothetical: the first run after the interrupted-command fix reported
    # the original three failures verbatim, because the service was still running the
    # code from before it — the mid-run restart on line 2 is what loaded the fix, one
    # step too late for the row it was meant to protect.
    restart_service("so the service under test is the code on disk")

    call(
        "PUT",
        f"/api/providers/{PROVIDER_ID}",
        {
            "id": PROVIDER_ID,
            "label": "Restart check (local)",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{PORT}/v1",
            "model": "restart-check-1",
            "secret_ref": None,
            "headers": {},
            "enabled": True,
        },
    )

    # Unconfined on purpose. bwrap adds `--die-with-parent`, so a confined command
    # dies even if nothing else works; unconfined leans entirely on the engine's own
    # cancel path, which is the thing under test.
    job = call(
        "POST",
        "/api/jobs",
        {
            "task": f"Run `{COMMAND}` and report what happened.",
            "provider_id": PROVIDER_ID,
            "sandbox": "unconfined",
        },
    )
    job_id = job["id"]
    print(f"job {job_id} created")

    # --- 1. the command is genuinely in flight ------------------------------
    row = wait_for(
        "the command started",
        lambda: next((r for r in sleep_rows(job_id) if r["status"] == "running"), None),
    )
    print(f"  command running: id={row['id']} phase={row['phase_id']} turn={row['turn']}")
    pids = wait_for("the sleep process appeared", lambda: pgrep(COMMAND), timeout=15.0)
    print(f"  host sees the process: pids={pids}")

    prompts_before = len(state["prompts"])

    # --- 2. restart the service underneath it -------------------------------
    restart_service("mid-command, the thing under test")

    # --- 3. the row tells the truth, and nothing was orphaned ---------------
    resolved = wait_for(
        "the interrupted row was resolved",
        lambda: next((r for r in sleep_rows(job_id) if r["status"] != "running"), None),
        timeout=30.0,
    )
    if resolved["status"] != "interrupted":
        failures.append(
            f"the in-flight command ended as {resolved['status']!r}, not 'interrupted'"
        )
    else:
        print(f"  row resolved: status=interrupted exit_code={resolved['exit_code']}")

    orphans = pgrep(COMMAND)
    if orphans:
        # Do not leave an hour-long sleep behind whatever the verdict is.
        subprocess.run(["pkill", "-f", COMMAND], check=False)
        failures.append(f"the command outlived the service it belonged to (pids {orphans})")
    else:
        print("  no orphan process")

    # --- 4. the phase re-ran, was told why, and did not re-run the command --
    def finished() -> str | None:
        current = call("GET", f"/api/jobs/{job_id}")["status"]
        return current if current in {"complete", "error", "stopped"} else None

    status = wait_for("the job finished", finished, timeout=120.0)
    print(f"  job reached {status}")
    if status != "complete":
        failures.append(f"the job ended {status!r} instead of completing after the restart")

    snapshot = call("GET", f"/api/jobs/{job_id}")
    work = [phase for phase in snapshot["plan"] if phase["kind"] != "plan"]
    if not work:
        failures.append("the re-run never produced a work phase")
    else:
        phase = work[0]
        if phase["attempts"] < 2:
            failures.append(f"the phase ran {phase['attempts']} time(s); a re-run should be 2")
        else:
            print(f"  phase re-ran: attempts={phase['attempts']} status={phase['status']}")

    rows = sleep_rows(job_id)
    if len(rows) != 1:
        failures.append(
            f"the command was recorded {len(rows)} times; a resumed phase must not re-run it"
        )
    else:
        print("  the command was not executed twice")

    with lock:
        after = state["prompts"][prompts_before:]
    notice = [
        entry
        for entry in after
        if "previous attempt at this phase was interrupted" in entry["prompt"]
        and COMMAND in entry["prompt"]
    ]
    if not notice:
        failures.append(
            "the re-run prompt never named the interrupted command "
            f"({len(after)} prompts after the restart)"
        )
    else:
        print(f"  the re-run was told what was in flight ({len(notice)} prompt(s))")

    # --- tidy up ------------------------------------------------------------
    outcome = call("DELETE", f"/api/providers/{PROVIDER_ID}")
    print(f"provider cleanup: {outcome}")
    server.shutdown()

    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for failure in failures:
            print(" -", failure)
        return 1
    print("OK: row interrupted, no orphan, phase re-ran once with the notice, command ran once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
