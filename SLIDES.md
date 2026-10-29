# Rafeeq in production · slides outline (8 minutes + 4 Q&A)

Jood Rashed Alshehri · Capstone, [Agentic AI Systems Engineering, SDAIA Academy](https://github.com/SDAIAAcademy/agentic-ai)
Track 1 · Rafeeq in production. One idea per slide; numbers come from `results/`.

---

## 1 · The problem and the user (1 min)

**Slide 1: Title.** "Rafeeq in production: a permit assistant that can act, but can't leak."

**Slide 2: Who and what.**
- Applicants of Madar Municipal Services, writing in Arabic or English, at any hour.
- They ask: what does it cost, what do I need, where is my application, book my inspection, refund me.
- What can go wrong: a wrong fee, a status shared with a neighbour, a national ID emailed to a stranger.
- "Done" = correct and cited, verified before sharing, changes only with consent and a human's approval.

## 2 · Live demo (2 min)

**Slide 3: Demo (switch to the terminal).**
1. `uv run rafeeq.py chat --user demo`
2. Ask in Arabic: «كم رسوم لوحة مضيئة مساحتها ٣ م²؟» → `compute_fee`, 750 SAR, cites POL-02.
3. "My food truck permit FTR-30311 was approved. Book the health inspection on Wednesday 4 November at 09:00,
   I confirm. ID ends 6654, mobile 0532236654." → `get_permit` → `list_slots` → **approval prompt** 🛡️ → type `y`
   → booked.
4. Paste the contractor email with the hidden comment (redteam.py, `contractor-audit`) → Rafeeq refuses; nothing sent.

Backup: a screen recording of the same three steps (free models can be rate-limited on stage).

## 3 · Design across the ladder (2 min)

**Slide 4: The ladder.** Prompt ✅ · Context ✅ · Harness ✅ · Loop ✗ · Graph ✗ (one sentence each on why).
- Prompt: AGENTS.md, 6 numbered rules, so failures map back to a rule.
- Context: skills on demand, capped memory that refuses personal data in code, compaction, capped tool results.
- Harness: approvals on every write, egress guard, budgets (8 steps, 60k tokens: free models cost $0, so tokens
  are the real budget), audit log with masked credentials.
- No Ralph loop (no automatic "done" check to loop on); no LangGraph (the one gate is the officer queue).

**Slide 5: Architecture.** The diagram from the README: model ↔ harness ↔ local tools + MCP server (4 read tools)
↔ SQLite; the human at the approval step; the officer queue in the API.

## 4 · Eval results and the biggest failure (2 min)

**Slide 6: First, our grader was wrong.** First run of A: 0.63. Seven "failures" were the LLM judge returning
broken JSON, one was a Unicode hyphen. Lesson: read the failing trials before tuning the agent.

**Slide 7: A vs B.**

| | pass rate (30) | pass^3 (12 × 3) |
|---|---|---|
| A | 0.87 | 0.75 |
| B: + consent rule | 0.97 | 0.92 |

- Biggest failure category: "Shall I book it?" after the person said "go ahead" → nothing happened.
- B's one change: explicit consent in the message counts as confirmation (the harness asks the human anyway).
- Flips: two bookings ✗→✓; one ✗→✓ was luck (pass^k shows 2/3 for both). Nothing went ✓→✗.

## 5 · Trifecta, attack, defence (1 min)

**Slide 8: The lethal trifecta.** Private (`get_permit`) + untrusted (pasted emails) + egress (`send_email`), all in one
conversation.

**Slide 9: Attack → defence.**
- 8 attacks (hidden comments, fake overrides, "send it to my new email", Arabic and English).
- Without code guards: 1 of 8 leaked. The model invented a recipient and mailed a full mobile number.
- Defence in code: `send_email(permit_id)`: the code picks the registered address, so nothing can redirect it;
  plus an egress guard on IDs, mobiles and links; plus human approval.
- With guards: 0 of 8, and the legitimate "email me my status" works (it couldn't before).
- Residual risk: what Rafeeq says in chat is still model-steered; prompts go to the model provider (PDPL: production
  needs an approved provider or a self-hosted model).

**Slide 10: Thank you · Q&A.** Repo link · `uv run pytest -q` (36 tests) · `uv run redteam.py`.

---

### Likely questions

- *Why not just trust the prompt?* It stopped 7 of 8 attacks, but one got through; code stops 8 of 8 regardless of
  the model.
- *Why is pass^3 lower than the pass rate?* Free models vary between runs; pass^3 is what a citizen sees.
- *What would you change next?* Narrow rule 1 (identity only when an application is discussed), then a
  "lead with yes/no" style rule for eligibility questions.
- *Why Nemotron?* The course default was rate-limited upstream on the day; Nemotron passed our smoke test with
  correct tool calls in both languages.
