#!/usr/bin/env python3
"""Assemble a /review-pr HTML report from a reviewer roster + per-agent reports + synthesis.json.

Usage:
    python3 assemble.py <REPORT_DIR>

Expects, inside <REPORT_DIR>:
    reports/<slug>.md   one markdown report per reviewer that ran (slug = reviewer frontmatter slug)
    synthesis.json      the main agent's consolidated verdict + buckets (+ optional pushback/callouts)

Reads the reviewer roster (for tab name/emoji/order/role) from <skill_dir>/reviewers/*.md.
Writes <REPORT_DIR>/report.html. Stdlib only - no third-party deps.
"""

import html
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
REVIEWERS_DIR = SKILL_DIR / "reviewers"
TEMPLATE = SKILL_DIR / "report-template.html"

BUCKET_DEFS = [
    ("red", "red", "🚨 Major Red Flags"),
    ("green", "green", "✅ Major Green Flags"),
    ("minor", "orange", "🤏 Minor Issues"),
    ("future", "yellow", "🔮 Future Pitfalls"),
]


# --------------------------------------------------------------------------- #
# Frontmatter parsing (minimal, no PyYAML dependency)
# --------------------------------------------------------------------------- #
def parse_frontmatter(text):
    """Return (meta_dict, body). Supports `key: value` lines with optional quotes,
    bool and int coercion. Body is everything after the closing `---`."""
    meta = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm, body = parts[1], parts[2].lstrip("\n")
            for line in fm.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                low = val.lower()
                if low in ("true", "false"):
                    val = (low == "true")
                else:
                    try:
                        val = int(val)
                    except ValueError:
                        pass
                meta[key] = val
    return meta, body


# --------------------------------------------------------------------------- #
# Tiny markdown -> HTML renderer (mirrors the structure subagents produce)
# --------------------------------------------------------------------------- #
def render_markdown(src):
    src = (src or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")
    out, para = [], []
    i, in_list, list_type = 0, False, None

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def inline(s):
        s = esc(s)
        s = re.sub(r"`([^`]+)`", lambda m: "<code>" + m.group(1) + "</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(^|[^*])\*([^*\s][^*]*)\*", r"\1<em>\2</em>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        return s

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list():
        nonlocal in_list, list_type
        if in_list:
            out.append("</ol>" if list_type == "ol" else "</ul>")
            in_list, list_type = False, None

    while i < len(lines):
        line = lines[i]
        if re.match(r"^```", line):
            flush_para(); close_list()
            i += 1
            code = []
            while i < len(lines) and not re.match(r"^```", lines[i]):
                code.append(lines[i]); i += 1
            i += 1
            out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            flush_para(); close_list()
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>" + inline(h.group(2)) + f"</h{lvl}>")
            i += 1; continue
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            flush_para(); close_list(); out.append("<hr>"); i += 1; continue
        ul = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if ul:
            flush_para()
            if not in_list or list_type != "ul":
                close_list(); out.append("<ul>"); in_list, list_type = True, "ul"
            out.append("<li>" + inline(ul.group(1)) + "</li>"); i += 1; continue
        ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if ol:
            flush_para()
            if not in_list or list_type != "ol":
                close_list(); out.append("<ol>"); in_list, list_type = True, "ol"
            out.append("<li>" + inline(ol.group(1)) + "</li>"); i += 1; continue
        if re.match(r"^\s*$", line):
            flush_para(); close_list(); i += 1; continue
        bq = re.match(r"^>\s?(.*)$", line)
        if bq:
            flush_para(); close_list()
            out.append("<blockquote>" + inline(bq.group(1)) + "</blockquote>")
            i += 1; continue
        para.append(line.strip()); i += 1

    flush_para(); close_list()
    return "".join(out)


# --------------------------------------------------------------------------- #
# HTML fragment builders
# --------------------------------------------------------------------------- #
def e(v):
    return html.escape(str(v))


def finding_li(item, with_src):
    label = e(item.get("label", "")).strip()
    body = e(item.get("body", "")).strip()
    loc = item.get("loc", "")
    src = item.get("src", "")
    src_html = f'<span class="src">{e(src)}</span>' if (with_src and src) else ""
    strong = f"<strong>{label}</strong> " if label else ""
    loc_html = f' <code>{e(loc)}</code>' if loc else ""
    return f"<li>{src_html}{strong}{body}{loc_html}</li>"


def build_header(syn):
    pr, v = syn["pr"], syn["verdict"]
    return (
        "<header>"
        f'<div class="verdict {e(v["class"])}">{e(v["text"])}</div>'
        f'<h1>{e(pr["title"])}</h1>'
        '<p class="meta">'
        f'<a href="{e(pr["url"])}" target="_blank" rel="noopener">{e(pr["repo"])}#{e(pr["number"])}</a>'
        ' <span class="sep">·</span> by '
        f'<strong>{e(pr["author"])}</strong>'
        ' <span class="sep">·</span> '
        f'<span style="color: var(--green);">+{e(pr["additions"])}</span>'
        f' / <span style="color: var(--red);">-{e(pr["deletions"])}</span>'
        f' across {e(pr["files"])} files'
        ' <span class="sep">·</span> '
        f'<code>{e(pr["head_ref"])}</code> &rarr; <code>{e(pr["base_ref"])}</code>'
        f' <span class="ci-badge">CI: {e(pr["ci_status"])}</span>'
        "</p></header>"
    )


def build_synthesis_panel(syn):
    v = syn["verdict"]
    parts = ['<div class="panel" id="panel-synthesis">']

    for key, cls, heading in (("pushback", "pushback", "Pushback on declined items"),
                              ("callouts", "callouts", "Reviewer notes")):
        items = syn.get(key) or []
        if items:
            parts.append(f'<section class="{cls}"><h2>{heading}</h2><ul class="findings">')
            parts += [finding_li(it, with_src=False) for it in items]
            parts.append("</ul></section>")

    parts.append(
        f'<div class="rationale"><div class="label">Verdict rationale</div>{e(v["rationale"])}</div>'
    )
    parts.append(
        f'<div class="purpose"><div class="label">What this PR does</div>{e(v["purpose"])}</div>'
    )

    buckets = syn.get("buckets", {})
    for key, cls, heading in BUCKET_DEFS:
        items = buckets.get(key) or []
        parts.append(f'<section class="{cls}"><h2>{heading}</h2><ul class="findings">')
        if items:
            parts += [finding_li(it, with_src=True) for it in items]
        else:
            parts.append('<li class="none">None.</li>')
        parts.append("</ul></section>")

    parts.append("</div>")
    return "".join(parts)


def load_roster():
    """slug -> metadata for every reviewer definition file."""
    roster = {}
    if REVIEWERS_DIR.is_dir():
        for f in REVIEWERS_DIR.glob("*.md"):
            meta, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
            slug = str(meta.get("slug", f.stem))
            meta.setdefault("name", slug.replace("-", " ").title())
            meta.setdefault("emoji", "🔎")
            meta.setdefault("order", 999)
            meta.setdefault("role", "")
            roster[slug] = meta
    return roster


def build_agent_panels(report_dir, roster):
    """Return (tab_buttons_html, panels_html) for every report present on disk."""
    reports_dir = report_dir / "reports"
    present = []
    if reports_dir.is_dir():
        for rf in reports_dir.glob("*.md"):
            slug = rf.stem
            meta = roster.get(slug, {"name": slug.replace("-", " ").title(),
                                     "emoji": "🔎", "order": 999, "role": ""})
            present.append((int(meta.get("order", 999)), slug, meta,
                            rf.read_text(encoding="utf-8")))
    present.sort(key=lambda x: (x[0], x[1]))

    tabs, panels = [], []
    for _, slug, meta, body in present:
        tabs.append(
            f'<button class="tab" data-panel="panel-{e(slug)}">{meta["emoji"]} {e(meta["name"])}</button>'
        )
        role = e(meta.get("role", "")).strip()
        role_html = f'<span class="role">&mdash; {role}</span>' if role else ""
        panels.append(
            f'<div class="panel agent-report" id="panel-{e(slug)}" hidden>'
            f'<div class="agent-head"><span class="badge">{meta["emoji"]}</span>'
            f'<span class="name">{e(meta["name"])}</span>{role_html}</div>'
            f'<div class="body">{render_markdown(body)}</div></div>'
        )
    return "\n".join(tabs), "\n".join(panels)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: assemble.py <REPORT_DIR>")
    report_dir = Path(sys.argv[1]).expanduser().resolve()
    syn = json.loads((report_dir / "synthesis.json").read_text(encoding="utf-8"))

    roster = load_roster()
    agent_tabs, agent_panels = build_agent_panels(report_dir, roster)

    tabs = ['<button class="tab active" data-panel="panel-synthesis">⚖️ Synthesis</button>',
            agent_tabs]
    panels = [build_synthesis_panel(syn), agent_panels]

    out = TEMPLATE.read_text(encoding="utf-8")
    out = out.replace("{{PR_TITLE}}", e(syn["pr"]["title"]))
    out = out.replace("{{HEADER}}", build_header(syn))
    out = out.replace("{{TABS}}", "\n".join(t for t in tabs if t))
    out = out.replace("{{PANELS}}", "\n".join(p for p in panels if p))
    out = out.replace("{{TIMESTAMP}}", e(syn["pr"].get("timestamp", "")))

    dest = report_dir / "report.html"
    dest.write_text(out, encoding="utf-8")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
