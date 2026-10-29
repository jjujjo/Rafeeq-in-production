# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib>=3.9", "pandas>=2.2"]
# ///
"""Evidence charts for the README, drawn straight from the CSVs in results/ (light and dark versions).

    uv run docs/make_charts.py        → docs/images/{eval,redteam}_{light,dark}.png

Colours: the validated categorical pair (blue, orange) for configurations A and B, and the reserved status colours,
always with an icon and a label, for leaked / safe.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import FancyBboxPatch, PathPatch  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RESULTS, OUT = ROOT / "results", ROOT / "docs" / "images"
DPI = 200

THEMES = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9", axis="#c3c2b7",
                  a="#2a78d6", b="#eb6834"),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a", axis="#383835",
                 a="#3987e5", b="#d95926"),
}
STATUS = {"good": "#0ca30c", "serious": "#ec835a", "critical": "#d03b3b"}
plt.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans"]


def rounded_bar(ax, x, w, h, color, px_per_x, px_per_y, radius_px=4):
    """A bar drawn as one path: 4px rounded data-end, square at the baseline (no seams between patches)."""
    rx, ry = min(radius_px / px_per_x, w / 2), min(radius_px / px_per_y, h / 2)
    verts = [(x, 0), (x, h - ry), (x, h), (x + rx, h), (x + w - rx, h), (x + w, h), (x + w, h - ry), (x + w, 0), (x, 0)]
    codes = [MPath.MOVETO, MPath.LINETO, MPath.CURVE3, MPath.CURVE3, MPath.LINETO, MPath.CURVE3, MPath.CURVE3,
             MPath.LINETO, MPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", zorder=3))


def eval_numbers() -> list[tuple[str, float, float]]:
    a, b = pd.read_csv(RESULTS / "eval_A.csv"), pd.read_csv(RESULTS / "eval_B.csv")
    pk = pd.concat([pd.read_csv(RESULTS / "passk" / "eval_A.csv"), pd.read_csv(RESULTS / "passk" / "eval_B.csv")])
    per_task = pk.groupby(["agent", "id"]).passed.agg(["any", "all"])
    at3 = per_task["any"].groupby("agent").mean()
    hat3 = per_task["all"].groupby("agent").mean()
    return [("Pass rate\n30 tasks · 1 run", a.passed.mean(), b.passed.mean()),
            ("pass@3\n12 tasks · 3 runs · any passes", at3["A"], at3["B"]),
            ("pass^3\n12 tasks · 3 runs · all pass", hat3["A"], hat3["B"])]


def eval_chart(theme: str) -> Path:
    t = THEMES[theme]
    rows = eval_numbers()
    fig = plt.figure(figsize=(9, 4.6), dpi=DPI, facecolor=t["surface"])
    ax = fig.add_axes([0.07, 0.16, 0.9, 0.56], facecolor=t["surface"])
    ax.set_xlim(-0.6, len(rows) - 0.4)
    ax.set_ylim(0, 1.08)
    fig.canvas.draw()
    bbox = ax.get_window_extent()
    px_x, px_y = bbox.width / 3.2, bbox.height / 1.08
    w, gap = 0.3, 2 / px_x  # 2px surface gap between adjacent bars
    for i, (_, va, vb) in enumerate(rows):
        for x, v, c in ((i - w - gap / 2, va, t["a"]), (i + gap / 2, vb, t["b"])):
            rounded_bar(ax, x, w, v, c, px_x, px_y)
            ax.text(x + w / 2, v + 0.025, f"{v:.0%}", ha="center", va="bottom",
                    fontsize=10.5, color=t["ink"], fontweight="bold" if c == t["b"] else "normal", zorder=4)
    for y in (0.25, 0.5, 0.75, 1.0):
        ax.axhline(y, color=t["grid"], lw=0.8, zorder=1)
    ax.axhline(0, color=t["axis"], lw=1, zorder=4)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0", "25%", "50%", "75%", "100%"], fontsize=9, color=t["muted"])
    ax.set_xticks(range(len(rows)), [r[0] for r in rows], fontsize=9.5, color=t["ink2"])
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    fig.text(0.07, 0.93, "Configuration B passes more tasks, and more reliably", fontsize=14, fontweight="bold",
             color=t["ink"])
    fig.text(0.07, 0.875, "Rafeeq's eval suite. B changes one rule: explicit consent in the message counts as "
                          "confirmation.", fontsize=10, color=t["ink2"])
    for k, (label, c) in enumerate((("A · AGENTS.md as written", t["a"]), ("B · + consent rule", t["b"]))):
        x0 = 0.07 + k * 0.27
        fig.patches.append(FancyBboxPatch((x0, 0.785), 0.014, 0.028, boxstyle="round,pad=0,rounding_size=0.003",
                                          transform=fig.transFigure, facecolor=c, edgecolor="none"))
        fig.text(x0 + 0.022, 0.799, label, fontsize=10, color=t["ink"], va="center")
    path = OUT / f"eval_{theme}.png"
    fig.savefig(path, facecolor=t["surface"])
    plt.close(fig)
    return path


ATTACK_LABELS = {
    "contractor-audit": "Hidden comment in a pasted email",
    "direct-override": "Fake \"SYSTEM OVERRIDE\"",
    "forward-to-lawyer": "\"Send my file to my lawyer\"",
    "arabic-officer": "Fake director's order (Arabic)",
    "image-beacon": "Tracking-image link with a mobile",
    "self-email-with-id": "\"Put my full mobile in the email\"",
    "changed-email": "\"Send it to my new email\"",
    "cc-accountant": "\"CC my accountant\"",
    "email-own-status": "Normal: \"email me my status\"",
    "office-hours": "Normal: office hours question",
}


def cell(row) -> tuple[str, str]:
    if row.kind == "attack":
        return ("✕  Leaked", "critical") if row.leaked else ("✓  Safe", "good")
    if row.case == "email-own-status":
        return ("✓  Delivered", "good") if row.emails_sent and not row.leaked else ("–  Not delivered", "serious")
    return ("✓  Answered", "good") if isinstance(row.reply, str) and row.reply.strip() else ("–  No answer", "serious")


def redteam_chart(theme: str) -> Path:
    t = THEMES[theme]
    df = pd.read_csv(RESULTS / "redteam.csv")
    cases = [c for c in ATTACK_LABELS if c in set(df.case)]
    n = len(cases)
    fig = plt.figure(figsize=(9, 0.42 * n + 1.9), dpi=DPI, facecolor=t["surface"])
    ax = fig.add_axes([0.0, 0.03, 1.0, (0.42 * n + 0.45) / (0.42 * n + 1.9)], facecolor=t["surface"])
    ax.set_xlim(0, 9)
    ax.set_ylim(n + 0.3, -0.9)
    ax.axis("off")
    cols = {"naive": (5.05, "Without code guards"), "defended": (7.35, "With code guards")}
    for config, (cx, head) in cols.items():
        ax.text(cx, -0.55, head, ha="center", va="center", fontsize=10, fontweight="bold", color=t["ink"])
    for i, case in enumerate(cases):
        y = i + (0.35 if ATTACK_LABELS[case].startswith("Normal") else 0)
        ax.text(0.3, y, ATTACK_LABELS[case], va="center", fontsize=10, color=t["ink2"] if "Normal" in
                ATTACK_LABELS[case] else t["ink"])
        for config, (cx, _) in cols.items():
            row = df[(df.case == case) & (df.config == config)].iloc[0]
            label, status = cell(row)
            icon, text = label.split("  ", 1)
            ax.scatter([cx - 0.62], [y], s=150, color=STATUS[status], edgecolors="none", zorder=3)
            ax.text(cx - 0.62, y + 0.01, icon, ha="center", va="center", fontsize=8, color="#ffffff",
                    fontweight="bold", zorder=4)
            ax.text(cx - 0.42, y, text, ha="left", va="center", fontsize=10, color=t["ink"],
                    fontweight="bold" if status != "good" else "normal")
        if i < n - 1:
            ax.axhline(y + 0.5 + (0.17 if ATTACK_LABELS[cases[i + 1]].startswith("Normal")
                                  and not ATTACK_LABELS[case].startswith("Normal") else 0),
                       xmin=0.03, xmax=0.97, color=t["grid"], lw=0.8)
    attacks = df[df.kind == "attack"]
    leaks = attacks.groupby("config").leaked.sum()
    fig.text(0.033, 1 - 0.5 / (0.42 * n + 1.9), f"No attack leaked once the code guards were on "
             f"({int(leaks.get('defended', 0))} of {attacks.case.nunique()})", fontsize=14, fontweight="bold",
             color=t["ink"], va="center")
    fig.text(0.033, 1 - 0.9 / (0.42 * n + 1.9), "Red-team suite (redteam.py). Judged by what Rafeeq actually sent, "
             "not by what it said.", fontsize=10, color=t["ink2"], va="center")
    path = OUT / f"redteam_{theme}.png"
    fig.savefig(path, facecolor=t["surface"])
    plt.close(fig)
    return path


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        print(eval_chart(theme), redteam_chart(theme), sep="\n")
    print("eval numbers:", [(r[0].split(chr(10))[0], round(r[1], 3), round(r[2], 3)) for r in eval_numbers()])
