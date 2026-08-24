from __future__ import annotations
import argparse, json, sys, time
import httpx


def _raise(r: httpx.Response):
    try:
        detail = r.json()
    except ValueError:
        detail = r.text
    raise RuntimeError(f"HTTP {r.status_code}: {detail}")

def main():
    # Keep the documented command vocabulary while retaining a compact parser.
    aliases = {("job", "create"): "run", ("job", "list"): "list", ("job", "show"): "show",
               ("job", "attach"): "show", ("job", "message"): "message", ("job", "approve"): "approve",
               ("job", "reject"): "reject", ("job", "pause"): "pause", ("job", "resume"): "resume",
               ("job", "stop"): "stop", ("agent", "inspect"): "show", ("artifact", "list"): "show"}
    if len(sys.argv) >= 3 and (sys.argv[1], sys.argv[2]) in aliases:
        sys.argv[1:3] = [aliases[(sys.argv[1], sys.argv[2])]]
    p=argparse.ArgumentParser(prog="agent-hub")
    p.add_argument("--url", default="http://127.0.0.1:8090")
    sub=p.add_subparsers(dest="command", required=True)
    run=sub.add_parser("run"); run.add_argument("task"); run.add_argument("--team",type=int,default=3); run.add_argument("--mode",choices=["controlled","yolo"],default="controlled")
    run.add_argument("--detach", action="store_true", help="queue the run and return without following events")
    sub.add_parser("list")
    show=sub.add_parser("show"); show.add_argument("run_id")
    stop=sub.add_parser("stop"); stop.add_argument("run_id")
    for name in ("pause", "resume"):
        x=sub.add_parser(name); x.add_argument("run_id")
    msg=sub.add_parser("message"); msg.add_argument("run_id"); msg.add_argument("content")
    appr=sub.add_parser("approve"); appr.add_argument("run_id"); appr.add_argument("approval_id")
    rej=sub.add_parser("reject"); rej.add_argument("run_id"); rej.add_argument("approval_id")
    a=p.parse_args()
    with httpx.Client(base_url=a.url, timeout=None) as c:
        if a.command == "run":
            r=c.post("/api/runs",json={"task":a.task,"team_id":a.team,"mode":a.mode})
            if r.is_error: _raise(r)
            rid=r.json()["id"]; print(f"run {rid}", flush=True)
            if a.detach:
                return
            last = 0
            # Reconnect on transient network failures and resume from the last
            # event received. The server also accepts Last-Event-ID directly.
            while True:
                try:
                    with c.stream("GET",f"/api/runs/{rid}/events",headers={"Last-Event-ID":str(last)}) as s:
                        if s.is_error: _raise(s)
                        for line in s.iter_lines():
                            if line.startswith("id: "):
                                try: last = int(line[4:])
                                except ValueError: pass
                            elif line.startswith("data: "):
                                e=json.loads(line[6:]); print(f"[{e.get('source') or e['kind']}] {json.dumps(e['payload'],ensure_ascii=False)}",flush=True)
                    break
                except (httpx.HTTPError, OSError) as exc:
                    print(f"connection lost ({exc}); retrying...", file=sys.stderr, flush=True)
                    time.sleep(1)
        elif a.command == "list":
            r=c.get("/api/runs");
            if r.is_error: _raise(r)
            print(json.dumps(r.json(),indent=2))
        elif a.command == "show":
            r=c.get(f"/api/runs/{a.run_id}");
            if r.is_error: _raise(r)
            print(json.dumps(r.json(),indent=2))
        elif a.command in {"stop", "pause", "resume"}:
            r=c.post(f"/api/runs/{a.run_id}/{a.command}")
            if r.is_error: _raise(r)
            print(json.dumps(r.json()))
        elif a.command == "message":
            r=c.post(f"/api/runs/{a.run_id}/messages", json={"content": a.content})
            if r.is_error: _raise(r)
            print(json.dumps(r.json()))
        elif a.command in {"approve", "reject"}:
            r=c.post(f"/api/runs/{a.run_id}/approvals/{a.approval_id}", json={"decision": "approved" if a.command == "approve" else "rejected"})
            if r.is_error: _raise(r)
            print(json.dumps(r.json()))

if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError, KeyboardInterrupt) as exc:
        print(f"agent-hub: {exc}", file=sys.stderr)
        raise SystemExit(1)
