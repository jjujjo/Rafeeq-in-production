# /// script
# requires-python = ">=3.12"
# dependencies = ["fastapi>=0.150", "uvicorn>=0.40", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1",
#                 "opentelemetry-sdk>=1.45", "mcp>=2.3"]
# ///
"""Rafeeq as an HTTP service (after Lab 4.4), with the capstone's spec, MCP tools, harness and an officer queue.

    uv run servers/rafeeq_api.py --port 8793          # keys come from .env (RAFEEQ_API_KEYS, RAFEEQ_OFFICER_KEYS)

    curl -s localhost:8793/v1/chat -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
         -d '{"session_id": "s1", "message": "What are your office hours?"}'
    curl -s localhost:8793/v1/officer/pending -H "X-API-Key: $OFFICER_KEY"
    curl -s -X POST localhost:8793/v1/officer/pending/1/approve -H "X-API-Key: $OFFICER_KEY"

What it adds around the agent:
- API keys: citizens' clients (RAFEEQ_API_KEYS) and officers (RAFEEQ_OFFICER_KEYS) are separate roles;
- sessions in SQLite: history, tokens spent, and which applicants this session verified (for the egress guard);
- budgets: RAFEEQ_SESSION_TOKENS per session (HTTP 429 after that), plus the harness's step/token budgets per turn;
- human approval: write tools are never run by the chat endpoint. They are queued (pending_actions); an officer
  approves or rejects each one, and approval re-checks the egress guard before anything runs;
- fallbacks: OpenRouter's `models` list (agentkit.FALLBACKS), then one retry on a second model;
- traces: OpenTelemetry spans with GenAI attributes (invoke_agent → chat / execute_tool) as JSON lines in
  artifacts/traces/rafeeq_api.jsonl (swap in an OTLP exporter for Jaeger, Phoenix, Langfuse, ...).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import agentkit as K  # noqa: E402
import context as C  # noqa: E402
import harness as H  # noqa: E402
import madar  # noqa: E402
import rafeeq  # noqa: E402


def _keys(var: str) -> set[str]:
    return {k.strip() for k in os.getenv(var, "").split(",") if k.strip()}


SESSION_TOKENS = int(os.getenv("RAFEEQ_SESSION_TOKENS", "40000"))
SECOND_MODEL = os.getenv("RAFEEQ_SECOND_MODEL", K.FALLBACKS[0] if K.FALLBACKS else K.MODEL)
CONFIG = rafeeq.CONFIGS[os.getenv("RAFEEQ_CONFIG", "B")]


# ---------------------------------------------------------------- traces
class JsonlExporter(SpanExporter):
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans) -> SpanExportResult:
        with self.path.open("a", encoding="utf-8") as f:
            for s in spans:
                f.write(json.dumps({"name": s.name, "trace_id": f"{s.context.trace_id:032x}",
                                    "span_id": f"{s.context.span_id:016x}",
                                    "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                                    "ms": round((s.end_time - s.start_time) / 1e6, 1),
                                    "attributes": dict(s.attributes or {})}, ensure_ascii=False) + "\n")
        return SpanExportResult.SUCCESS


def tracer_for(path: Path):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(path)))
    return provider.get_tracer("rafeeq")


class TracedShape(H.BudgetShape):
    """Chat Completions with the harness's token budget and one `chat {model}` span per model call."""

    def __init__(self, tracer, **kw):
        super().__init__(**kw)
        self.tracer = tracer

    def step(self, state, tools, model, use_cache=None, **kw):
        with self.tracer.start_as_current_span(f"chat {model}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model", model)
            turn = super().step(state, tools, model, use_cache=use_cache, **kw)
            u = turn.usage.model_dump() if hasattr(turn.usage, "model_dump") else (turn.usage or {})
            span.set_attribute("gen_ai.usage.input_tokens", u.get("prompt_tokens") or 0)
            span.set_attribute("gen_ai.usage.output_tokens", u.get("completion_tokens") or 0)
            span.set_attribute("gen_ai.response.finish_reasons", ["tool_calls" if turn.calls else "stop"])
            return turn


def traced(tracer, fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with tracer.start_as_current_span(f"execute_tool {fn.__name__}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", fn.__name__)
            return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- the human gate
class OfficerQueueHarness(H.Harness):
    """The chat endpoint never runs a write: it queues it for an officer and tells the model so, plainly (a "denied"
    result made the model think the request had failed). The egress guard still runs first."""

    def __init__(self, m: madar.Madar, pending: list, audit_file: Path):
        super().__init__(m, lambda n, a: False, audit_file)
        self.pending = pending

    def permission(self, name: str, args: dict) -> str:
        return "allow"  # before_tool decides: blocked by policy, queued, or (read tools) run

    def before_tool(self, name: str, args: dict) -> str | None:
        out = super().before_tool(name, args)
        if out is not None or name not in H.WRITE_TOOLS:
            return out
        self.pending.append({"tool": name, "args": args})
        self.audit("queued_for_officer", tool=name, args=H.redact(args))
        return json.dumps({"status": "queued_for_officer", "note": "Not done yet: a licensing officer reviews and "
                           "confirms this change. Tell the person it has been sent for approval and what will happen."})


# ---------------------------------------------------------------- the service
class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    message: str = Field(min_length=1, max_length=4000)


class ChatOut(BaseModel):
    session_id: str
    reply: str
    tools: list[str]
    pending_actions: list[dict]
    tokens: dict
    model_steps: int


def create_app(workdir: Path | None = None, trace_file: Path | None = None) -> FastAPI:
    api_keys, officer_keys = _keys("RAFEEQ_API_KEYS"), _keys("RAFEEQ_OFFICER_KEYS")
    if not api_keys or not officer_keys:
        raise SystemExit("Set RAFEEQ_API_KEYS and RAFEEQ_OFFICER_KEYS in .env (see .env.example).")
    if api_keys & officer_keys:
        raise SystemExit("An officer key must not also be a client key.")
    workdir = workdir or K.lab_dir("rafeeq_api")
    db = workdir / "sessions.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, history TEXT, tokens INTEGER, "
                    "verified TEXT, updated REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS pending_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, "
                    "tool TEXT, args TEXT, status TEXT, result TEXT, created REAL, decided REAL)")
    tracer = tracer_for(trace_file or K.ARTIFACTS / "traces" / "rafeeq_api.jsonl")
    lock = threading.Lock()  # one agent run at a time: K.LEDGER is process-wide (fine for a capstone, not for scale)
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with K.MCPBridge(rafeeq.mcp_server(workdir / "madar", CONFIG["retrieval"])) as mcp:
            state["mcp"], state["m"] = mcp, madar.Madar(workdir / "madar", actor="rafeeq-api")
            yield

    app = FastAPI(title="Rafeeq API", version="1.1.0", lifespan=lifespan)

    def client_key(x_api_key: str = Header(default="")) -> str:
        if x_api_key not in api_keys:
            raise HTTPException(401, "Missing or invalid X-API-Key")
        return x_api_key

    def officer_key(x_api_key: str = Header(default="")) -> str:
        if x_api_key not in officer_keys:
            raise HTTPException(403, "Officer key required")
        return x_api_key

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "model": CONFIG["model"] or K.MODEL, "mcp_tools": [t.name for t in state["mcp"].list_tools()]}

    @app.post("/v1/chat", response_model=ChatOut)
    def chat(req: ChatIn, key: str = Depends(client_key)) -> ChatOut:
        sid = f"{key[-6:]}:{req.session_id}"  # sessions are scoped to the client that created them
        with sqlite3.connect(db) as con:
            row = con.execute("SELECT history, tokens, verified FROM sessions WHERE id = ?", (sid,)).fetchone()
        history, spent, verified = (json.loads(row[0]), row[1], json.loads(row[2] or "{}")) if row else ([], 0, {})
        if spent >= SESSION_TOKENS:
            raise HTTPException(429, f"Session budget of {SESSION_TOKENS} tokens used up. Start a new session.")
        m, pending = state["m"], []

        h = OfficerQueueHarness(m, pending, K.ARTIFACTS / "rafeeq_api" / "audit.jsonl")
        h.verified = verified
        memory = C.Memory(key[-6:], root=workdir / "memory")
        tools = [traced(tracer, t) for t in rafeeq.local_tools(m, memory, h)
                 + [t for t in state["mcp"].agent_tools() if t.__name__ in rafeeq.MCP_TOOLS]]
        with lock, tracer.start_as_current_span("invoke_agent Rafeeq") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "Rafeeq")
            span.set_attribute("gen_ai.conversation.id", req.session_id)
            before = (K.LEDGER.input_tokens, K.LEDGER.output_tokens)
            kw = dict(system=C.system_prompt(memory, CONFIG["extra"]), history=H.compact(history), max_steps=H.MAX_STEPS,
                      max_cost=H.MAX_COST, permission=h.permission, before_tool=h.before_tool,
                      after_tool=h.after_tool, verbose=False)
            try:
                run = K.run_agent(req.message, tools, model=CONFIG["model"], shape=TracedShape(tracer), **kw)
            except Exception as e:  # noqa: BLE001 - every model in the routing list failed: try a second model once
                span.set_attribute("error.type", type(e).__name__)
                pending.clear()
                run = K.run_agent(req.message, tools, model=SECOND_MODEL, shape=TracedShape(tracer), **kw)
            used = {"input": K.LEDGER.input_tokens - before[0], "output": K.LEDGER.output_tokens - before[1]}
        reply = run.final or "I couldn't finish this request. An officer will follow up."
        if pending:
            reply += "\n\n(Sent to an officer for approval: " + ", ".join(p["tool"] for p in pending) + ".)"
        with sqlite3.connect(db) as con:
            con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
                        (sid, json.dumps(run.state[1:], ensure_ascii=False, default=str),
                         spent + used["input"] + used["output"], json.dumps(h.verified), time.time()))
            for p in pending:
                cur = con.execute("INSERT INTO pending_actions (session, tool, args, status, created) VALUES (?,?,?,?,?)",
                                  (sid, p["tool"], json.dumps(p["args"], ensure_ascii=False), "pending", time.time()))
                p["id"] = cur.lastrowid
        return ChatOut(session_id=req.session_id, reply=reply, tools=[n for n, _ in run.tool_calls],
                       pending_actions=[{"id": p["id"], "tool": p["tool"], "args": H.redact(p["args"])} for p in pending],
                       tokens=used, model_steps=run.steps)

    @app.get("/v1/officer/pending")
    def list_pending(_: str = Depends(officer_key)) -> list[dict]:
        with sqlite3.connect(db) as con:
            rows = con.execute("SELECT id, session, tool, args, created FROM pending_actions WHERE status = 'pending' "
                               "ORDER BY id").fetchall()
        return [{"id": r[0], "session": r[1], "tool": r[2], "args": H.redact(json.loads(r[3])), "created": r[4]}
                for r in rows]

    @app.post("/v1/officer/pending/{action_id}/{decision}")
    def decide(action_id: int, decision: str, officer: str = Depends(officer_key)) -> dict:
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "Use /approve or /reject")
        with sqlite3.connect(db) as con:
            row = con.execute("SELECT p.tool, p.args, s.verified FROM pending_actions p LEFT JOIN sessions s "
                              "ON s.id = p.session WHERE p.id = ? AND p.status = 'pending'", (action_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"No pending action {action_id}")
        tool, args = row[0], json.loads(row[1])
        result: dict = {"status": "rejected"}
        if decision == "approve":
            m = state["m"]
            h = H.Harness(m, lambda n, a: True, K.ARTIFACTS / "rafeeq_api" / "audit.jsonl")
            h.verified = json.loads(row[2] or "{}")
            with lock:
                fn = H.email_tool(m, h) if tool == "send_email" else getattr(m, tool)
                out = h.before_tool(tool, args) or K.call_tool({tool: fn}, tool, args)
            h.audit("officer_decision", tool=tool, action_id=action_id, officer=officer[-4:], approved=True)
            result = {"status": "failed" if out.startswith('{"error"') else "done", "result": json.loads(out)}
        with sqlite3.connect(db) as con:
            con.execute("UPDATE pending_actions SET status = ?, result = ?, decided = ? WHERE id = ?",
                        (result["status"], json.dumps(result.get("result"), ensure_ascii=False), time.time(), action_id))
        return {"id": action_id, "tool": tool, **result}

    return app


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8793)
    args = ap.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
