"""Rafeeq's context engineering: the spec, skills loaded on demand, and a capped preference memory.

- spec: AGENTS.md is the system prompt. Only the skills' one-line descriptions are appended (progressive disclosure);
  the full procedure enters the context when the model calls `load_skill`.
- memory: one small JSON file per user. It holds preferences only, is capped (MAX_ITEMS, MAX_CHARS), is injected into
  the system prompt at the start of each conversation, and refuses personal data in code (PDPL data minimisation).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import madar

ROOT = Path(__file__).resolve().parent
SPEC_FILE = ROOT / "AGENTS.md"
SKILLS_DIR = ROOT / "data" / "skills"
MEMORY_DIR = ROOT / "artifacts" / "memory"

MAX_ITEMS, MAX_CHARS = 8, 160

# personal data that must never be written to memory (or leave the building, see harness.py)
NATIONAL_ID_RE = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")
MOBILE_RE = re.compile(r"(?:\+?966|00966|0)?5\d{8}(?!\d)")
PERMIT_RE = re.compile(r"\b(?:BLD|REN|SGN|FTR|EVT)-\d{5}\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LAST4_RE = re.compile(r"(?<!\d)\d{4}(?!\d)")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


# ---------------------------------------------------------------- skills
def skills() -> dict[str, dict]:
    """name → {description, path} for every data/skills/*/SKILL.md."""
    out = {}
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        head = path.read_text(encoding="utf-8").split("---")[1]
        meta = dict(line.split(": ", 1) for line in head.strip().splitlines() if ": " in line)
        out[meta.get("name", path.parent.name)] = {"description": meta.get("description", ""), "path": path}
    return out


def load_skill(name: str) -> str:
    """Read the step-by-step procedure for a multi-step task before doing it.

    Args:
        name: one of the skills listed in your instructions, e.g. "inspection-booking" or "refund-request".
    """
    found = skills()
    if name not in found:
        raise madar.ToolError(f"No skill {name!r}. Available: {', '.join(found)}.")
    return found[name]["path"].read_text(encoding="utf-8").split("---", 2)[2].strip()


# ---------------------------------------------------------------- memory
def has_personal_data(text: str) -> bool:
    t = text.translate(ARABIC_DIGITS)
    return any(r.search(t) for r in (NATIONAL_ID_RE, MOBILE_RE, PERMIT_RE, EMAIL_RE, LAST4_RE))


class Memory:
    """A capped list of preferences for one user, stored as JSON."""

    def __init__(self, user: str, root: Path = MEMORY_DIR):
        safe = re.sub(r"[^\w.-]+", "_", user)[:64] or "default"
        self.path = root / f"{safe}.json"

    def items(self) -> list[str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []

    def remember_preference(self, preference: str) -> dict:
        """Keep one durable preference of this person for future conversations (e.g. "prefers Arabic",
        "prefers morning inspections"). Never store IDs, mobile numbers, emails, permit numbers or case details.

        Args:
            preference: one short sentence.
        """
        text = " ".join(str(preference).split())
        if not text:
            raise madar.ToolError("Empty preference.")
        if len(text) > MAX_CHARS:
            raise madar.ToolError(f"Too long: keep a preference under {MAX_CHARS} characters.")
        if has_personal_data(text):
            raise madar.ToolError("Refused: memory may not hold national IDs, mobiles, emails, permit numbers or "
                                  "other case data (POL-08). Store only the preference itself.")
        items = [i for i in self.items() if i.lower() != text.lower()] + [text]
        items = items[-MAX_ITEMS:]  # the cap: the oldest preference is forgotten first
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"saved": text, "items": len(items), "cap": MAX_ITEMS}

    def as_context(self) -> str:
        items = self.items()
        if not items:
            return ""
        return "\n\n## What you remember about this person (preferences only)\n" + "\n".join(f"- {i}" for i in items)


# ---------------------------------------------------------------- the system prompt
def system_prompt(memory: Memory | None = None, extra: str = "") -> str:
    index = "\n".join(f"- `{n}`: {s['description']}" for n, s in skills().items())
    prompt = SPEC_FILE.read_text(encoding="utf-8").strip() + "\n\n## Skills (load with `load_skill`)\n" + index
    if extra:
        prompt += "\n\n" + extra.strip()
    return prompt + (memory.as_context() if memory else "")
