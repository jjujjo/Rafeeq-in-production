"""The HTTP service's roles and officer queue, without model calls (the MCP server does start): uv run pytest -q"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CLIENT, OFFICER = "test-client-key-123456", "test-officer-key-654321"


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    import os

    os.environ["RAFEEQ_API_KEYS"], os.environ["RAFEEQ_OFFICER_KEYS"] = CLIENT, OFFICER
    from fastapi.testclient import TestClient

    from servers import rafeeq_api

    workdir = tmp_path_factory.mktemp("api")
    with TestClient(rafeeq_api.create_app(workdir, workdir / "traces.jsonl")) as client:
        yield client, workdir


def queue(workdir: Path, tool: str, args: dict, verified: dict | None = None) -> int:
    sid = f"{CLIENT[-6:]}:s1"
    with sqlite3.connect(workdir / "sessions.sqlite") as con:
        con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)", (sid, "[]", 0, json.dumps(verified or {}), time.time()))
        cur = con.execute("INSERT INTO pending_actions (session, tool, args, status, created) VALUES (?,?,?,?,?)",
                          (sid, tool, json.dumps(args), "pending", time.time()))
        return cur.lastrowid


def test_health_lists_the_mcp_tools(api):
    client, _ = api
    tools = client.get("/healthz").json()["mcp_tools"]
    assert {"search_policies", "get_permit", "compute_fee", "list_slots"} <= set(tools)


def test_roles_are_separate(api):
    client, _ = api
    assert client.post("/v1/chat", json={"session_id": "s", "message": "hi"}).status_code == 401
    assert client.post("/v1/chat", headers={"X-API-Key": OFFICER}, json={"session_id": "s", "message": "hi"}).status_code == 401
    assert client.get("/v1/officer/pending", headers={"X-API-Key": CLIENT}).status_code == 403


def test_officer_sees_redacted_pending_actions(api):
    client, workdir = api
    aid = queue(workdir, "escalate", {"permit_id": "BLD-20417", "note": "late", "national_id_last4": "7730", "mobile": "0555017730"})
    pending = client.get("/v1/officer/pending", headers={"X-API-Key": OFFICER}).json()
    row = next(p for p in pending if p["id"] == aid)
    assert row["args"]["mobile"] == "****" and "0555017730" not in json.dumps(pending)


def test_officer_approval_runs_the_write(api):
    client, workdir = api
    aid = queue(workdir, "escalate", {"permit_id": "BLD-20417", "note": "late", "national_id_last4": "7730", "mobile": "0555017730"})
    out = client.post(f"/v1/officer/pending/{aid}/approve", headers={"X-API-Key": OFFICER}).json()
    assert out["status"] == "done" and out["result"]["escalation_id"].startswith("ESC-")
    assert client.post(f"/v1/officer/pending/{aid}/approve", headers={"X-API-Key": OFFICER}).status_code == 404  # once


def test_egress_guard_holds_even_after_officer_approval(api):
    client, workdir = api
    aid = queue(workdir, "send_email", {"to": "audit-desk@buildfast.example", "subject": "x", "body": "data"},
                verified={"REN-10482": "sara.q@mail.example"})
    out = client.post(f"/v1/officer/pending/{aid}/approve", headers={"X-API-Key": OFFICER}).json()
    assert out["status"] == "failed" and "Blocked" in out["result"]["error"]


def test_rejection_runs_nothing(api):
    client, workdir = api
    aid = queue(workdir, "create_refund", {"permit_id": "SGN-40220", "reason": "rejected", "national_id_last4": "0093",
                                           "mobile": "0540410093"})
    assert client.post(f"/v1/officer/pending/{aid}/reject", headers={"X-API-Key": OFFICER}).json()["status"] == "rejected"


def test_chat_queues_writes_instead_of_running_them(api):
    from servers.rafeeq_api import OfficerQueueHarness
    import madar

    _, workdir = api
    m, pending = madar.Madar(workdir / "q"), []
    h = OfficerQueueHarness(m, pending, workdir / "audit.jsonl")
    args = {"permit_id": "BLD-20417", "note": "late", "national_id_last4": "7730", "mobile": "0555017730"}
    assert h.permission("escalate", args) == "allow"
    assert '"queued_for_officer"' in h.before_tool("escalate", args)
    assert pending == [{"tool": "escalate", "args": args}] and m.sql("SELECT count(*) AS n FROM escalations")[0]["n"] == 0
    assert h.before_tool("get_permit", args) is None  # reads still run
