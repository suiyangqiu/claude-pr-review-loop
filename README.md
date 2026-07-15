# claude-pr-review-loop

A pair of [Claude Code](https://claude.com/claude-code) skills that turn pull-request review into a closed loop: a multi-agent reviewer (`/review-pr`) that produces a verdict and a tabbed HTML report, and a companion (`/action-feedback`) that works the feedback, makes the changes it agrees with, and feeds its reasoning back in to trigger a re-review.

```
  /review-pr  ──►  HTML report  ──►  /action-feedback  ──►  paste output back  ──►  re-review
       ▲                                                                                │
       └────────────────────────────  loop until the verdict is green  ◄───────────────┘
```

The two skills are useful on their own, but they are designed to be run together.

## What each skill does

### `/review-pr <pr-url-or-number>`

Reviews a GitHub **or** Azure DevOps PR with a roster of specialist sub-agents that each form an independent view, then synthesizes a single verdict graded by severity tiers. Output is a self-contained, editorial-styled HTML report - a Synthesis tab plus one tab per reviewer - for a human to read and act on.

Verdicts are one of:

| | |
|---|---|
| 🟢 | Approve and merge |
| 🟡 | Approve with minor changes |
| 🟠 | Request changes |
| 🔴 | Reject |

The verdict is **derived from severity tiers**, not picked by feel. Every finding is graded **Blocking** (must fix), **Material** (should fix before merge), or **Optional** (polish / nits), and the verdict falls out of the worst tier present: any Blocking → 🟠 / 🔴, else any Material → 🟡, else (only Optional) → 🟢. So 🟢 means *no Blocking or Material issues remain* - not that the PR is flawless. That is the point of the loop: a later fresh pass that only surfaces Optional nits does not change the verdict, so the review converges instead of finding things to "fix" forever.

### `/action-feedback <path-to-report>`

Reads a feedback document (the `/review-pr` HTML report, or any markdown feedback file), **challenges each point on its merits** rather than taking it on faith, actions the ones it agrees with as real code changes, and declines the rest with concrete reasoning. It prints an `Actioned / Not actioned` report and copies it to the clipboard.

Paste that output back into the same chat that ran `/review-pr` and the review skill re-runs in **re-review mode**: it re-spawns the same roster against the new diff, and any declined item that a reviewer still stands behind becomes a **Pushback** entry at the top of the refreshed report.

## The full loop

```
1.  /review-pr <pr>
       Resolve PR ─► locate/clone repo ─► build project understanding (diff-blind)
       ─► spawn reviewers in parallel ─► read diff + synthesize ─► open HTML report

2.  /action-feedback ~/.cache/review-pr/<...>/report.html
       Read findings ─► challenge each ─► make the changes worth making
       ─► print "Actioned / Not actioned" ─► copy to clipboard

3.  Paste that output back into the /review-pr chat
       Re-review mode: re-spawn roster on the new diff ─► fill Pushback for
       declined-but-still-valid items ─► refresh the same report in place

4.  Repeat 2-3 until the verdict is 🟢 / 🟡, then let /review-pr
    wrap up and clean up its worktree + report.
```

The review skill deliberately stays **diff-blind** until after the sub-agents have run, so the reviewers form independent views and the final synthesis is not anchored by an early read of the diff.

## How `/review-pr` gets its agents

The reviewer roster is **data, not code**. Each reviewer is a single markdown file in [`skills/review-pr/reviewers/`](skills/review-pr/reviewers/). At review time the skill lists that folder, reads every `.md`, and spawns one sub-agent per reviewer whose frontmatter says `enabled: true`, all in parallel. Each sub-agent prompt is built from a shared envelope (the PR identifier, the clone path, the project understanding, instructions to fetch the diff itself, and a severity-tiering instruction) plus that file's body, which is the reviewer's role brief. The one exception is the **independent reviewer** (see below), which is deliberately given no project context at all.

The default roster:

| Reviewer | Slug | Looks for |
|---|---|---|
| 🔴 Doomsayer | `doomsayer` | Reasons not to merge: flaws, risks, whether it is needed at all |
| 🧊 Independent Reviewer | `independent` | A cold read with **no project context at all** (see below) |
| 🟢 Positive Reviewer | `positive` | Why the PR is worth shipping, with suggestions on the weak spots |
| 🔬 Code Quality Nitpicker | `nitpicker` | Duplication, abstraction opportunities, dead weight, tidiness |
| 🏛️ Architect | `architect` | Structural fit, boundaries, whether a better-shaped solution exists |
| 📋 Rule Stickler | `rules` | Compliance with the repo's own rule docs (CLAUDE.md, CONTRIBUTING, linters, etc.) |

Because the roster is just files, you tailor it to your codebase without touching the workflow.

### The independent reviewer

Every other reviewer is briefed with your project understanding. The **independent reviewer** (`independent: true` in its frontmatter) is deliberately not: its prompt is built with no agent memory, no external notes or knowledge base, and no maintainer brief. It may read what ships with the repo (the diff, the source, in-repo docs like `README`/`CLAUDE.md`), but it builds its own understanding from scratch. The point is to catch what context-primed reviewers rationalize away or never question because "that's just how this repo does it". It is enabled by default; skip it for a run like any other reviewer.

### Add a reviewer

Drop a new `.md` into `skills/review-pr/reviewers/`. It is picked up automatically on the next run, no other file changes. Template:

```markdown
---
name: Security Auditor       # display name on the report tab
slug: security              # tab id + report filename (reports/<slug>.md) + finding source tag; must be unique + filesystem-safe
emoji: "🔒"                  # shown on the tab and the panel header
order: 25                   # tab order, lower = further left
enabled: true               # false = never spawned unless you ask for it by name
feeds: blocking             # soft hint for the usual severity tier: blocking | material | optional | green | any (optional)
independent: false          # optional; true = spawned with NO project context (a fresh-eyes reviewer)
role: authn, secrets, input validation   # short subtitle under the panel header (optional)
---

Role: security voice. Goal is to find ways this change could be exploited or leak data.

Cover:
- **Input handling** - injection, unsafe deserialization, path traversal, SSRF.
- **Secrets & auth** - hardcoded credentials, weakened auth checks, over-broad scopes.
- **Data exposure** - sensitive data in logs, responses, or error messages.

Reinforce: every finding must map to a concrete attack path with a quoted file:line. No theoretical hand-waving.
```

The **body** (everything after the frontmatter) is pasted verbatim into the sub-agent prompt, so write it as a self-contained brief. The sub-agent never sees this skill or the other reviewers.

### Remove or disable a reviewer

- **Permanently:** delete the file, or set `enabled: false` in its frontmatter.
- **For one run:** just tell Claude in the chat, e.g. "skip the nitpicker this time" or "only run the doomsayer and the rule stickler". The skill honours natural-language subsetting; there is no flag to learn.

### Reorder

Change the `order` value. The Synthesis tab is always first regardless.

## Azure DevOps support

`/review-pr` works against Azure DevOps PRs as well as GitHub. The review engine (roster, synthesis, report) is identical; only the plumbing differs:

- Paste a `dev.azure.com/<org>/<project>/_git/<repo>/pullrequest/<N>` URL and the skill detects the provider automatically.
- GitHub uses the `gh` CLI. ADO uses the REST API with a short-lived bearer token minted via `az account get-access-token` (the skill walks through this, including a guest-tenant fallback when your account is a guest in the org).
- ADO has no convenient raw-content API, so a local clone matters more there; the skill fetches both PR commit SHAs into the clone and diffs locally.

The full provider command map (auth, metadata, file list, clone, diff, CI status) is in the "Provider reference" section of [`skills/review-pr/SKILL.md`](skills/review-pr/SKILL.md).

## Install

These are Claude Code skills. Copy each folder into your skills directory so Claude Code discovers them:

```bash
git clone https://github.com/suiyangqiu/claude-pr-review-loop.git
cp -r claude-pr-review-loop/skills/review-pr      ~/.claude/skills/
cp -r claude-pr-review-loop/skills/action-feedback ~/.claude/skills/
```

Then in any Claude Code session: `/review-pr <pr-url>`.

> Note: a few paths inside `review-pr/SKILL.md` reference `~/.claude/skills/review-pr/...` (for `assemble.py` and the reviewers folder). If you install somewhere else, adjust those paths to match.

### Requirements

- [Claude Code](https://claude.com/claude-code)
- `python3` (stdlib only, for `assemble.py`; no build step)
- GitHub reviews: the [`gh` CLI](https://cli.github.com/), authenticated (`gh auth status`)
- Azure DevOps reviews: the [`az` CLI](https://learn.microsoft.com/cli/azure/), logged in (`az login`)
- macOS for the clipboard step in `/action-feedback` (`pbcopy`) and `open` to auto-launch the report; swap these for your platform's equivalents if you are elsewhere

## Repo layout

```
skills/
  review-pr/
    SKILL.md              # the full review + re-review workflow (GitHub + Azure DevOps)
    reviewers/            # one .md per reviewer = the data-driven roster
      doomsayer.md
      independent.md
      positive.md
      nitpicker.md
      architect.md
      rule-stickler.md
    assemble.py           # builds the tabbed HTML report from reviews + synthesis.json
    report-template.html  # the report shell (editorial CSS + structure)
  action-feedback/
    SKILL.md              # the feedback-actioning companion
```

## License

MIT. See [LICENSE](LICENSE).
