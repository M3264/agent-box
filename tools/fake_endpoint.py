"""Two local OpenAI-compatible endpoints, for §8 item 8.

Verifying "each job uses its own base_url and model" needs two *working* profiles.
Pointing both at the live upstream would mean copying its key into a second place,
so instead each of these serves a distinct port and stamps its own identity into
every completion. What comes back through the API is then proof of which profile
the engine actually dialled.

    python tools/fake_endpoint.py <tag> <port> [--gate]

``--gate`` marks the planned phase ``requires_approval``, which is what §8 item 3
needs: a gate that appears on demand, without spending a real provider call or
waiting on a live model to decide the work is risky.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAN_MARKER = "Reply with JSON only"

PLAN = {
    "phases": [
        {
            "name": "Do the one thing",
            "owner": "coder",
            "acceptance": "it is done",
            "requires_approval": False,
        }
    ],
    "notes": "minimal",
}


def make_handler(tag: str, port: int, gate: bool):
    plan = json.loads(json.dumps(PLAN))
    plan["phases"][0]["requires_approval"] = gate

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            request = json.loads(self.rfile.read(length) or b"{}")
            prompt = request["messages"][-1]["content"]
            model = request.get("model", "?")

            if PLAN_MARKER in prompt:
                text = json.dumps(plan)
            else:
                # The identity stamp: endpoint tag, port, and the model the engine
                # asked this endpoint for.
                text = f"served-by={tag} port={port} model={model}"

            payload = {
                "choices": [{"message": {"content": text}}],
                "model": model,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    return Handler


if __name__ == "__main__":
    tag, port = sys.argv[1], int(sys.argv[2])
    gate = "--gate" in sys.argv[3:]
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(tag, port, gate)).serve_forever()
