# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "pandas>=2.2", "mcp>=2.3", "bm25s>=0.3"]
# ///
"""Rafeeq in production: Madar's bilingual permit assistant with MCP tools, skills, memory, approvals and evals.

    uv run rafeeq.py ask "What is the fee for an illuminated 3 m² sign?"
    uv run rafeeq.py chat --user sara                    # a conversation; preferences are remembered per user
    uv run rafeeq.py eval data/eval/tasks.jsonl --config A B
    uv run redteam.py                                    # the attack suite, naive vs. defended

The parts (each maps to a capstone requirement):
1. tools: 4 read tools over MCP (servers/madar_mcp.py, stdio) + 7 local tools (5 write tools, load_skill, memory);
2. context: AGENTS.md as the system prompt, skills loaded on demand, a capped preference memory (context.py),
   history compaction and a cap on tool results (harness.py);
3. harness: write tools need a human's approval, an egress guard on email, step/token/cost budgets, an audit log;
4. evals: evals.py over tasks.jsonl on two configurations, one CSV row per trial for error analysis.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp import StdioServerParameters  # noqa: E402

import agentkit as K  # noqa: E402
import context as C  # noqa: E402
import evals  # noqa: E402
import harness as H  # noqa: E402
import madar  # noqa: E402

OUT = K.lab_dir("rafeeq")
MCP_TOOLS = ("search_policies", "get_permit", "compute_fee", "list_slots")

# Two configurations for the eval (requirement 4): B changes ONE thing from A, chosen from A's error analysis
# (the biggest failure category: the model asked "Shall I book it?" after the person had already said "go ahead").
CONSENT_RULE = """## Rule 3, clarified: explicit consent
When the person's own message already names the exact change and tells you to go ahead (for example "book it,
I confirm", «نعم، نفّذ»), that IS the confirmation: verify, pick the slot or amount that matches what they asked
for, and call the write tool in the same turn. Do not ask "Shall I…?" again. The harness still asks a human to
approve every change, so asking twice only stalls the request. If their wish is ambiguous (two matching slots, an
unclear reason), ask one short question instead."""

CONFIGS = {
    "A": {"model": None, "retrieval": "keyword", "extra": ""},  # AGENTS.md as written
    "B": {"model": None, "retrieval": "keyword", "extra": CONSENT_RULE},  # + the consent clarification
}


def mcp_server(workdir: Path | None = None, retrieval: str = "keyword") -> StdioServerParameters:
    """Madar's read tools over MCP. With `workdir`, the server shares the agent's copy of the database."""
    args = ["run", "--quiet", str(ROOT / "servers" / "madar_mcp.py"), "--retrieval", retrieval]
    return StdioServerParameters(command="uv", args=args + (["--workdir", str(workdir)] if workdir else []))


def local_tools(m: madar.Madar, memory: C.Memory, h: H.Harness, raw_email: bool = False) -> list:
    """Tools that live in this process: the write tools act on this conversation's database copy.
    `raw_email` swaps in the kit's send_email(to, ...) for the naive red-team configuration."""
    return [m.book_inspection, m.reschedule_inspection, m.create_refund, m.escalate,
            m.send_email if raw_email else H.email_tool(m, h), C.load_skill, memory.remember_preference]


def approve_cli(name: str, args: dict) -> bool:
    shown = json.dumps(H.redact(args), ensure_ascii=False)
    return input(f"\n🛡️  Rafeeq wants to run {name}({shown}). Approve? [y/N] ").strip().lower() == "y"


def make_agent(config: dict, mcp: K.MCPBridge, approve=approve_cli, memory_root: Path | None = None,
               audit_file: Path | None = None, guarded: bool = True):
    """An agent `(prompt, m, history=None, user="default", harness=None) -> K.Run` (the shape evals.run_suite expects).

    Pass the same `harness` for every turn of one conversation, so identity verified earlier still counts."""

    def agent(prompt: str, m: madar.Madar, history: list | None = None, user: str = "default",
              harness: H.Harness | None = None, **kw) -> K.Run:
        memory = C.Memory(user, root=memory_root or Path(m.workdir or OUT) / "memory")
        h = harness or H.Harness(m, approve, audit_file, guarded=guarded)
        tools = local_tools(m, memory, h, raw_email=not guarded) + [t for t in mcp.agent_tools() if t.__name__ in MCP_TOOLS]
        run = K.run_agent(prompt, tools, system=C.system_prompt(memory, config["extra"]), model=config["model"],
                          shape=H.BudgetShape(), history=history, max_steps=H.MAX_STEPS, max_cost=H.MAX_COST,
                          permission=h.permission if guarded else None, before_tool=h.before_tool,
                          after_tool=h.after_tool, verbose=kw.pop("verbose", False), **kw)
        run.harness = h
        return run

    return agent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="one question")
    a.add_argument("question")
    a.add_argument("--user", default="default")
    a.add_argument("--config", default="B", choices=CONFIGS)
    c = sub.add_parser("chat", help="a conversation in the terminal")
    c.add_argument("--user", default="default")
    c.add_argument("--config", default="B", choices=CONFIGS)
    e = sub.add_parser("eval", help="run the eval suite on one or more configurations")
    e.add_argument("tasks", type=Path, nargs="?", default=ROOT / "data" / "eval" / "tasks.jsonl")
    e.add_argument("--config", nargs="+", default=["A", "B"], choices=CONFIGS)
    e.add_argument("--trials", type=int, default=1)
    e.add_argument("--ids", nargs="*", help="only these task IDs")
    e.add_argument("--out", type=Path, default=ROOT / "results")
    args = ap.parse_args()

    if args.cmd in ("ask", "chat"):
        cfg, workdir = CONFIGS[args.config], OUT / "db"
        with K.MCPBridge(mcp_server(workdir, cfg["retrieval"])) as mcp:
            m = madar.Madar(workdir)  # after the server started: both now share this fresh copy
            agent = make_agent(cfg, mcp, memory_root=OUT / "memory", audit_file=OUT / "audit.jsonl")
            h = H.Harness(m, approve_cli, OUT / "audit.jsonl")
            if args.cmd == "ask":
                run = agent(args.question, m, user=args.user, harness=h, verbose=True)
                print("\n" + (run.final or f"(stopped: {run.stopped})"))
            else:
                history: list = []
                print("Rafeeq · type your message (empty line to quit).")
                while (msg := input("\n🧑 ").strip()):
                    run = agent(msg, m, history=H.compact(history), user=args.user, harness=h)
                    print("🤖", run.final or f"(stopped: {run.stopped})")
                    history = run.state[1:]
        print(f"\n[{K.LEDGER}]")
        return 0

    tasks = evals.load_tasks(args.tasks, ids=args.ids)
    consent = lambda name, a: True  # noqa: E731 - unattended: task prompts give consent; the guards still apply
    import pandas as pd

    args.out.mkdir(parents=True, exist_ok=True)
    frames = []
    for name in args.config:
        cfg = CONFIGS[name]
        with K.MCPBridge(mcp_server(retrieval=cfg["retrieval"])) as mcp:
            df = evals.run_suite(make_agent(cfg, mcp, approve=consent), tasks, trials=args.trials, name=name)
        df.to_csv(args.out / f"eval_{name}.csv", index=False, encoding="utf-8")
        frames.append(df)
    overall, per_task = evals.report(pd.concat(frames))
    print("\n", overall, f"\n\nper-trial rows written to {args.out}/eval_<config>.csv · {K.LEDGER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
