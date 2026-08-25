#!/usr/bin/env python3
"""Command-line client for Agent Hub.

``job`` verbs are primary now that the API speaks ``/api/jobs``. v1 kept an alias
table mapping the documented ``job create`` vocabulary onto internal ``run``
commands, because the endpoints were ``/api/runs``; the rename removed the reason
for the shim.

The follow loop is SSE, not WebSocket: ``Last-Event-ID`` reconnect-and-resume is
exactly what a terminal client wants and it already worked, so it is kept — with
one fix. A stream can end without the job being finished (an idle proxy timing the
connection out), and v1 treated any clean end as "job done" and exited. This
version asks the server before deciding.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

DEFAULT_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:8090")

TERMINAL = {"complete", "error", "stopped"}


class CliError(RuntimeError):
    pass


def check(response: httpx.Response) -> httpx.Response:
    """Return the response, or raise with the server's own error text."""
    if not response.is_error:
        return response
    try:
        payload = response.json()
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    except ValueError:
        detail = response.text.strip() or response.reason_phrase
    if isinstance(detail, list):  # pydantic validation errors
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', [])[1:])}: {item.get('msg', '')}"
            for item in detail
            if isinstance(item, dict)
        )
    raise CliError(f"HTTP {response.status_code}: {detail}")


def dump(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def one_line(event: dict[str, Any]) -> str:
    """A readable line per event, falling back to the raw payload."""
    payload = event.get("payload") or {}
    kind = event.get("kind", "?")
    source = event.get("source") or "system"

    def field(name: str) -> str:
        return str(payload.get(name, "")).strip()

    if kind == "message":
        head = f" ({field('phase')})" if payload.get("phase") else ""
        return f"[{source}]{head} {field('content')}"
    if kind == "result":
        return f"[{source}] RESULT\n{field('content')}"
    if kind == "phase":
        return f"[{source}] phase {field('seq')} {field('name')} -> {field('status')}"
    if kind == "status":
        return f"[{source}] job {field('status')}"
    if kind == "agent_state":
        return f"[{source}] {field('status')}: {field('current_action')}"
    if kind == "handoff":
        return f"[{source}] handoff {field('from')} -> {field('to')}"
    if kind == "approval":
        gate = field("approval_id")
        action = field("action")
        auto = " (auto)" if payload.get("auto") else ""
        return f"[{source}] approval {field('status')}{auto} {gate}{f': {action}' if action else ''}"
    if kind == "artifact":
        return f"[{source}] artifact {field('name')}"
    if kind == "plan":
        phases = payload.get("phases") or []
        lines = [f"[{source}] plan: {len(phases)} phases"]
        lines += [
            f"    {index}. {item.get('name')} — {item.get('owner')}"
            for index, item in enumerate(phases, start=1)
            if isinstance(item, dict)
        ]
        return "\n".join(lines)
    if kind == "guidance":
        return f"[{source}] guidance delivered: {len(payload.get('messages') or [])} message(s)"
    if kind == "notice":
        return f"[{source}] {field('message')}"
    if kind == "error":
        return f"[{source}] ERROR {field('error')}"
    return f"[{source}] {kind} {json.dumps(payload, ensure_ascii=False)}"


def follow(client: httpx.Client, job_id: str, after: int = 0) -> str:
    """Print events until the job reaches a terminal status. Returns that status."""
    cursor = after
    while True:
        try:
            with client.stream(
                "GET",
                f"/api/jobs/{job_id}/events",
                headers={"Last-Event-ID": str(cursor), "Accept": "text/event-stream"},
            ) as stream:
                check(stream)
                for line in stream.iter_lines():
                    if line.startswith("id: "):
                        try:
                            cursor = int(line[4:])
                        except ValueError:
                            pass
                    elif line.startswith("data: "):
                        try:
                            print(one_line(json.loads(line[6:])), flush=True)
                        except (ValueError, KeyError):
                            print(line[6:], flush=True)
        except (httpx.HTTPError, OSError) as exc:
            print(f"connection lost ({exc}); resuming from event {cursor}", file=sys.stderr, flush=True)
            time.sleep(1)
            continue

        # A clean end is not proof the job finished — ask, and only stop if it did.
        status = check(client.get(f"/api/jobs/{job_id}")).json().get("status", "")
        if status in TERMINAL:
            return str(status)
        time.sleep(1)


# ----------------------------------------------------------------------- commands


def cmd_create(client: httpx.Client, args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"task": args.task, "mode": args.mode}
    if args.team is not None:
        payload["team_id"] = args.team
    if args.provider:
        payload["provider_id"] = args.provider

    created = check(client.post("/api/jobs", json=payload)).json()
    job_id = created["id"]
    print(f"job {job_id} ({created['mode']}, team {created['team_id']})", flush=True)
    if args.detach:
        return 0
    status = follow(client, job_id)
    print(f"job {job_id} {status}", flush=True)
    return 0 if status == "complete" else 1


def cmd_list(client: httpx.Client, args: argparse.Namespace) -> int:
    jobs = check(client.get("/api/jobs", params={"limit": args.limit})).json()
    if args.json:
        dump(jobs)
        return 0
    if not jobs:
        print("no jobs")
        return 0
    print(f"{'ID':<14}{'STATUS':<12}{'PHASES':<9}{'GATES':<7}TASK")
    for job in jobs:
        status = f"{job['status']}{'*' if job['paused'] else ''}"
        phases = f"{job['phase_complete']}/{job['phase_total']}"
        gates = str(job["pending_approvals"] or "")
        task = job["task"].replace("\n", " ")
        print(f"{job['id']:<14}{status:<12}{phases:<9}{gates:<7}{task[:64]}")
    return 0


def cmd_show(client: httpx.Client, args: argparse.Namespace) -> int:
    job = check(client.get(f"/api/jobs/{args.job_id}")).json()
    if args.json:
        dump(job)
        return 0

    print(f"job      {job['id']}")
    print(f"task     {job['task']}")
    print(f"status   {job['status']}{' (paused)' if job['paused'] else ''}")
    print(f"mode     {job['mode']}   team {job['team_id']}   provider {job['provider_id'] or 'default'}")
    if job.get("error"):
        print(f"error    {job['error']}")

    print("\nphases")
    for phase in job["plan"]:
        gate = " [gated]" if phase["requires_approval"] else ""
        print(f"  {phase['seq']:>2}. {phase['status']:<20}{phase['owner']:<12}{phase['name']}{gate}")

    if job["team"]:
        print("\nagents")
        for agent in job["team"]:
            print(f"  {agent['agent']:<12}{agent['status']:<12}{agent['current_action'] or ''}")

    pending = [gate for gate in job["approvals"] if gate["status"] == "pending"]
    if pending:
        print("\napprovals waiting")
        for gate in pending:
            print(f"  {gate['id']}  {gate['action']}")

    if job["artifacts"]:
        print("\nartifacts")
        for artifact in job["artifacts"]:
            print(f"  {artifact['id']:>3}  {artifact['name']:<28}{artifact['size']:>8} B")

    if job.get("result"):
        print(f"\nresult\n{job['result'].get('content', '')}")
    return 0


def cmd_follow(client: httpx.Client, args: argparse.Namespace) -> int:
    status = follow(client, args.job_id, after=args.after)
    print(f"job {args.job_id} {status}", flush=True)
    return 0 if status == "complete" else 1


def cmd_plan(client: httpx.Client, args: argparse.Namespace) -> int:
    phases = check(client.get(f"/api/jobs/{args.job_id}/plan")).json()
    if args.json:
        dump(phases)
        return 0
    if not phases:
        print("no plan yet")
        return 0
    for phase in phases:
        print(f"{phase['seq']:>2}. {phase['status']:<20}{phase['owner']:<12}{phase['name']}")
        if phase.get("acceptance"):
            print(f"      acceptance: {phase['acceptance']}")
        if phase.get("error"):
            print(f"      error: {phase['error']}")
    return 0


def cmd_approvals(client: httpx.Client, args: argparse.Namespace) -> int:
    if args.job_id:
        gates = check(client.get(f"/api/jobs/{args.job_id}/approvals")).json()
    else:
        gates = check(client.get("/api/approvals", params={"status": args.status})).json()
    if args.json:
        dump(gates)
        return 0
    if not gates:
        print("nothing waiting" if args.status == "pending" else "no approvals")
        return 0
    for gate in gates:
        where = gate.get("job_task") or gate.get("phase_name") or ""
        print(f"{gate['id']}  {gate['status']:<10}{gate['action']}")
        if where:
            print(f"    {str(where)[:80]}")
        if gate.get("detail"):
            print(f"    {gate['detail'][:200]}")
    return 0


def cmd_decide(client: httpx.Client, args: argparse.Namespace) -> int:
    decision = "approved" if args.command == "approve" else "rejected"
    result = check(
        client.post(
            f"/api/jobs/{args.job_id}/approvals/{args.approval_id}",
            json={"decision": decision, "note": args.note},
        )
    ).json()
    print(f"{result['id']} {result['status']}")
    return 0


def cmd_message(client: httpx.Client, args: argparse.Namespace) -> int:
    sent = check(
        client.post(f"/api/jobs/{args.job_id}/messages", json={"content": args.content})
    ).json()
    print(f"message {sent['id']} queued; the team picks it up before the next phase")
    return 0


def cmd_control(client: httpx.Client, args: argparse.Namespace) -> int:
    result = check(client.post(f"/api/jobs/{args.job_id}/{args.command}")).json()
    print(f"{result['id']} {result['status']}{' (paused)' if result['paused'] else ''}")
    return 0


def cmd_artifact(client: httpx.Client, args: argparse.Namespace) -> int:
    response = check(client.get(f"/api/jobs/{args.job_id}/artifacts/{args.artifact_id}"))
    sys.stdout.write(response.text)
    return 0


def cmd_providers(client: httpx.Client, args: argparse.Namespace) -> int:
    profiles = check(client.get("/api/providers")).json()
    if args.json:
        dump(profiles)
        return 0
    for profile in profiles:
        # secret_ok is a resolution check, not the secret: the API never returns one.
        secret = (
            "no secret"
            if profile["secret_ok"] is None
            else f"{profile['secret_ref']} {'ok' if profile['secret_ok'] else 'MISSING'}"
        )
        state = "enabled" if profile["enabled"] else "disabled"
        print(f"{profile['id']:<16}{state:<10}{profile['model']:<20}{profile['base_url']}")
        print(f"{'':<16}{secret}")
    return 0


def cmd_teams(client: httpx.Client, args: argparse.Namespace) -> int:
    teams = check(client.get("/api/teams")).json()
    if args.json:
        dump(teams)
        return 0
    for team in teams:
        mark = " (default)" if team["is_default"] else ""
        roles = ", ".join(
            f"{role['id']}{'*' if role.get('orchestrator') else ''}" for role in team["roles"]
        )
        print(f"{team['id']:>3}  {team['name']}{mark}\n     {roles}")
    return 0


def cmd_health(client: httpx.Client, args: argparse.Namespace) -> int:
    health = check(client.get("/api/health")).json()
    if args.json:
        dump(health)
        return 0
    detail = health.get("detail", {})
    print(f"{health['status']}  v{health['version']}  schema {health['schema_version']}")
    print(f"active jobs {health['active_jobs']}   subscribers {detail.get('subscribers', 0)}")
    print(f"jobs {detail.get('jobs', 0)}   pending approvals {detail.get('pending_approvals', 0)}")
    print(f"db {detail.get('db_path', '?')}")
    if detail.get("error"):
        print(f"error {detail['error']}")
    return 0


# ------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-hub", description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL, help=f"service base URL (default {DEFAULT_URL})")
    parser.add_argument("--json", action="store_true", help="print raw JSON instead of a summary")
    top = parser.add_subparsers(dest="group", required=True)

    # Accepted before or after the verb. SUPPRESS matters: a plain `default=False`
    # here would overwrite a `--json` given before the verb.
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    job = top.add_parser("job", help="work with jobs").add_subparsers(dest="command", required=True)

    create = job.add_parser("create", help="create a job and follow it")
    create.add_argument("task")
    # No default: omitting team_id lets the server pick the template flagged
    # default. v1 defaulted to team 3, which never existed.
    create.add_argument("--team", type=int, default=None)
    create.add_argument("--mode", choices=["controlled", "yolo"], default="controlled")
    create.add_argument("--provider", default=None)
    create.add_argument("--detach", action="store_true", help="return without following events")
    create.set_defaults(handler=cmd_create)

    listing = job.add_parser("list", help="list jobs", parents=[output])
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(handler=cmd_list)

    show = job.add_parser("show", help="full job state", parents=[output])
    show.add_argument("job_id")
    show.set_defaults(handler=cmd_show)

    attach = job.add_parser("follow", aliases=["attach"], help="stream events from a cursor")
    attach.add_argument("job_id")
    attach.add_argument("--after", type=int, default=0, help="resume after this event id")
    attach.set_defaults(handler=cmd_follow)

    plan = job.add_parser("plan", help="the phases the manager authored", parents=[output])
    plan.add_argument("job_id")
    plan.set_defaults(handler=cmd_plan)

    gates = job.add_parser("approvals", help="approvals for one job", parents=[output])
    gates.add_argument("job_id")
    gates.set_defaults(handler=cmd_approvals, status="all")

    message = job.add_parser("message", help="send operator guidance")
    message.add_argument("job_id")
    message.add_argument("content")
    message.set_defaults(handler=cmd_message)

    for name, help_text in (
        ("approve", "approve a gate"),
        ("reject", "reject a gate; the phase is skipped"),
    ):
        decide = job.add_parser(name, help=help_text)
        decide.add_argument("job_id")
        decide.add_argument("approval_id")
        decide.add_argument("--note", default=None, help="recorded with the decision")
        decide.set_defaults(handler=cmd_decide)

    for name, help_text in (
        ("pause", "pause at the next phase boundary"),
        ("resume", "resume a paused job"),
        ("stop", "stop, cancelling any in-flight provider call"),
    ):
        control = job.add_parser(name, help=help_text)
        control.add_argument("job_id")
        control.set_defaults(handler=cmd_control)

    artifact = top.add_parser("artifact", help="artifacts").add_subparsers(
        dest="command", required=True
    )
    fetch = artifact.add_parser("get", help="print an artifact to stdout")
    fetch.add_argument("job_id")
    fetch.add_argument("artifact_id", type=int)
    fetch.set_defaults(handler=cmd_artifact)

    inbox = top.add_parser("approvals", help="approvals across all jobs", parents=[output])
    inbox.add_argument("--status", choices=["pending", "approved", "rejected", "all"], default="pending")
    inbox.set_defaults(handler=cmd_approvals, job_id=None, command="approvals")

    providers = top.add_parser("providers", help="configured provider profiles", parents=[output])
    providers.set_defaults(handler=cmd_providers, command="providers")

    teams = top.add_parser("teams", help="team templates", parents=[output])
    teams.set_defaults(handler=cmd_teams, command="teams")

    health = top.add_parser("health", help="service health", parents=[output])
    health.set_defaults(handler=cmd_health, command="health")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    # No read timeout: `job create` follows an SSE stream that is idle between
    # phases, and a provider call can legitimately take minutes.
    timeout = httpx.Timeout(None, connect=10.0)
    with httpx.Client(base_url=args.url, timeout=timeout) as client:
        handler = args.handler
        return int(handler(client, args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted (the job keeps running; reattach with `job follow`)", file=sys.stderr)
        raise SystemExit(130) from None
    except (CliError, httpx.HTTPError) as exc:
        print(f"agent-hub: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
