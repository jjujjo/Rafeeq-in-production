"""Rafeeq's harness: the controls that hold even when the model doesn't.

Layer by layer (each one is enforced in code, not in the prompt):
- permissions: read tools run; write tools ask a human first (`approve`), and a denial is final;
- egress guard: `send_email` only reaches the registered address of an applicant this conversation verified, with no
  national IDs, mobiles or outside links in the body (Lab 4.3);
- budgets: steps per turn (run_agent), tokens per turn (BudgetShape: free models cost $0, so tokens are the real
  budget), cost per turn, a cap on each tool result, and history compaction between turns;
- audit: every tool call, decision and block is appended to a JSON-lines log.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

import agentkit as K
import madar
from context import MOBILE_RE, NATIONAL_ID_RE, ARABIC_DIGITS

MAX_STEPS = 8  # model calls per user turn
MAX_TURN_TOKENS = 60_000  # input + output tokens per user turn
MAX_COST = 0.05  # USD per user turn (a backstop for paid models)
MAX_RESULT_CHARS = 3000  # per tool result
MAX_HISTORY_MESSAGES = 24  # kept between turns

WRITE_TOOLS = {"book_inspection", "reschedule_inspection", "create_refund", "escalate", "send_email"}
VERIFYING_TOOLS = {"get_permit", "book_inspection", "create_refund", "escalate"}  # take permit_id + credentials
LINK_RE = re.compile(r"https?://([^/\s)\]>\"']+)", re.IGNORECASE)
ALLOWED_LINK_HOSTS = ("madar.example",)


def blocked(reason: str) -> str:
    return json.dumps({"error": f"Blocked by Madar's policy: {reason}. Tell the person what you can do instead."},
                      ensure_ascii=False)


class Harness:
    """The controls for one conversation. `approve(name, args) -> bool` is the human in the loop."""

    def __init__(self, m: madar.Madar, approve: Callable[[str, dict], bool], audit_file: Path | None = None,
                 guarded: bool = True):
        self.m, self.approve, self.guarded = m, approve, guarded
        self.audit_file = audit_file
        self.verified: dict[str, str] = {}  # permit_id → registered applicant email, filled by successful verifications
        self.events: list[dict] = []

    # -- audit
    def audit(self, event: str, **detail) -> None:
        row = {"t": round(time.time(), 3), "event": event, **detail}
        self.events.append(row)
        if self.audit_file:
            self.audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    # -- run_agent hooks
    def permission(self, name: str, args: dict) -> str:
        if name not in WRITE_TOOLS:
            return "allow"
        if self.guarded and name == "send_email" and self.egress_problems(args):
            return "allow"  # before_tool blocks it: don't ask a human to approve what policy forbids anyway
        ok = bool(self.approve(name, args))
        self.audit("approval", tool=name, args=redact(args), approved=ok)
        return "allow" if ok else "deny"

    def before_tool(self, name: str, args: dict) -> str | None:
        if not self.guarded or name != "send_email":
            return None
        reasons = self.egress_problems(args)
        if reasons:
            self.audit("egress_blocked", tool=name, to=args.get("to") or args.get("permit_id"), reasons=reasons)
            return blocked("; ".join(reasons))
        return None

    def after_tool(self, name: str, args: dict, result: str) -> str | None:
        ok = not result.startswith('{"error"')
        self.audit("tool", tool=name, args=redact(args), ok=ok, chars=len(result))
        ran = ok and '"queued_for_officer"' not in result[:40]  # a queued write hasn't checked any credentials yet
        if ran and name in VERIFYING_TOOLS and args.get("permit_id"):
            self.remember_verified(str(args["permit_id"]))
        if len(result) > MAX_RESULT_CHARS:
            return result[:MAX_RESULT_CHARS] + f"\n[… {len(result) - MAX_RESULT_CHARS} characters cut; ask for less]"
        return None

    # -- egress
    def remember_verified(self, permit_id: str) -> None:
        rows = self.m.sql("SELECT a.email FROM permits p JOIN applicants a USING (applicant_id) WHERE p.permit_id = ?",
                          (permit_id.strip().upper(),))
        if rows:
            self.verified[permit_id.strip().upper()] = rows[0]["email"].lower()

    def egress_problems(self, args: dict) -> list[str]:
        if "permit_id" in args:  # Rafeeq's send_email: the recipient is looked up, never chosen by the model
            to = self.verified.get(str(args["permit_id"]).strip().upper(), "")
        else:  # the kit's send_email(to, ...), used by the naive red-team configuration
            to = str(args.get("to", "")).strip().lower()
        body = f"{args.get('subject', '')}\n{args.get('body', '')}"
        reasons = []
        if not to or to not in self.verified.values():
            reasons.append("email goes only to the registered address of an applicant verified in this conversation "
                           "(POL-10)")
        plain = body.translate(ARABIC_DIGITS)
        if NATIONAL_ID_RE.search(plain) or MOBILE_RE.search(plain):
            reasons.append("the message contains a national ID or mobile number")
        if any(not h.lower().endswith(ALLOWED_LINK_HOSTS) for h in LINK_RE.findall(body)):
            reasons.append("links may only point to madar.example")
        return reasons


def email_tool(m: madar.Madar, h: Harness):
    """Least privilege by construction: the model names a permit, the code picks the recipient. No argument lets an
    injected instruction redirect an email, whatever the model is persuaded to do."""

    def send_email(permit_id: str, subject: str, body: str) -> dict:
        """Email the applicant of a permit at the address registered on the application (POL-10). Works only for a
        permit whose applicant you verified with get_permit in this conversation; you cannot choose the recipient.
        Never include national IDs, mobile numbers or outside links.

        Args:
            permit_id: the verified permit, e.g. "REN-10482".
            subject: subject line.
            body: plain-text body.
        """
        to = h.verified.get(permit_id.strip().upper())
        if not to:
            raise madar.ToolError("Verify the applicant with get_permit first: email goes only to the address "
                                  "registered on a verified application (POL-10). You cannot send to other addresses.")
        return m.send_email(to, subject, body)

    return send_email


def redact(args: dict) -> dict:
    """Audit logs keep what was done, not the credentials used to do it."""
    return {k: ("****" if k in ("national_id_last4", "mobile") else v) for k, v in args.items()}


class BudgetShape(K.ChatShape):
    """Chat Completions with a token budget per user turn: once spent, the loop ends with a hand-over message."""

    def __init__(self, max_tokens: int = MAX_TURN_TOKENS, **kw):
        super().__init__(**kw)
        self.max_tokens = max_tokens
        self.baseline = K.LEDGER.input_tokens + K.LEDGER.output_tokens

    def spent(self) -> int:
        return K.LEDGER.input_tokens + K.LEDGER.output_tokens - self.baseline

    def step(self, state, tools, model, use_cache=None, **kw):
        if self.spent() > self.max_tokens:
            return K.Turn("I've reached the processing budget for this request, so I'm handing it to an officer, "
                          "who will follow up. / وصلت إلى حد المعالجة لهذا الطلب، وسيتابعه أحد الموظفين.", [], None, None)
        return super().step(state, tools, model, use_cache=use_cache, **kw)


def compact(history: list) -> list:
    """Keep the last messages between turns, never starting on a tool result (its call would be missing)."""
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    tail = history[-MAX_HISTORY_MESSAGES:]
    while tail and tail[0].get("role") != "user":
        tail = tail[1:]
    return tail
