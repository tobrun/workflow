#!/usr/bin/env python3
"""Render a spec.md into the review page an engineer reads before build.

Usage: python3 render-spec.py .dev/{plan-name}/spec.md [--open]
Writes spec.html beside the spec and prints its path. The page is a pure function
of spec.md - every number on it is counted from the file - so it can never claim
what the spec does not say. Only a ### Flow mermaid block loads anything remote.
"""

from __future__ import annotations

import html
import importlib.util
import re
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lint_spec", HERE / "lint-spec.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
CONFIDENCE = re.compile(r"Confidence:\s*(\d{1,3})%")
USER_CITED = re.compile(r"\(user\b|\buser \(\d{4}-")
ITEM = re.compile(r"^\s+([a-z])\.\s+(.*)$")
TAG = re.compile(r"\s*\[(?:open|impact:\s*\w+)\]")
IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}
IMPACT_PIPS = {"high": 3, "medium": 2, "low": 1}
LAYERS = lint.TAGS
ASK_BELOW = 50  # scope asks the user below this confidence


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


ACRONYMS = {"pr", "ci", "api", "ui", "ux", "e2e", "id", "url", "http", "sql", "json", "yaml", "toml", "io", "db", "cli",
            "sdk", "llm", "tla", "ast", "ssh", "vm", "jvm", "tls", "dns", "ttl", "s3", "os", "gpu", "cpu", "html", "css"}


# What a person reads for each verification kind; the slug stays in files and tables tools parse.
KIND_NAMES = {"static-analysis": "Static analysis", "ui-ux": "UI/UX", "threat-model": "Threat model"}


def kind_name(kind: str) -> str:
    return KIND_NAMES.get(kind, kind.capitalize())


def title(slug: str) -> str:
    """A decision's readable name: D-complexity-threshold -> Complexity threshold. People read this; files keep the slug."""
    words = slug.removeprefix("D-").split("-")
    words = [w.upper() if w in ACRONYMS else w for w in words]
    return (words[0][:1].upper() + words[0][1:] + " " + " ".join(words[1:])).strip()


# ---------------------------------------------------------------------------
# Reading spec.md
# ---------------------------------------------------------------------------


def split_alternative(mark: str, text: str) -> dict:
    label, _, rest = text.partition(" - ")
    because, *downsides = rest.split("⚠")
    confidence = CONFIDENCE.search(text)
    return {
        "mark": mark,
        "label": label.strip(),
        "because": because.strip(),
        "downsides": [d.strip() for d in downsides if d.strip()],
        "verify": text.count("? verify:"),
        "confidence": min(int(confidence.group(1)), 100) if confidence else None,
        "user": bool(USER_CITED.search(text)),
    }


def read_decisions(research: list[tuple[int, str]]) -> list[dict]:
    decisions: list[dict] = []
    for _, line in research:
        header = lint.SLUG.match(line)
        if header:
            question = header.group(2)
            impact = lint.IMPACT.search(question)
            decisions.append({
                "slug": header.group(1),
                "question": TAG.sub("", question).strip(),
                "open": "[open]" in question,
                "impact": impact.group(1) if impact else "unrated",
                "alternatives": [],
                "flags": [],
            })
            continue
        if not decisions:
            continue
        if lint.FLAG.match(line):
            decisions[-1]["flags"].append(line.strip().lstrip("⚑").strip())
            continue
        alternative = lint.ALTERNATIVE.match(line)
        if alternative:
            decisions[-1]["alternatives"].append(split_alternative(*alternative.groups()))
    for decision in decisions:
        alts = decision["alternatives"]
        resolution = next((a for a in alts if a["mark"] in (lint.CHOSEN, lint.NOT_DOING)), None)
        decision["resolution"] = resolution
        decision["state"] = (
            "open" if decision["open"] or not resolution
            else "not-doing" if resolution["mark"] == lint.NOT_DOING else "chosen"
        )
        decision["confidence"] = resolution["confidence"] if resolution else None
        if decision["state"] == "open" or decision["flags"]:
            decision["settled"] = "you-pending"
        elif resolution["confidence"] is not None:
            decision["settled"] = "auto"
        elif resolution["user"]:
            decision["settled"] = "you"
        else:
            decision["settled"] = "scope"
        decision["downsides"] = sum(len(a["downsides"]) for a in alts)
        decision["verify"] = sum(a["verify"] for a in alts)
        decision["reach_sets"], decision["reach_files"] = set(), set()
    return decisions


def read_change_plan(body: list[tuple[int, str]]) -> list[dict]:
    sets: list[dict] = []
    for _, line in body:
        change_set = lint.CHANGE_SET.match(line)
        if change_set:
            title = re.sub(r"^\d+\.\s+", "", line).strip()
            sets.append({"n": int(change_set.group(1)), "title": title, "items": [], "tests": [],
                         "none": None, "echoes": [s for s, _ in lint.ECHO.findall(line)]})
            continue
        if not sets:
            continue
        current = sets[-1]
        tests = lint.TESTS.match(line)
        if tests:
            value = tests.group(1).strip()
            if value.lower().startswith("none"):
                current["none"] = value.partition(" - ")[2].strip() or "no reason given"
                continue
            for scenario in (s.strip() for s in value.split(";") if s.strip()):
                layer = re.match(r"^\[(" + "|".join(LAYERS) + r")\]\s*(.*)$", scenario)
                current["tests"].append(
                    {"layer": layer.group(1), "text": layer.group(2)} if layer
                    else {"layer": "untagged", "text": scenario}
                )
            continue
        item = ITEM.match(line)
        if item:
            text = item.group(2)
            current["items"].append({
                "files": [t for t in lint.BACKTICKED.findall(text) if lint.FILE_NAME.search(t)],
                "text": text,
                "echoes": [s for s, _ in lint.ECHO.findall(text)],
            })
    return sets


def read_scope(body: list[tuple[int, str]]) -> dict:
    """The scope section split into prose, non-goals, validation commands, and the flow sketch."""
    prose, non_goals, validation, flow, verification, threats, theorems = [], [], [], [], [], [], []
    block, fence = None, False
    for _, line in body:
        stripped = line.strip()
        if fence:
            if stripped.startswith("```"):
                fence = False
            else:
                flow.append(line)
            continue
        if stripped.startswith("```mermaid"):
            fence = True
            continue
        if stripped.startswith("### "):
            block = stripped[4:].strip().lower()
            if block in ("validation", "flow", "verification", "threat model", "theorems"):
                continue
        if block == "theorems":
            if stripped:
                theorems.append((0, line))
            continue
        if block == "validation":
            if stripped:
                command = stripped.lstrip("-* ").strip()
                late = "(end of build)" in command
                command = command.replace("(end of build)", "").strip().strip("`").strip()
                validation.append({"command": command, "late": late})
            continue
        if block == "verification":
            match = lint.VERIFICATION_LINE.match(line)
            if match:
                verification.append({"kind": match.group(1), "yes": match.group(2) == "yes",
                                     "why": match.group(3).removeprefix("- ").strip()})
            continue
        if block == "threat model":
            if stripped:
                threats.append(stripped.lstrip("-* ").strip())
            continue
        if block == "flow":
            if stripped:
                prose.append(line)
            continue
        if "⊘" in stripped:
            non_goals.append(stripped.lstrip("-* ").lstrip("⊘").strip())
            continue
        prose.append(line)
    return {"prose": prose, "non_goals": non_goals, "validation": validation, "flow": "\n".join(flow).strip(),
            "verification": verification, "threats": threats, "theorems": lint.read_theorems(theorems)}


def read_header(lines: list[str]) -> dict:
    title, risk, date, intent = "Untitled spec", None, None, []
    for line in lines:
        if lint.HEADING.match(line):
            break
        stripped = line.strip()
        if stripped.startswith("# ") and title == "Untitled spec":
            title = stripped[2:].strip()
            continue
        match = lint.RISK.match(stripped)
        if match:
            risk = {"tier": match.group(1).lower(), "why": stripped.partition(" - ")[2].strip()}
            continue
        found = DATE.search(stripped)
        if found and not date and len(stripped) < 40:
            date = found.group(1)
            continue
        if stripped:
            intent.append(stripped)
    return {"title": title, "risk": risk, "date": date, "intent": " ".join(intent)}


def read_spec(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    found = lint.sections(lines)
    problems, _ = lint.lint(path, lines)
    decisions = read_decisions(found.get("research", []))
    change_sets = read_change_plan(found.get("change plan", []))
    by_slug = {d["slug"]: d for d in decisions}
    for change_set in change_sets:
        for slug in change_set["echoes"]:
            if slug in by_slug:
                by_slug[slug]["reach_sets"].add(change_set["n"])
        for item in change_set["items"]:
            for slug in item["echoes"]:
                if slug in by_slug:
                    by_slug[slug]["reach_sets"].add(change_set["n"])
                    by_slug[slug]["reach_files"].update(item["files"])
    built = lint.built_change_sets(path)
    files = {n: paths for n, paths in lint.change_set_files(found.get("change plan", [])).items() if n not in built}
    waves, waited = lint.build_waves(files) if files else ([], {})
    return {
        "path": path, "text": text, "header": read_header(lines), "problems": problems,
        "decisions": decisions, "change_sets": change_sets, "scope": read_scope(found.get("scope", [])),
        "built": built, "waves": waves, "waited": waited,
    }


# ---------------------------------------------------------------------------
# Inline markdown, enough for spec prose
# ---------------------------------------------------------------------------


def inline(text: str) -> str:
    out = esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"(D-[a-z0-9-]+) \(([^)]*)\)",
                  lambda m: f'<a class="echo" href="#{m.group(1)}" data-tab="decisions" title="{m.group(1)}">'
                            f'{title(m.group(1))} ({m.group(2)})</a>', out)


def prose_html(lines: list[str]) -> str:
    out, para = [], []

    def flush() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            out.append(f"<h4>{inline(stripped.lstrip('#').strip())}</h4>")
        elif stripped[:2] in ("- ", "* "):
            flush()
            depth = (len(line) - len(line.lstrip())) // 2
            out.append(f'<div class="li" style="margin-left:{depth * 18}px">{inline(stripped[2:])}</div>')
        else:
            para.append(stripped)
    flush()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Pieces of the page
# ---------------------------------------------------------------------------


def impact_badge(impact: str) -> str:
    pips = IMPACT_PIPS.get(impact, 0)
    dots = "".join(f'<i class="pip{" on" if i < pips else ""}"></i>' for i in range(3))
    return f'<span class="impact impact-{esc(impact)}" title="impact: {esc(impact)}">{dots}{esc(impact.title())}</span>'


def meter(confidence: int | None) -> str:
    if confidence is None:
        return ""
    low = confidence < ASK_BELOW
    label = f"{'⚑ ' if low else ''}{confidence}%"
    return (f'<span class="meter{" low" if low else ""}" title="confidence {confidence}%; scope asks below {ASK_BELOW}%">'
            f'<span class="track"><span class="fill" style="width:{confidence}%"></span>'
            f'<span class="tick" style="left:{ASK_BELOW}%"></span></span><span class="mval">{label}</span></span>')


SETTLED_LABEL = {"you": "Settled by you", "auto": "Auto-applied", "scope": "Settled by scope",
                 "you-pending": "Waiting on you"}


def settled_chip(decision: dict) -> str:
    kind = decision["settled"]
    icon = {"you": "●", "auto": "◐", "scope": "◐", "you-pending": "⚑"}[kind]
    return f'<span class="chip settled-{kind}">{icon} {SETTLED_LABEL[kind]}</span>'


def state_chip(decision: dict) -> str:
    mark, label = {"chosen": ("✓", "Decided"), "open": ("?", "Open"), "not-doing": ("⊘", "Not doing")}[decision["state"]]
    return f'<span class="chip state-{decision["state"]}">{mark} {label}</span>'


def reach_html(decision: dict) -> str:
    sets, files = len(decision["reach_sets"]), len(decision["reach_files"])
    if not sets:
        return '<span class="reach none" title="no change set cites this decision">reach 0</span>'
    links = " ".join(f'<a href="#cs-{n}" data-tab="plan">#{n}</a>' for n in sorted(decision["reach_sets"]))
    return (f'<span class="reach" title="change sets and files citing this decision">'
            f'{sets} change set{"s" * (sets != 1)} · {files} file{"s" * (files != 1)} <span class="links">{links}</span></span>')


def warnings_html(decision: dict) -> str:
    bits = []
    if decision["downsides"]:
        bits.append(f'<span class="warn" title="accepted downsides">⚠ {decision["downsides"]}</span>')
    if decision["verify"]:
        bits.append(f'<span class="verify" title="claims nobody checked yet">? verify {decision["verify"]}</span>')
    return " ".join(bits)


def reply_button(text: str, label: str) -> str:
    return f'<button class="reply" type="button" data-reply="{esc(text)}">{esc(label)}</button>'


def alternative_html(decision: dict, alt: dict, with_replies: bool) -> str:
    mark_class = {"✓": "chosen", "✗": "rejected", "?": "open", "⊘": "notdoing"}[alt["mark"]]
    downs = "".join(f'<div class="down">⚠ {inline(d)}</div>' for d in alt["downsides"])
    action = ""
    if with_replies:
        resolution = decision["resolution"]
        if alt["mark"] == "?" or (alt["mark"] == "✗" and decision["state"] == "open"):
            action = reply_button(f'{decision["slug"]}: choose "{alt["label"]}"', "Choose this")
        elif alt["mark"] == "✗" and resolution:
            action = reply_button(f'{decision["slug"]}: take "{alt["label"]}" instead of "{resolution["label"]}"',
                                  "Take this instead")
    because = re.sub(r"\? verify:", '<b class="verify">? verify:</b>', inline(alt["because"]))
    return (f'<div class="alt {mark_class}"><span class="mark">{esc(alt["mark"])}</span>'
            f'<div class="atext"><strong>{inline(alt["label"])}</strong> <span class="because">{because}</span>{downs}</div>'
            f'{action}</div>')


def decision_card(decision: dict) -> str:
    alts = "".join(alternative_html(decision, a, True) for a in decision["alternatives"])
    flags = "".join(f'<div class="flag">⚑ {inline(f)}</div>' for f in decision["flags"])
    return f"""<article class="card decision" id="{esc(decision['slug'])}">
  <header><h3 title="{esc(decision['slug'])}">{esc(title(decision['slug']))}</h3><span class="question">{inline(decision['question'])}</span></header>
  <div class="indicators">{impact_badge(decision['impact'])}{state_chip(decision)}{settled_chip(decision)}{meter(decision['confidence'])}{reach_html(decision)}{warnings_html(decision)}</div>
  {flags}<div class="alts">{alts}</div>
</article>"""


def quadrant_svg(decisions: list[dict]) -> str:
    """A strip plot: one row per impact level, confidence across, open and user-settled in their own bands."""
    width, left, right, row_h, top = 800, 230, 660, 64, 26
    rows = ["high", "medium", "low"]
    height = top + row_h * len(rows) + 34

    def x_of(decision: dict) -> float:
        if decision["settled"] == "you-pending":
            return 175
        if decision["confidence"] is None:
            return 740
        return left + (right - left) * decision["confidence"] / 100

    parts = [f'<svg class="quadrant" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Decisions by impact and confidence; the decision list is the table view">']
    zone_w = (right - left) * ASK_BELOW / 100
    parts.append(f'<rect class="zone" x="{left}" y="{top}" width="{zone_w:.1f}" height="{row_h * 2}" rx="6"/>')
    parts.append(f'<text class="zone-label" x="{left + 6}" y="{top + 14}">look here: high impact, low confidence</text>')
    for i, row in enumerate(rows):
        y = top + row_h * i
        parts.append(f'<line class="grid" x1="20" x2="{width - 10}" y1="{y + row_h}" y2="{y + row_h}"/>')
        parts.append(f'<text class="row-label" x="20" y="{y + row_h / 2 + 4}">{row.title()} impact</text>')
    for pct in (0, 25, 50, 75, 100):
        x = left + (right - left) * pct / 100
        cls = "threshold" if pct == ASK_BELOW else "grid"
        parts.append(f'<line class="{cls}" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top + row_h * 3}"/>')
        parts.append(f'<text class="tick-label" x="{x:.1f}" y="{top + row_h * 3 + 16}" text-anchor="middle">{pct}%</text>')
    parts.append(f'<text class="tick-label" x="{left + zone_w:.1f}" y="{top - 8}" text-anchor="middle">asks below {ASK_BELOW}%</text>')
    parts.append(f'<text class="band-label" x="175" y="{top + row_h * 3 + 16}" text-anchor="middle">open</text>')
    parts.append(f'<text class="band-label" x="740" y="{top + row_h * 3 + 16}" text-anchor="middle">settled by you</text>')
    parts.append(f'<text class="band-label" x="{(left + right) / 2:.0f}" y="{height - 2}" text-anchor="middle">confidence of the applied recommendation</text>')
    placed: dict[str, list[float]] = {row: [] for row in rows}
    for decision in sorted(decisions, key=x_of):
        row = decision["impact"] if decision["impact"] in rows else "low"
        x = x_of(decision)
        near = sum(1 for other in placed[row] if abs(other - x) < 22)
        placed[row].append(x)
        y = top + row_h * rows.index(row) + row_h / 2 + (near % 3 - 1) * 16 * (1 if near else 0)
        attention = decision["settled"] == "you-pending" or (
            decision["confidence"] is not None and decision["confidence"] < ASK_BELOW)
        cls = "dot attention" if attention else ("dot you" if decision["settled"] == "you" else "dot")
        tip = (f'{title(decision["slug"])} · {decision["impact"]} impact · '
               + (f'{decision["confidence"]}% confidence' if decision["confidence"] is not None
                  else SETTLED_LABEL[decision["settled"]].lower())
               + f' · {decision["question"]}')
        label = title(decision["slug"])
        label = label if len(label) <= 24 else label[:23].rstrip() + "…"
        label_y = y - 11 if len(placed[row]) % 2 else y + 20
        parts.append(f'<a href="#{esc(decision["slug"])}" data-tab="decisions" class="{cls}" data-tip="{esc(tip)}">'
                     f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="13"/>'
                     f'<circle class="mark" cx="{x:.1f}" cy="{y:.1f}" r="6"/>'
                     f'<text class="dot-label" x="{x:.1f}" y="{label_y:.1f}" text-anchor="middle">'
                     f'{"⚑ " if attention else ""}{esc(label)}</text></a>')
    parts.append("</svg>")
    return "".join(parts)


def heat_step(count: int) -> int:
    return 0 if count == 0 else 1 if count <= 2 else 2 if count <= 5 else 3 if count <= 10 else 4


def heatmap(change_sets: list[dict]) -> str:
    used = {t["layer"] for c in change_sets for t in c["tests"]}
    layers = [layer for layer in LAYERS if layer in used] or ["unit", "integration", "e2e"]
    rows = []
    for change_set in change_sets:
        head = f'<th scope="row"><a href="#cs-{change_set["n"]}" data-tab="plan">{change_set["n"]}. {inline(change_set["title"])}</a></th>'
        if change_set["none"] is not None:
            rows.append(f'<tr>{head}<td colspan="{len(layers) + 1}" class="none">tests: none - {inline(change_set["none"])}</td></tr>')
            continue
        counts = [sum(1 for t in change_set["tests"] if t["layer"] == layer) for layer in layers]
        cells = "".join(f'<td class="heat h{heat_step(c)}" title="{c} {layer} scenario{"s" * (c != 1)}">{c or "-"}</td>'
                        for c, layer in zip(counts, layers))
        untagged = sum(1 for t in change_set["tests"] if t["layer"] == "untagged")
        total = sum(counts) + untagged
        rows.append(f'<tr>{head}{cells}<td class="total">{total}{" ⚠" if untagged else ""}</td></tr>')
    legend = "".join(f'<span><i class="heat h{s}"></i>{label}</span>'
                     for s, label in ((0, "0"), (1, "1-2"), (2, "3-5"), (3, "6-10"), (4, "11+")))
    heads = "".join(f"<th>{'E2E' if layer == 'e2e' else layer.title()}</th>" for layer in layers)
    return (f'<table class="heatmap"><thead><tr><th>Change set</th>{heads}'
            f'<th>Total</th></tr></thead><tbody>{"".join(rows)}</tbody></table><div class="legend">Scenarios {legend}</div>')


def verification_html(scope: dict) -> str:
    """The twenty kinds as a grid: planned ones with how, skipped ones with why; the lint names any missing."""
    planned = {v["kind"]: v for v in scope["verification"]}
    tiles = []
    for kind in lint.KINDS:
        entry = planned.get(kind)
        if entry is None:
            tiles.append(f'<div class="vk missing"><b>? {esc(kind_name(kind))}</b><span>not planned - lint fails</span></div>')
        elif entry["yes"]:
            tiles.append(f'<div class="vk yes"><b>✓ {esc(kind_name(kind))}</b><span>{inline(entry["why"])}</span></div>')
        else:
            tiles.append(f'<div class="vk no"><b>- {esc(kind_name(kind))}</b><span>{inline(entry["why"])}</span></div>')
    count = sum(1 for v in scope["verification"] if v["yes"])
    threats = "".join(f"<li>{inline(t)}</li>" for t in scope["threats"])
    threat_html = f'<h4>Threat model</h4><ul class="threats">{threats}</ul>' if threats else ""
    theorems = "".join(f'<li><b>{esc(t["name"])}</b>: {inline(t["reading"])}<pre>{esc(t["statement"])}</pre>'
                       f'<span class="muted">witness: {inline(t["witness"])}</span></li>' for t in scope["theorems"])
    theorem_html = (f'<h4>Theorems to approve</h4><p class="muted">Approving the spec approves these statements; build copies them '
                    f'verbatim into Spec.lean and no agent may change them.</p><ul class="threats">{theorems}</ul>') if theorems else ""
    return (f'<p class="muted">{count} of {len(lint.KINDS)} kinds apply; ship runs each one and skips the rest with these reasons.</p>'
            f'<div class="vgrid">{"".join(tiles)}</div>{threat_html}{theorem_html}')


def waves_html(data: dict) -> str:
    if not data["waves"]:
        return '<p class="muted">One change set, so build runs it alone.</p>'
    titles = {c["n"]: c["title"] for c in data["change_sets"]}
    out = []
    for i, wave in enumerate(data["waves"], start=1):
        chips = "".join(f'<a class="wave-chip" href="#cs-{n}" data-tab="plan"><b>{n}</b> {inline(titles.get(n, ""))}</a>' for n in wave)
        out.append(f'<div class="wave"><span class="wave-n">Wave {i}</span><div class="wave-sets">{chips}</div></div>')
    for n, (other, shared) in sorted(data["waited"].items()):
        more = f" +{len(shared) - 3} more" if len(shared) > 3 else ""
        out.append(f'<div class="waits">{n} waits on {other}: {", ".join(f"<code>{esc(s)}</code>" for s in shared[:3])}{more}</div>')
    return "\n".join(out)


def needs_you(data: dict) -> str:
    decisions = data["decisions"]
    pending = [d for d in decisions if d["settled"] == "you-pending"]
    applied = sorted((d for d in decisions if d["settled"] in ("auto", "scope")),
                     key=lambda d: (d["confidence"] if d["confidence"] is not None else 101, IMPACT_RANK.get(d["impact"], 3)))
    out = []
    if data["problems"]:
        items = "".join(f"<li><code>{esc(p)}</code></li>" for p in data["problems"][:8])
        more = f'<li class="muted">and {len(data["problems"]) - 8} more</li>' if len(data["problems"]) > 8 else ""
        out.append(f'<div class="banner critical"><b>⚑ Draft:</b> lint-spec.py reports {len(data["problems"])} problem(s); '
                   f'this plan is not final.<ul>{items}{more}</ul></div>')
    for decision in pending:
        alts = "".join(alternative_html(decision, a, True) for a in decision["alternatives"])
        flags = "".join(f'<div class="flag">⚑ {inline(f)}</div>' for f in decision["flags"])
        out.append(f'<div class="card pending"><header><h3 title="{esc(decision["slug"])}">{esc(title(decision["slug"]))}</h3>'
                   f'<span class="question">{inline(decision["question"])}</span>{impact_badge(decision["impact"])}</header>{flags}{alts}</div>')
    if applied:
        rows = []
        for decision in applied:
            resolution = decision["resolution"]
            swaps = "".join(reply_button(f'{decision["slug"]}: take "{a["label"]}" instead of "{resolution["label"]}"',
                                         f'↺ {a["label"]}')
                            for a in decision["alternatives"] if a["mark"] == "✗")
            confidence = meter(decision["confidence"]) or '<span class="muted">no confidence stated</span>'
            rows.append(f'<tr><td><a href="#{esc(decision["slug"])}" data-tab="decisions" title="{esc(decision["slug"])}"><b>{esc(title(decision["slug"]))}</b></a>'
                        f'<div class="q">{inline(decision["question"])}</div></td><td>{impact_badge(decision["impact"])}</td>'
                        f'<td><b>{esc(resolution["mark"])} {inline(resolution["label"])}</b></td><td>{confidence}</td>'
                        f'<td class="swaps">{swaps}</td></tr>')
        out.append('<h3 class="sub">Applied without asking, least confident first</h3>'
                   '<table class="applied"><thead><tr><th>Decision</th><th>Impact</th><th>Applied</th>'
                   f'<th>Confidence</th><th>Overturn</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')
    if not out:
        return '<p class="muted">Nothing waits on you: every decision was settled with you.</p>'
    return "\n".join(out)


def change_set_card(change_set: dict, built: set[int]) -> str:
    items = "".join(
        f'<li>{"".join(f"<code>{esc(f)}</code> " for f in item["files"])}<span>{inline(item["text"])}</span></li>'
        for item in change_set["items"])
    if change_set["none"] is not None:
        tests = f'<p class="muted">tests: none - {inline(change_set["none"])}</p>'
    else:
        tests = "".join(f'<li><span class="layer layer-{t["layer"]}">{esc(t["layer"])}</span> {inline(t["text"])}</li>'
                        for t in change_set["tests"])
        tests = f'<ul class="tests">{tests}</ul>'
    done = '<span class="chip state-chosen">✓ built</span>' if change_set["n"] in built else ""
    return (f'<article class="card" id="cs-{change_set["n"]}"><header><span class="csn">{change_set["n"]}</span>'
            f'<h3>{inline(change_set["title"])}</h3>{done}</header><ul class="items">{items}</ul>'
            f'<h4>Tests</h4>{tests}</article>')


def flow_html(flow: str) -> str:
    if not flow:
        return ""
    return f"""<section><h2>Flow <span class="provenance">design sketch from the spec, not derived from code</span></h2>
<div class="card flow"><pre class="mermaid">{esc(flow)}</pre></div></section>"""


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def render(data: dict) -> str:
    header, scope, decisions = data["header"], data["scope"], data["decisions"]
    change_sets = data["change_sets"]
    pending = sum(1 for d in decisions if d["settled"] == "you-pending")
    if data["problems"]:
        status = '<span class="status critical">⚑ Draft - lint fails</span>'
    elif pending:
        status = f'<span class="status warning">⚑ {pending} waiting on you</span>'
    else:
        status = '<span class="status good">✓ Ready to build</span>'
    risk = header["risk"]
    risk_html = (f'<span class="risk risk-{risk["tier"]}">{"▲" if risk["tier"] == "high" else "◆" if risk["tier"] == "medium" else "▽"} '
                 f'{risk["tier"].title()} risk</span><span class="risk-why">{inline(risk["why"])}</span>'
                 if risk else '<span class="risk">Risk not stated</span>')
    files = sorted({f for c in change_sets for i in c["items"] for f in i["files"]})
    scenarios = sum(len(c["tests"]) for c in change_sets)
    tiles = "".join(f'<div class="tile"><b>{value}</b><span>{label}</span></div>' for value, label in (
        (len(decisions), "decisions"), (sum(1 for d in decisions if d["impact"] == "high"), "high impact"),
        (len(change_sets), "change sets"), (len(files), "files planned"), (scenarios, "test scenarios")))
    by_impact = sorted(decisions, key=lambda d: IMPACT_RANK.get(d["impact"], 3))
    touches = "".join(
        f'<li><a href="#cs-{c["n"]}" data-tab="plan">{c["n"]}</a> '
        + " ".join(f"<code>{esc(f)}</code>" for f in sorted({f for i in c["items"] for f in i["files"]}))
        + "</li>" for c in change_sets)
    non_goals = "".join(f"<li>⊘ {inline(n)}</li>" for n in scope["non_goals"]) or '<li class="muted">None stated.</li>'
    validation = "".join(f'<li><code>{esc(v["command"])}</code>{" <span class=muted>run in verify</span>" if v["late"] else ""}</li>'
                         for v in scope["validation"])
    mermaid = ('<script type="module">\nconst nodes = document.querySelectorAll("pre.mermaid");\n'
               'if (nodes.length) {\n  try {\n    const { default: mermaid } = await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs");\n'
               '    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "neutral" });\n'
               '    await mermaid.run({ nodes });\n  } catch (e) { nodes.forEach(n => n.classList.add("offline")); }\n}\n</script>'
               if scope["flow"] else "")
    title = esc(header["title"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spec: {title}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<header class="top">
  <div class="titles"><h1>{title}</h1><p class="intent">{inline(header["intent"]) or '<span class="muted">No intent stated.</span>'}</p>
  <p class="meta"><code>{esc(data["path"])}</code>{" · " + esc(header["date"]) if header["date"] else ""}</p></div>
  <div class="badges">{status}<div class="riskbox">{risk_html}</div></div>
</header>
<nav class="tabs" role="tablist">
  <button role="tab" data-tab="overview" aria-selected="true">Overview</button>
  <button role="tab" data-tab="decisions">Decisions <span class="count">{len(decisions)}</span></button>
  <button role="tab" data-tab="plan">Change plan <span class="count">{len(change_sets)}</span></button>
  <button role="tab" data-tab="source">spec.md</button>
</nav>

<div class="panel" data-panel="overview">
  <div class="tiles">{tiles}</div>
  <section><h2>Needs you</h2>{needs_you(data)}</section>
  <section><h2>Where to look</h2><div class="card chart">{quadrant_svg(decisions) if decisions else '<p class="muted">No decisions argued.</p>'}</div></section>
  {flow_html(scope["flow"])}
  <section class="two"><div><h2>Will touch</h2><div class="card"><ul class="touches">{touches or '<li class="muted">No files named.</li>'}</ul></div></div>
  <div><h2>Will not do</h2><div class="card"><ul class="nongoals">{non_goals}</ul></div></div></section>
  <section><h2>Verification plan</h2><div class="card">{verification_html(scope)}</div></section>
  <section><h2>Tests per layer</h2><div class="card">{heatmap(change_sets) if change_sets else '<p class="muted">No change sets yet.</p>'}
  <h4>Validation</h4><ul class="validation">{validation or '<li class="muted">No Validation block.</li>'}</ul></div></section>
  <section><h2>Build order</h2><div class="card">{waves_html(data)}</div></section>
  <section><h2>Scope details</h2><div class="card prose">{prose_html(scope["prose"]) or '<p class="muted">Empty.</p>'}</div></section>
</div>

<div class="panel" data-panel="decisions" hidden>
  <p class="muted">Highest impact first. Indicators: impact, state, who settled it, confidence of the applied recommendation (scope asks below {ASK_BELOW}%), reach (change sets and files citing it), accepted downsides and unchecked claims.</p>
  {"".join(decision_card(d) for d in by_impact) or '<p class="muted">No decisions argued.</p>'}
</div>

<div class="panel" data-panel="plan" hidden>
  {"".join(change_set_card(c, data["built"]) for c in change_sets) or '<p class="muted">No change sets yet.</p>'}
</div>

<div class="panel" data-panel="source" hidden><pre class="source">{esc(data["text"])}</pre></div>
</main>

<aside class="tray" hidden>
  <div class="tray-head"><b>Reply to scope</b><span class="muted">paste into the session</span></div>
  <textarea rows="4" spellcheck="false"></textarea>
  <div class="tray-actions"><button type="button" class="copy">Copy reply</button><button type="button" class="clear">Clear</button></div>
</aside>
<div class="tip" hidden></div>
<script>{JS}</script>
{mermaid}
</body>
</html>
"""


CSS = """
:root { color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series: #2a78d6; --series-wash: rgba(42,120,214,0.08);
  --good: #0ca30c; --good-text: #006300; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --critical-wash: rgba(208,59,59,0.07);
  --h1: #cde2fb; --h2: #86b6ef; --h3: #3987e5; --h4: #1c5cab; --h-ink-dark: #0b0b0b; --h-ink-light: #ffffff; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series: #3987e5; --series-wash: rgba(57,135,229,0.12); --good-text: #0ca30c;
  --critical-wash: rgba(208,59,59,0.14);
  --h1: #104281; --h2: #1c5cab; --h3: #3987e5; --h4: #86b6ef; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 28px 24px 120px; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12.5px; }
code { background: var(--series-wash); border-radius: 4px; padding: 1px 5px; }
a { color: var(--series); text-decoration: none; } a:hover { text-decoration: underline; }
.muted { color: var(--muted); }
.top { display: flex; gap: 24px; justify-content: space-between; align-items: flex-start; }
h1 { margin: 0 0 6px; font-size: 26px; letter-spacing: -0.01em; }
.intent { margin: 0 0 6px; color: var(--ink-2); font-size: 15px; max-width: 70ch; }
.meta { margin: 0; color: var(--muted); font-size: 12px; } .meta code { background: none; padding: 0; }
.badges { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; min-width: 240px; }
.status { font-weight: 600; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--ring); background: var(--surface); }
.status.good { color: var(--good-text); } .status.warning, .status.critical { color: var(--critical); border-color: var(--critical); }
.riskbox { text-align: right; } .risk { font-weight: 600; display: block; } .risk-why { color: var(--ink-2); font-size: 12.5px; }
.risk-high { color: var(--critical); }
.tabs { display: flex; gap: 4px; margin: 22px 0 18px; border-bottom: 1px solid var(--grid); }
.tabs button { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--ink-2); font: inherit; font-weight: 600; padding: 10px 14px; cursor: pointer; }
.tabs button[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--series); }
.count { color: var(--muted); font-weight: 400; margin-left: 4px; }
section { margin: 0 0 26px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em; color: var(--ink-2); margin: 0 0 10px; }
h3 { margin: 0; font-size: 15px; } h3.sub { margin: 18px 0 8px; font-size: 13px; color: var(--ink-2); }
h4 { margin: 14px 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 16px 18px; margin-bottom: 12px; }
.card > header { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.question { color: var(--ink-2); }
.tiles { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 26px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 14px 16px; }
.tile b { display: block; font-size: 26px; font-weight: 600; } .tile span { color: var(--ink-2); }
.banner { border-radius: 12px; padding: 12px 16px; margin-bottom: 12px; border: 1px solid var(--critical); background: var(--critical-wash); }
.banner ul { margin: 6px 0 0; padding-left: 18px; } .banner code { background: none; }
.card.pending { border-color: var(--critical); }
.flag { color: var(--critical); font-weight: 600; margin: 4px 0 8px; }
.alt { display: grid; grid-template-columns: 22px 1fr auto; gap: 8px; align-items: start; padding: 8px 0; border-top: 1px solid var(--grid); }
.alt .mark { font-weight: 700; text-align: center; color: var(--muted); }
.alt.chosen .mark { color: var(--good-text); } .alt.chosen strong { color: var(--ink); }
.alt.rejected .atext { color: var(--ink-2); } .alt.open .mark, .alt.notdoing .mark { color: var(--series); }
.because { color: var(--ink-2); } .down { color: var(--ink-2); margin-top: 2px; } .down::first-letter { color: var(--serious); }
.verify { color: var(--critical); font-weight: 600; }
.warn { color: var(--ink-2); }
.indicators { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin-bottom: 8px; font-size: 12.5px; }
.impact { display: inline-flex; align-items: center; gap: 3px; font-weight: 600; font-size: 12.5px; color: var(--ink-2); }
.pip { width: 7px; height: 7px; border-radius: 2px; background: var(--grid); display: inline-block; }
.pip.on { background: var(--series); } .impact-high .pip.on { background: var(--h4); }
.impact { padding-right: 2px; } .impact .pip:last-of-type { margin-right: 4px; }
.chip { border: 1px solid var(--ring); border-radius: 999px; padding: 1px 9px; color: var(--ink-2); white-space: nowrap; }
.state-chosen { color: var(--good-text); } .settled-you-pending, .state-open { color: var(--critical); border-color: var(--critical); }
.meter { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.track { position: relative; width: 90px; height: 6px; border-radius: 3px; background: var(--grid); display: inline-block; }
.fill { position: absolute; inset: 0 auto 0 0; border-radius: 3px; background: var(--series); }
.tick { position: absolute; top: -3px; width: 2px; height: 12px; background: var(--ink-2); }
.meter.low .fill { background: var(--critical); } .meter.low .mval { color: var(--critical); font-weight: 600; }
.mval { font-variant-numeric: tabular-nums; color: var(--ink-2); }
.reach { color: var(--ink-2); } .reach.none { color: var(--muted); } .reach .links a { margin-right: 3px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
th[scope="row"] { text-transform: none; letter-spacing: 0; font-size: 13px; color: var(--ink); font-weight: 500; }
td .q { color: var(--ink-2); font-size: 12.5px; }
.swaps { display: flex; flex-wrap: wrap; gap: 6px; }
button.reply { font: inherit; font-size: 12px; border: 1px solid var(--ring); background: var(--page); color: var(--ink); border-radius: 8px; padding: 3px 9px; cursor: pointer; white-space: nowrap; }
button.reply:hover { border-color: var(--series); } button.reply.on { background: var(--series); color: #fff; border-color: var(--series); }
.chart { overflow-x: auto; }
svg.quadrant { width: 100%; min-width: 640px; height: auto; display: block; font-size: 11px; }
.quadrant .grid { stroke: var(--grid); stroke-width: 1; } .quadrant .threshold { stroke: var(--critical); stroke-width: 1.5; }
.quadrant .zone { fill: var(--critical-wash); } .quadrant .zone-label { fill: var(--critical); font-size: 11px; }
.quadrant .row-label { fill: var(--ink-2); font-weight: 600; font-size: 12px; }
.quadrant .tick-label, .quadrant .band-label { fill: var(--muted); }
.quadrant .hit { fill: transparent; } .quadrant .mark { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
.quadrant .you .mark { fill: var(--muted); } .quadrant .attention .mark { fill: var(--critical); }
.quadrant .dot-label { fill: var(--ink-2); font-size: 11px; } .quadrant .attention .dot-label { fill: var(--critical); font-weight: 600; }
.quadrant a:hover .mark, .quadrant a:focus .mark { stroke: var(--ink); }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
ul { margin: 0; padding-left: 0; list-style: none; } li { padding: 4px 0; }
.touches li a { font-weight: 600; margin-right: 6px; } .touches code { margin: 0 4px 4px 0; display: inline-block; }
.heatmap td.heat { text-align: center; font-variant-numeric: tabular-nums; font-weight: 600; width: 92px; border: 2px solid var(--surface); border-radius: 4px; }
.heat.h0 { color: var(--muted); } .heat.h1 { background: var(--h1); color: var(--h-ink-dark); }
.heat.h2 { background: var(--h2); color: var(--h-ink-dark); } .heat.h3 { background: var(--h3); color: var(--h-ink-light); }
.heat.h4 { background: var(--h4); color: var(--h-ink-light); }
@media (prefers-color-scheme: dark) { .heat.h1, .heat.h2 { color: var(--h-ink-light); } .heat.h4 { color: var(--h-ink-dark); } }
.heatmap td.total { text-align: center; font-variant-numeric: tabular-nums; width: 70px; } .heatmap td.none { color: var(--muted); }
.vgrid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.vk { border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; background: var(--page); font-size: 12.5px; }
.vk b { display: block; font-size: 13px; } .vk span { color: var(--ink-2); }
.vk.yes { border-left: 3px solid var(--series); } .vk.yes b { color: var(--ink); }
.vk.no b { color: var(--muted); font-weight: 500; } .vk.no span { color: var(--muted); }
.vk.missing { border-color: var(--critical); } .vk.missing b, .vk.missing span { color: var(--critical); }
.threats li { border-top: 1px solid var(--grid); padding: 6px 0; color: var(--ink-2); }
@media (max-width: 820px) { .vgrid { grid-template-columns: repeat(2, 1fr); } }
.legend { display: flex; gap: 12px; align-items: center; margin-top: 10px; color: var(--muted); font-size: 12px; }
.legend i { display: inline-block; width: 14px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; border: 1px solid var(--ring); }
.wave { display: grid; grid-template-columns: 70px 1fr; gap: 10px; align-items: center; padding: 6px 0; }
.wave-n { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; }
.wave-sets { display: flex; flex-wrap: wrap; gap: 8px; }
.wave-chip { border: 1px solid var(--ring); border-left: 3px solid var(--series); border-radius: 8px; padding: 5px 10px; color: var(--ink); background: var(--page); }
.waits { color: var(--ink-2); font-size: 12.5px; margin: 4px 0 0 80px; }
.csn { background: var(--series); color: #fff; border-radius: 6px; padding: 0 8px; font-weight: 700; }
.items li { display: flex; gap: 8px; flex-wrap: wrap; border-top: 1px solid var(--grid); padding: 6px 0; } .items li span { color: var(--ink-2); }
.tests li { border-top: 1px solid var(--grid); padding: 6px 0; color: var(--ink-2); }
.layer { display: inline-block; min-width: 82px; text-align: center; font-size: 11px; font-weight: 600; border: 1px solid var(--ring); border-radius: 6px; padding: 0 6px; margin-right: 6px; color: var(--ink); }
.layer-untagged { color: var(--critical); border-color: var(--critical); }
.prose p { margin: 0 0 8px; } .prose .li::before { content: "- "; color: var(--muted); }
.provenance { text-transform: none; letter-spacing: 0; font-weight: 400; color: var(--muted); margin-left: 8px; }
pre.mermaid { margin: 0; background: none; text-align: center; } pre.mermaid.offline { text-align: left; white-space: pre-wrap; }
pre.mermaid.offline::before { content: "Mermaid could not load, so this is the sketch's source.\\A\\A"; color: var(--muted); }
pre.source { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 18px; white-space: pre-wrap; line-height: 1.55; }
.tray { position: fixed; right: 20px; bottom: 20px; width: min(460px, calc(100vw - 40px)); background: var(--surface); border: 1px solid var(--series); border-radius: 12px; padding: 12px; box-shadow: 0 8px 30px rgba(0,0,0,.18); }
.tray-head { display: flex; justify-content: space-between; margin-bottom: 6px; }
.tray textarea { width: 100%; font: 12.5px ui-monospace, Menlo, monospace; background: var(--page); color: var(--ink); border: 1px solid var(--ring); border-radius: 8px; padding: 8px; resize: vertical; }
.tray-actions { display: flex; gap: 8px; margin-top: 8px; }
.tray-actions button { font: inherit; border-radius: 8px; padding: 5px 12px; cursor: pointer; border: 1px solid var(--ring); background: var(--page); color: var(--ink); }
.tray-actions .copy { background: var(--series); color: #fff; border-color: var(--series); }
.tip { position: fixed; pointer-events: none; background: var(--ink); color: var(--surface); font-size: 12px; padding: 6px 9px; border-radius: 6px; max-width: 360px; z-index: 5; }
@media (max-width: 820px) { .tiles { grid-template-columns: repeat(2, 1fr); } .two { grid-template-columns: 1fr; } .top { flex-direction: column; } .badges { align-items: flex-start; } .riskbox { text-align: left; } }
"""

JS = """
const tabs = document.querySelectorAll('.tabs button');
function show(name, anchor) {
  tabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
  document.querySelectorAll('.panel').forEach(p => { p.hidden = p.dataset.panel !== name; });
  if (anchor) { const el = document.getElementById(anchor); if (el) el.scrollIntoView({ block: 'start' }); }
}
tabs.forEach(t => t.addEventListener('click', () => show(t.dataset.tab)));
document.addEventListener('click', e => {
  const link = e.target.closest('a[data-tab]');
  if (!link) return;
  e.preventDefault();
  show(link.dataset.tab, decodeURIComponent((link.getAttribute('href') || '').slice(1)));
});
const tray = document.querySelector('.tray'), area = tray.querySelector('textarea');
const chosen = new Map();
function refresh() { area.value = [...chosen.values()].join('\\n'); tray.hidden = chosen.size === 0; }
document.querySelectorAll('button.reply').forEach(b => b.addEventListener('click', () => {
  const slug = b.dataset.reply.split(':')[0];
  const same = chosen.get(slug) === b.dataset.reply;
  document.querySelectorAll('button.reply').forEach(o => { if (o.dataset.reply.split(':')[0] === slug) o.classList.remove('on'); });
  if (same) chosen.delete(slug); else { chosen.set(slug, b.dataset.reply); document.querySelectorAll('button.reply').forEach(o => { if (o.dataset.reply === b.dataset.reply) o.classList.add('on'); }); }
  refresh();
}));
tray.querySelector('.copy').addEventListener('click', async e => {
  try { await navigator.clipboard.writeText(area.value); } catch (_) { area.select(); document.execCommand('copy'); }
  e.target.textContent = 'Copied'; setTimeout(() => { e.target.textContent = 'Copy reply'; }, 1400);
});
tray.querySelector('.clear').addEventListener('click', () => {
  chosen.clear(); document.querySelectorAll('button.reply.on').forEach(o => o.classList.remove('on')); refresh();
});
const tip = document.querySelector('.tip');
document.querySelectorAll('[data-tip]').forEach(el => {
  const move = e => { tip.style.left = (e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; };
  el.addEventListener('mouseenter', e => { tip.textContent = el.dataset.tip; tip.hidden = false; move(e); });
  el.addEventListener('mousemove', move);
  el.addEventListener('mouseleave', () => { tip.hidden = true; });
  el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); tip.textContent = el.dataset.tip; tip.hidden = false; tip.style.left = r.right + 'px'; tip.style.top = r.bottom + 'px'; });
  el.addEventListener('blur', () => { tip.hidden = true; });
});
"""


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--open"]
    if len(args) != 1:
        print("usage: render-spec.py <path to spec.md> [--open]", file=sys.stderr)
        return 2
    path = Path(args[0])
    if not path.is_file():
        print(f"no spec at {path}", file=sys.stderr)
        return 2
    try:
        data = read_spec(path)
    except UnicodeDecodeError as error:
        print(f"{path} is not valid UTF-8: {error}", file=sys.stderr)
        return 2
    out = path.with_suffix(".html")
    out.write_text(render(data), encoding="utf-8")
    print(out)
    if data["problems"]:
        print(f"rendered as a draft: lint-spec.py reports {len(data['problems'])} problem(s)")
    if "--open" in argv:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
