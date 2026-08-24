from __future__ import annotations

import asyncio
import json
import os
import tomllib
from pathlib import Path
from typing import Awaitable, Callable

import httpx

CONFIG = Path("/home/ubuntu/.codex/config.toml")
ROLES = [
    ("project_lead", "Project lead", "Break the task into a concrete plan, define acceptance criteria, and coordinate the specialists."),
    ("researcher", "Researcher", "Investigate facts, options, and risks. Return concise evidence and sources when relevant."),
    ("builder", "Builder", "Turn the plan and research into a practical implementation or detailed solution."),
    ("reviewer", "Reviewer", "Challenge assumptions, find gaps, and propose precise corrections."),
    ("tester", "Tester", "Verify the proposed result against the task and acceptance criteria. Report failures and final confidence."),
]


def provider():
    with CONFIG.open("rb") as f:
        cfg = tomllib.load(f)
    p = cfg["model_providers"][cfg.get("model_provider", "agentrouter")]
    return p["base_url"].rstrip("/"), p["experimental_bearer_token"], cfg.get("model", "gpt-5.6-sol")


async def ask(client: httpx.AsyncClient, base: str, token: str, model: str, system: str, prompt: str) -> str:
    response = await client.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {token}", "originator": "codex_cli_rs"}, json={
        "model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.2,
    })
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


async def run_team(task: str, emit: Callable[[str, str, dict, str | None], Awaitable[None]], cancelled: Callable[[], bool]) -> str:
    base, token, model = provider()
    outputs: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
        for index, (agent, label, role) in enumerate(ROLES):
            if cancelled(): break
            # The supervisor persists pause state; waiting here keeps the job
            # durable across browser disconnects and lets the operator resume it.
            while not cancelled() and getattr(emit, "is_paused", lambda: False)():
                await asyncio.sleep(0.5)
            if index:
                await emit("handoff", {"from": ROLES[index - 1][0], "to": agent, "reason": f"Passing the work to {label}."}, ROLES[index - 1][0])
            await emit("agent_state", {"status": "active", "current_action": role}, agent)
            context = "\n\n".join(f"{name}: {text}" for name, text in outputs.items())
            prompt = f"Original task:\n{task}\n\nPrior team work:\n{context or '(none yet)'}\n\nYour responsibility:\n{role}\nReturn your work for the next specialist."
            text = await ask(client, base, token, model, f"You are {label}. {role} Be concrete and concise.", prompt)
            outputs[agent] = text
            await emit("message", {"content": text, "phase": index + 1}, agent)
            await emit("agent_state", {"status": "waiting", "current_action": "Waiting for next handoff"}, agent)
        if cancelled():
            return "Run stopped before synthesis."
        await emit("agent_state", {"status": "active", "current_action": "Synthesizing overall result"}, "project_lead")
        result = await ask(client, base, token, model, "You are the project lead. Synthesize a final answer from the team.", f"Task:\n{task}\n\nTeam reports:\n{json.dumps(outputs, indent=2)}\n\nGive the overall result, decisions, caveats, and next actions.")
        await emit("result", {"content": result, "reports": outputs}, "project_lead")
        return result
