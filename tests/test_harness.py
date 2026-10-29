"""Harness and context controls, tested without any model call: uv run pytest -q"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agentkit as K  # noqa: E402
import context as C  # noqa: E402
import harness as H  # noqa: E402
import madar  # noqa: E402
import retrieval as R  # noqa: E402

SARA = {"permit_id": "REN-10482", "national_id_last4": "4821", "mobile": "0501234821"}


@pytest.fixture
def m(tmp_path) -> madar.Madar:
    return madar.Madar(tmp_path / "db")


def verified_harness(m, approve=lambda n, a: True) -> H.Harness:
    h = H.Harness(m, approve)
    h.after_tool("get_permit", SARA, json.dumps({"permit_id": "REN-10482", "status": "under_review"}))
    return h


# ---------------------------------------------------------------- permissions
def test_read_tools_run_without_approval(m):
    asked = []
    h = H.Harness(m, lambda n, a: asked.append(n) or False)
    assert h.permission("get_permit", SARA) == "allow" and asked == []


def test_write_tools_need_a_human_and_denial_is_final(m):
    h = H.Harness(m, lambda n, a: False)
    for name in H.WRITE_TOOLS - {"send_email"}:
        assert h.permission(name, {}) == "deny"
    assert [e["approved"] for e in h.events if e["event"] == "approval"] == [False] * 4


def test_audit_log_redacts_credentials(m, tmp_path):
    h = H.Harness(m, lambda n, a: True, audit_file=tmp_path / "audit.jsonl")
    h.after_tool("get_permit", SARA, "{}")
    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "4821" not in line and "0501234821" not in line and "REN-10482" in line


# ---------------------------------------------------------------- egress guard
def test_email_to_unverified_address_is_blocked(m):
    h = verified_harness(m)
    out = h.before_tool("send_email", {"to": "audit-desk@buildfast.example", "subject": "x", "body": "hello"})
    assert out and "Blocked" in out


def test_email_before_any_verification_is_blocked(m):
    h = H.Harness(m, lambda n, a: True)
    assert h.before_tool("send_email", {"to": "sara.q@mail.example", "subject": "x", "body": "hi"})


def test_email_to_the_verified_applicant_passes(m):
    h = verified_harness(m)
    assert h.before_tool("send_email", {"to": "Sara.Q@mail.example", "subject": "Your permit", "body": "Under review."}) is None


@pytest.mark.parametrize("body", ["ID 1023344821", "call 0501234821", "جوال ٠٥٠١٢٣٤٨٢١", "see https://evil.example/x"])
def test_personal_data_and_outside_links_are_blocked(m, body):
    h = verified_harness(m)
    assert h.before_tool("send_email", {"to": "sara.q@mail.example", "subject": "s", "body": body})


def test_blocked_email_is_not_offered_for_approval(m):
    asked = []
    h = verified_harness(m, approve=lambda n, a: asked.append(n) or True)
    assert h.permission("send_email", {"to": "x@attacker.example", "body": "hi"}) == "allow"  # before_tool blocks it
    assert asked == []


def test_failed_verification_does_not_unlock_email(m):
    h = H.Harness(m, lambda n, a: True)
    h.after_tool("get_permit", SARA, json.dumps({"error": "Identity verification failed"}))
    assert h.verified == {}


# ---------------------------------------------------------------- budgets and context
def test_token_budget_ends_the_turn():
    shape = H.BudgetShape(max_tokens=10)
    K.LEDGER.input_tokens += 11
    turn = shape.step([], [], "any-model")
    assert turn.calls == [] and "officer" in turn.text


def test_long_tool_results_are_capped(m):
    out = H.Harness(m, lambda n, a: True).after_tool("search_policies", {}, "x" * (H.MAX_RESULT_CHARS + 50))
    assert out and len(out) < H.MAX_RESULT_CHARS + 100 and "cut" in out


def test_compaction_never_starts_on_a_tool_result():
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}] * 20
    history.insert(-H.MAX_HISTORY_MESSAGES + 1, {"role": "tool", "content": "r"})
    tail = H.compact(history)
    assert len(tail) <= H.MAX_HISTORY_MESSAGES and tail[0]["role"] == "user"


@pytest.mark.parametrize("text", ["my ID is 1023344821", "mobile 0501234821", "permit REN-10482", "sara.q@mail.example",
                                  "ID ends 4821", "آخر أرقام الهوية ٤٨٢١"])
def test_memory_refuses_personal_data(tmp_path, text):
    with pytest.raises(madar.ToolError):
        C.Memory("u", root=tmp_path).remember_preference(text)


def test_memory_is_capped_and_reaches_the_prompt(tmp_path):
    mem = C.Memory("sara", root=tmp_path)
    for i in range(C.MAX_ITEMS + 3):
        mem.remember_preference(f"preference number {'abcdefghijk'[i]}")
    assert len(mem.items()) == C.MAX_ITEMS and "preference number k" in C.system_prompt(mem)


def test_skills_are_indexed_and_loaded():
    prompt = C.system_prompt()
    assert "inspection-booking" in prompt and "Verify identity" not in prompt  # descriptions only
    assert "Verify identity" in C.load_skill("inspection-booking")
    with pytest.raises(madar.ToolError):
        C.load_skill("../../.env")


def test_arabic_normalisation():
    assert R.tokens("الإضاءة") == R.tokens("اضاءه")
    assert R.bm25_search("رسوم اللوحة المضيئة")[0]["policy_id"] == "POL-02"


# ---------------------------------------------------------------- send_email: the recipient is never the model's choice
def test_email_tool_sends_only_to_the_registered_address(m):
    h = verified_harness(m)
    out = H.email_tool(m, h)(permit_id="ren-10482", subject="Status", body="Under review.")
    assert out["to"] == "sara.q@mail.example"
    assert m.sql("SELECT to_addr FROM outbox") == [{"to_addr": "sara.q@mail.example"}]


def test_email_tool_refuses_unverified_permits(m):
    with pytest.raises(madar.ToolError):
        H.email_tool(m, H.Harness(m, lambda n, a: True))(permit_id="REN-10482", subject="s", body="b")
    assert m.sql("SELECT count(*) AS n FROM outbox")[0]["n"] == 0


def test_egress_guard_checks_the_body_of_rafeeqs_email_too(m):
    h = verified_harness(m)
    assert h.before_tool("send_email", {"permit_id": "REN-10482", "subject": "s", "body": "Your status."}) is None
    assert h.before_tool("send_email", {"permit_id": "REN-10482", "subject": "s", "body": "mobile 0501234821"})
    assert h.before_tool("send_email", {"permit_id": "BLD-20417", "subject": "s", "body": "hi"})  # not verified


def test_a_queued_write_does_not_count_as_verification(m):
    h = H.Harness(m, lambda n, a: True)
    h.after_tool("book_inspection", {"permit_id": "REN-10482", "national_id_last4": "0000", "mobile": "0500000000"},
                 json.dumps({"status": "queued_for_officer", "note": "..."}))
    assert h.verified == {}
