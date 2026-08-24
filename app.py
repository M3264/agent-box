from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from runtime import ROLES, run_team

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).parent
DB = ROOT / "data" / "agent-hub.db"
SUBSCRIBERS: dict[str, set[asyncio.Queue]] = {}
RUN_TOKENS: dict[str, Any] = {}
ACTIVE_TASKS: set[asyncio.Task] = set()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        create table if not exists runs(
          id text primary key, task text not null, team_id integer not null,
          status text not null, created_at real not null, updated_at real not null,
          result text, error text
        );
        create table if not exists events(
          id integer primary key autoincrement, run_id text not null,
          created_at real not null, kind text not null, source text, payload text not null
        );
        create index if not exists events_run_idx on events(run_id,id);
        create table if not exists run_agents(
          run_id text not null, agent text not null, status text not null,
          current_action text, last_event_id integer, updated_at real not null,
          primary key(run_id, agent)
        );
        create table if not exists artifacts(
          id integer primary key autoincrement, run_id text not null,
          agent text, name text, mime_type text, content text,
          created_at real not null
        );
        create index if not exists artifacts_run_idx on artifacts(run_id,id);
        create table if not exists job_messages(
          id integer primary key autoincrement, run_id text not null,
          agent text not null, role text not null, content text not null,
          created_at real not null
        );
        create table if not exists plan_phases(
          id integer primary key autoincrement, run_id text not null,
          name text not null, owner text, status text not null,
          acceptance text, depends_on text, created_at real not null
        );
        create table if not exists approvals(
          id text primary key, run_id text not null, agent text,
          action text not null, risk text, status text not null,
          created_at real not null, decided_at real, decision_note text
        );
        create table if not exists provider_profiles(
          id text primary key, label text not null, base_url text not null,
          model text not null, secret_ref text, headers text, enabled integer not null default 1
        );
        create table if not exists team_templates(
          id integer primary key autoincrement, name text not null,
          version integer not null, roles text not null, created_at real not null
        );
        """)
        # Keep existing installations compatible with the richer job model.
        for stmt in ("alter table runs add column mode text not null default 'controlled'",
                     "alter table runs add column paused integer not null default 0",
                     "alter table runs add column workspace text"):
            try: c.execute(stmt)
            except sqlite3.OperationalError: pass
        if not c.execute("select 1 from team_templates limit 1").fetchone():
            c.execute("insert into team_templates(name,version,roles,created_at) values(?,?,?,?)",
                      ("Default delivery team", 1, json.dumps([{"id": a, "name": n, "instructions": r} for a,n,r in ROLES]), time.time()))
        if not c.execute("select 1 from provider_profiles limit 1").fetchone():
            cfg_path = Path('/home/ubuntu/.codex/config.toml')
            if cfg_path.exists():
                try:
                    cfg = tomllib.loads(cfg_path.read_text())
                    name = cfg.get('model_provider', 'agentrouter')
                    p = cfg.get('model_providers', {}).get(name, {})
                    if p.get('base_url'):
                        c.execute("insert into provider_profiles(id,label,base_url,model,secret_ref,headers,enabled) values(?,?,?,?,?,?,?)",
                                  (name, name.title(), p['base_url'], cfg.get('model', 'gpt-5.6-sol'), 'experimental_bearer_token', json.dumps({'originator': 'codex_cli_rs'}), 1))
                except Exception:
                    pass


def event(run_id: str, kind: str, payload: Any, source: str | None = None):
    now = time.time()
    record = {"run_id": run_id, "created_at": now, "kind": kind, "source": source, "payload": payload}
    with db() as c:
        c.execute("insert into events(run_id,created_at,kind,source,payload) values(?,?,?,?,?)",
                  (run_id, now, kind, source, json.dumps(payload, default=str)))
        c.execute("update runs set updated_at=? where id=?", (now, run_id))
        record["id"] = c.execute("select last_insert_rowid()").fetchone()[0]
    for queue in list(SUBSCRIBERS.get(run_id, ())):
        queue.put_nowait(record)
    return record


def update_agent(run_id: str, agent: str | None, status: str, action: str | None = None,
                 event_id: int | None = None):
    if not agent:
        return
    now = time.time()
    with db() as c:
        c.execute("""insert into run_agents(run_id,agent,status,current_action,last_event_id,updated_at)
                    values(?,?,?,?,?,?) on conflict(run_id,agent) do update set
                    status=excluded.status,current_action=excluded.current_action,
                    last_event_id=excluded.last_event_id,updated_at=excluded.updated_at""",
                  (run_id, agent, status, action, event_id, now))


def classify_message(msg: Any, payload: dict) -> tuple[str, str | None]:
    """Map framework message classes to stable UI event categories."""
    name = msg.__class__.__name__.lower()
    text = json.dumps(payload, default=str).lower()
    if "handoff" in name or "handoff" in text:
        return "handoff", "Handing work to another agent"
    if "tool" in name or "function" in name or "tool_call" in text:
        return "tool_call", "Using a tool"
    if "result" in name or "taskresult" in name:
        return "result", "Producing result"
    if "error" in name:
        return "error", "Encountered an error"
    return "message", "Working"


def maybe_artifact(run_id: str, source: str | None, payload: dict):
    # Keep artifact capture conservative: only explicit artifact-shaped fields.
    candidates = payload.get("artifacts") or payload.get("artifact")
    if not candidates:
        return
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return
    with db() as c:
        for item in candidates:
            if not isinstance(item, dict):
                continue
            c.execute("insert into artifacts(run_id,agent,name,mime_type,content,created_at) values(?,?,?,?,?,?)",
                      (run_id, source, item.get("name"), item.get("mime_type") or item.get("mimeType"),
                       item.get("content") if isinstance(item.get("content"), str) else json.dumps(item.get("content"), default=str), time.time()))


async def execute(run_id: str, task: str, team_id: int):
    try:
        RUN_TOKENS.setdefault(run_id, asyncio.Event())
        with db() as c:
            c.execute("update runs set status='running',updated_at=? where id=?", (time.time(), run_id))
        event(run_id, "status", {"status": "running"}, "system")
        for agent, _, _ in ROLES:
            update_agent(run_id, agent, "queued", "Queued")

        async def emit(kind: str, payload: dict, source: str | None):
            rec = event(run_id, kind, payload, source)
            if kind == "agent_state":
                update_agent(run_id, source, payload.get("status", "active"), payload.get("current_action"), rec.get("id"))
            elif source:
                update_agent(run_id, source, "active", payload.get("current_action") or ("Handing off work" if kind == "handoff" else "Working"), rec.get("id"))
            if kind == "result":
                with db() as c: c.execute("update runs set result=?,updated_at=? where id=?", (json.dumps(payload), time.time(), run_id))

        def paused():
            with db() as c:
                row = c.execute("select paused from runs where id=?", (run_id,)).fetchone()
                return bool(row and row[0])
        emit.is_paused = paused
        result = await run_team(task, emit, lambda: run_id not in RUN_TOKENS or RUN_TOKENS[run_id].is_set())
        with db() as c:
            status = c.execute("select status from runs where id=?", (run_id,)).fetchone()[0]
            if status != "stopped":
                c.execute("update runs set status='complete',result=?,updated_at=? where id=?", (json.dumps({"content": result}), time.time(), run_id))
        if status != "stopped":
            event(run_id, "status", {"status": "complete"}, "system")
            with db() as c:
                c.execute("update run_agents set status='complete',current_action='Complete',updated_at=? where run_id=?", (time.time(), run_id))
    except Exception as e:
        with db() as c:
            c.execute("update runs set status='error',error=?,updated_at=? where id=?", (str(e),time.time(),run_id))
        event(run_id, "error", {"error": str(e)}, "system")
        with db() as c:
            c.execute("update run_agents set status='error',current_action='Error',updated_at=? where run_id=?", (time.time(), run_id))
    finally:
        RUN_TOKENS.pop(run_id, None)


class RunRequest(BaseModel):
    task: str
    team_id: int = 3
    mode: str = "controlled"
    provider_id: str | None = None

class MessageRequest(BaseModel):
    content: str
    agent: str = "project_lead"

class DecisionRequest(BaseModel):
    decision: str
    note: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with db() as c:
        pending = c.execute("select id,task,team_id from runs where status in ('queued','running')").fetchall()
        c.execute("update runs set status='queued' where status='running'")
    for row in pending:
        RUN_TOKENS[row["id"]] = asyncio.Event()
        launch(execute(row["id"], row["task"], row["team_id"]))
    yield


app = FastAPI(title="Agent Hub", lifespan=lifespan)


def launch(coro):
    task = asyncio.create_task(coro)
    ACTIVE_TASKS.add(task)
    task.add_done_callback(ACTIVE_TASKS.discard)
    return task


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/runs")
async def create_run(req: RunRequest):
    run_id = uuid.uuid4().hex[:12]
    now = time.time()
    with db() as c:
        workspace = str(ROOT / "data" / "workspaces" / run_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        c.execute("insert into runs(id,task,team_id,status,created_at,updated_at,result,error,mode,paused,workspace) values(?,?,?,?,?,?,?,?,?,?,?)",
                  (run_id, req.task, req.team_id, "queued", now, now, None, None, req.mode if req.mode in {"controlled", "yolo"} else "controlled", 0, workspace))
        for i, (agent, label, role) in enumerate(ROLES):
            c.execute("insert into plan_phases(run_id,name,owner,status,acceptance,depends_on,created_at) values(?,?,?,?,?,?,?)",
                      (run_id, label, agent, "queued", role, json.dumps([ROLES[i-1][0]]) if i else "[]", now))
    RUN_TOKENS[run_id] = asyncio.Event()
    event(run_id, "status", {"status": "queued"}, "system")
    launch(execute(run_id, req.task, req.team_id))
    return {"id": run_id, "status": "queued"}

@app.post("/api/runs/{run_id}/messages")
async def send_message(run_id: str, req: MessageRequest):
    with db() as c:
        if not c.execute("select 1 from runs where id=?", (run_id,)).fetchone(): raise HTTPException(404, "run not found")
        c.execute("insert into job_messages(run_id,agent,role,content,created_at) values(?,?,?,?,?)", (run_id, req.agent, "operator", req.content, time.time()))
    return event(run_id, "message", {"content": req.content, "operator": True}, req.agent)

@app.get("/api/runs/{run_id}/messages")
async def messages(run_id: str):
    with db() as c: return [dict(x) for x in c.execute("select * from job_messages where run_id=? order by id", (run_id,))]

@app.get("/api/runs/{run_id}/plan")
async def plan(run_id: str):
    with db() as c: return [dict(x) for x in c.execute("select * from plan_phases where run_id=? order by id", (run_id,))]

@app.get("/api/approvals")
async def approvals(status: str = "pending"):
    with db() as c: return [dict(x) for x in c.execute("select * from approvals where status=? order by created_at", (status,))]

@app.post("/api/runs/{run_id}/approvals")
async def create_approval(run_id: str, req: dict):
    aid = uuid.uuid4().hex[:10]
    with db() as c: c.execute("insert into approvals values(?,?,?,?,?,?,?, ?,?)", (aid, run_id, req.get("agent"), req.get("action", "Action"), req.get("risk", "high"), "pending", time.time(), None, None))
    event(run_id, "approval", {"approval_id": aid, "action": req.get("action", "Action"), "status": "pending"}, req.get("agent"))
    return {"id": aid, "status": "pending"}

@app.post("/api/runs/{run_id}/approvals/{approval_id}")
async def decide_approval(run_id: str, approval_id: str, req: DecisionRequest):
    decision = req.decision if req.decision in {"approved", "rejected"} else "rejected"
    with db() as c:
        cur = c.execute("update approvals set status=?,decided_at=?,decision_note=? where id=? and run_id=? and status='pending'", (decision, time.time(), req.note, approval_id, run_id))
        if not cur.rowcount: raise HTTPException(404, "pending approval not found")
    return event(run_id, "approval", {"approval_id": approval_id, "status": decision, "note": req.note}, "operator")

@app.post("/api/runs/{run_id}/pause")
async def pause_run(run_id: str):
    with db() as c: c.execute("update runs set paused=1,updated_at=? where id=? and status in ('queued','running')", (time.time(), run_id))
    return event(run_id, "status", {"status": "paused"}, "system")

@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    with db() as c: c.execute("update runs set paused=0,updated_at=? where id=?", (time.time(), run_id))
    return event(run_id, "status", {"status": "running"}, "system")

@app.get("/api/templates")
async def templates():
    with db() as c: return [dict(x) for x in c.execute("select * from team_templates order by id desc")]

@app.get("/api/providers")
async def providers():
    with db() as c: return [dict(x) for x in c.execute("select id,label,base_url,model,secret_ref,headers,enabled from provider_profiles")]


@app.get("/api/runs")
async def list_runs():
    with db() as c:
        return [dict(r) for r in c.execute("select * from runs order by created_at desc limit 100")]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    with db() as c:
        r = c.execute("select * from runs where id=?", (run_id,)).fetchone()
        if not r: raise HTTPException(404, "run not found")
        events = [dict(e) for e in c.execute("select * from events where run_id=? order by id", (run_id,))]
        agents = [dict(a) for a in c.execute("select * from run_agents where run_id=? order by agent", (run_id,))]
        artifacts = [dict(a) for a in c.execute("select * from artifacts where run_id=? order by id", (run_id,))]
        messages = [dict(a) for a in c.execute("select * from job_messages where run_id=? order by id", (run_id,))]
        phases = [dict(a) for a in c.execute("select * from plan_phases where run_id=? order by id", (run_id,))]
        approvals = [dict(a) for a in c.execute("select * from approvals where run_id=? order by created_at", (run_id,))]
    for e in events: e["payload"] = json.loads(e["payload"])
    return {**dict(r), "events": events, "agents": agents, "artifacts": artifacts, "messages": messages, "plan": phases, "approvals": approvals}


@app.get("/api/runs/{run_id}/agents")
async def run_agents(run_id: str):
    with db() as c:
        if not c.execute("select 1 from runs where id=?", (run_id,)).fetchone():
            raise HTTPException(404, "run not found")
        return [dict(a) for a in c.execute("select * from run_agents where run_id=? order by agent", (run_id,))]


@app.get("/api/runs/{run_id}/artifacts")
async def run_artifacts(run_id: str):
    with db() as c:
        if not c.execute("select 1 from runs where id=?", (run_id,)).fetchone():
            raise HTTPException(404, "run not found")
        return [dict(a) for a in c.execute("select * from artifacts where run_id=? order by id", (run_id,))]


@app.get("/api/runs/{run_id}/events/history")
async def list_events(run_id: str, after: int = 0, limit: int = 500):
    with db() as c:
        if not c.execute("select 1 from runs where id=?", (run_id,)).fetchone():
            raise HTTPException(404, "run not found")
        rows = c.execute("select * from events where run_id=? and id>? order by id limit ?", (run_id, after, min(limit, 5000))).fetchall()
    return [{**dict(e), "payload": json.loads(e["payload"])} for e in rows]


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str):
    with db() as c:
        row = c.execute("select status from runs where id=?", (run_id,)).fetchone()
        if not row: raise HTTPException(404, "run not found")
        if row[0] in {"complete", "error", "stopped"}: return {"id": run_id, "status": row[0]}
        c.execute("update runs set status='stopped',updated_at=? where id=?", (time.time(), run_id))
    token = RUN_TOKENS.get(run_id)
    if token: token.set()
    with db() as c:
        c.execute("update run_agents set status='stopped',current_action='Stopped',updated_at=? where run_id=?", (time.time(), run_id))
    event(run_id, "status", {"status": "stopped"}, "system")
    return {"id": run_id, "status": "stopped"}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request):
    # Validate up front so clients do not wait forever on a typoed run ID.
    with db() as c:
        exists = c.execute("select 1 from runs where id=?", (run_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "run not found")

    async def gen():
        # EventSource sends this header when reconnecting; accepting it makes
        # browser and CLI consumers resume without replaying old events.
        raw_last = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        try:
            last = max(0, int(raw_last or 0))
        except ValueError:
            last = 0
        while True:
            if await request.is_disconnected(): return
            with db() as c:
                rows = c.execute("select * from events where run_id=? and id>? order by id", (run_id,last)).fetchall()
                status = c.execute("select status from runs where id=?", (run_id,)).fetchone()
            for row in rows:
                last = row["id"]
                yield f"id: {last}\ndata: {json.dumps({**dict(row), 'payload': json.loads(row['payload'])}, default=str)}\n\n"
            if status and status[0] in ("complete", "error", "stopped") and not rows: return
            await asyncio.sleep(.5)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.websocket("/ws/runs/{run_id}")
async def run_socket(websocket: WebSocket, run_id: str):
    await websocket.accept()
    try:
        last = int(websocket.query_params.get("after", "0"))
        with db() as c:
            exists = c.execute("select 1 from runs where id=?", (run_id,)).fetchone()
            rows = c.execute("select * from events where run_id=? and id>? order by id", (run_id, last)).fetchall()
            status = c.execute("select status from runs where id=?", (run_id,)).fetchone()
        if not exists:
            await websocket.close(code=4404); return
        for row in rows:
            await websocket.send_json({**dict(row), "payload": json.loads(row["payload"])})
            last = row["id"]
        if status and status[0] in {"complete", "error", "stopped"}:
            await websocket.close(code=1000)
            return
        queue: asyncio.Queue = asyncio.Queue()
        SUBSCRIBERS.setdefault(run_id, set()).add(queue)
        while True:
            item = await queue.get()
            if item["id"] <= last: continue
            await websocket.send_json(item)
            last = item["id"]
            if item["kind"] == "status" and item["payload"].get("status") in {"complete", "error", "stopped"}:
                return
    except WebSocketDisconnect:
        return
    finally:
        for subscribers in [SUBSCRIBERS.get(run_id, set())]:
            subscribers.difference_update({queue}) if "queue" in locals() else None
            if not subscribers: SUBSCRIBERS.pop(run_id, None)
