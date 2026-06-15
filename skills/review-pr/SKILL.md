---
name: review-pr
description: Multi-agent review of a GitHub or Azure DevOps PR. Build project context first, spawn a roster of specialist reviewers (defined as files under reviewers/ - doomsayer, positive, code-quality nitpicker, architect, rule stickler, independent reviewer by default) in parallel, synthesize a verdict graded by severity tiers, then open a tabbed HTML report for a human reviewer. Supports a re-review mode when paired with `/action-feedback` output. Trigger when the user asks for a PR review, says "review this PR", "can you review", "give feedback on this PR", or shares a github.com or dev.azure.com PR URL and asks for thoughts.
argument-hint: <pr-url-or-number>
---

# Review PR: $ARGUMENTS

## When to use

Auto-trigger on any of:
- "review this PR" / "can you review" / "give feedback on this PR" / "look at this PR"
- A GitHub PR URL (`github.com/<org>/<repo>/pull/<N>`) shared with a request for thoughts/feedback
- An Azure DevOps PR URL (`dev.azure.com/<org>/<project>/_git/<repo>/pullrequest/<N>`) shared with a request for thoughts/feedback
- "PR review please" / "review #<N>" when a repo context is obvious

This skill works against **two providers** - GitHub (via the `gh` CLI) and Azure DevOps (via the REST API + a bearer token). The review engine (roster, synthesis, report) is identical for both; only the plumbing commands differ. Every step below is written for GitHub; wherever a step uses a `gh`/`git`/`api.github` command, the **Provider reference** section near the end gives the Azure DevOps equivalent. Detect the provider once in Step 1 and use the matching column throughout.

If the user only asks a question about a PR (e.g. "what does this PR change?") without asking for review/feedback, do NOT auto-run this skill — just answer the question.

There is also a **re-review mode** triggered when `/action-feedback` output is pasted into the same chat after a `/review-pr` run. See the "Re-review mode" section at the end.

## Process overview

```
Step 1  Resolve the PR
Step 2  Pre-flight: locate or clone the repo, surface state warnings
Step 3  Build project understanding   (no diff read yet)
Step 4  Clarify with the user         (if anything is genuinely unclear)
Step 5  Load the reviewer roster + spawn the selected reviewers in PARALLEL
Step 6  Read the diff yourself + synthesize a verdict
Step 7  Write reports + synthesis.json, run assemble.py, open the report
Step 8  Wait for the human; offer to post a comment; clean up
```

The reviewer roster is **data, not hard-coded**: each reviewer is a file under `~/.claude/skills/review-pr/reviewers/`. The default roster is **doomsayer**, **positive reviewer**, **code-quality nitpicker**, **architect**, **rule stickler**, and **independent reviewer**. They run in parallel and each forms an independent view. The **independent reviewer** is special: it is spawned with NO project context (no agent memory, no external notes/knowledge base, no project brief) so it reviews with genuinely fresh eyes - see Step 5.3 and the "Reviewer roster" section for how that works. See that section near the end for the file format and how to add/disable one.

The main agent does NOT read the PR diff until Step 6. Steps 3-5 are deliberately diff-blind so the subagents form independent views and the synthesis isn't anchored by an early read.

---

### Step 1: Resolve the PR (provider-aware)

First decide the **provider** from `$ARGUMENTS`, then parse the identifier:

- **GitHub** - the URL contains `github.com`, or the argument is short-form/bare-number. Extract `org`, `repo`, PR number. Accept:
  - Full URL: `https://github.com/acme/widgets/pull/99`
  - Short form: `acme/widgets#99`
  - Bare number: `#99` (only if the working directory is a clone of the target repo)
- **Azure DevOps** - the URL contains `dev.azure.com` (or `visualstudio.com`). Extract `org`, `project`, `repo`, PR number from:
  - `https://dev.azure.com/<org>/<project>/_git/<repo>/pullrequest/<N>`
  - (older host) `https://<org>.visualstudio.com/<project>/_git/<repo>/pullrequest/<N>`

  ADO needs the extra `project` segment that GitHub doesn't have - capture it; every ADO REST call needs it.

Set `PROVIDER` to `github` or `azure-devops` and carry it through every later step. From here on, wherever a step shows a `gh`/`api.github` command, use the matching row of the **Provider reference** section instead when `PROVIDER` is `azure-devops`.

If the provider or identifier is ambiguous, ask for the full PR URL before proceeding.

---

### Step 2: Pre-flight repo check

A reliable review needs the actual source files, not just the diff. Sort out the local clone before going further.

**2a — Locate the clone.** In order:
1. Check your notes/memory for a known local path for `<org>/<repo>`, if you keep one
2. Otherwise, glob the roots where you usually keep clones, e.g.:
   ```bash
   ls -d ~/Documents/<repo> ~/code/<repo> ~/<repo> 2>/dev/null
   ```
3. If nothing turns up, the clone is missing.

**2b — If no clone exists, offer to clone.** Use `AskUserQuestion`:
- "I don't have a local clone of `<org>/<repo>`. Want me to clone it?"
- Options: `~/Documents/<repo>` · `~/code/<repo>` · Skip (review via API only)

If the user picks a location, clone with `gh repo clone <org>/<repo> <chosen-path>`. If they skip, set a flag for Steps 3 and 5: file reads must use `gh api` raw content, and surrounding-file context will be limited.

> **Azure DevOps:** clone and fetch via the bearer-token git commands in the Provider reference (a missing clone is a much bigger deal on ADO - there is no convenient raw-content API, so reading files locally is the only good path; strongly prefer cloning over skipping). After locating/cloning, **fetch both PR commits into the clone** (`source` and `target` SHAs from the PR JSON) so the subagents and Step 6 can diff locally without a token.

**2c — If the clone exists, check its state.** From inside the repo:
```bash
git -C <clone-path> status --short
git -C <clone-path> branch --show-current
git -C <clone-path> rev-parse --abbrev-ref origin/HEAD | sed 's@^origin/@@'
```

Surface as a warning (don't block) if:
- Current branch is not the default branch, OR
- `git status --short` has any output (uncommitted changes / untracked files)

Tell the user in one line: e.g. "Heads-up: your clone is on branch `feature/foo` with 3 uncommitted files. Surrounding-file context will reflect that state, not the PR. Want me to `gh pr checkout <N>` first?" — then wait briefly for them to react before continuing.

If they say "just proceed", continue. If they say "checkout the PR", run `gh pr checkout <N> --repo <org>/<repo>` from the clone path (ADO: `git -C <clone> checkout <sourceBranch>` after the token-authed fetch - see Provider reference). Do not stash or modify their working tree silently.

---

### Step 3: Build project understanding (no diff yet)

Goal: understand the repo as a whole before looking at the change. You're building enough context to brief the reviewer subagents.

In parallel where possible:
- `gh repo view <org>/<repo> --json name,description,defaultBranchRef,languages,topics,url` for high-level metadata (ADO: the repo GET endpoint in the Provider reference - note ADO returns no languages/topics, so lean on reading the actual repo files below)
- `ls` the repo root and read top-level docs (`README.md`, `CLAUDE.md`/`AGENTS.md`, `package.json`/`pyproject.toml`/`go.mod`, etc.)
- If you keep a project knowledge base (a notes vault, a `docs/` folder, etc.), read the relevant project's notes for extra context
- For larger or unfamiliar codebases, use the Explore subagent (breadth: "medium") to map structure
- If Step 2 left you without a local clone, fall back to `gh api` raw content for README + obvious config files (ADO has no convenient raw-content equivalent - if there's no clone, say so and keep context to the diff + PR description)

By the end of this step you should be able to answer, in your own words:
- What is this project for, in one sentence?
- What stack and architecture does it use?
- What conventions does it follow (testing approach, file layout, naming)?
- Which area of the codebase does this PR touch? (Touched paths come from `gh pr view --json files` on GitHub, or the PR `iterations/<id>/changes` endpoint on ADO - either gives the file list without reading diff content.)

**Do NOT read the PR diff during this step.** Files-changed metadata is fine; diff content is not.

---

### Step 4: Clarify with the user (if needed)

If anything is genuinely ambiguous after Step 3 (unfamiliar stack, undocumented purpose, unclear which subsystem the PR touches), use `AskUserQuestion` to clarify before spawning subagents. Skip this step entirely if you have a strong understanding — don't ask for the sake of asking.

---

### Step 5: Load the roster and spawn the reviewers in PARALLEL

**5.1 — Load the roster.** `ls ~/.claude/skills/review-pr/reviewers/` and read every `.md` file. Each file is one reviewer: YAML frontmatter (`name`, `slug`, `emoji`, `order`, `enabled`, optional `feeds`, `role`) followed by a body holding that reviewer's role brief.

**5.2 — Decide who runs.** Select every reviewer with `enabled: true`, MINUS any the user has asked to skip in this conversation. There is no command-line flag — honour natural-language requests like "skip the nitpicker this time", "don't run the architect", or "only run the doomsayer and the rule stickler". If the user named a subset to run, run exactly that subset. Tell the user in one line which reviewers you're running if it differs from the full enabled roster.

**5.3 — Spawn them.** Send one `Agent` call per selected reviewer, **all in a single message** (parallel). Use `subagent_type: general-purpose` for all. Each prompt is self-contained — the subagent has no view of this conversation — and is built as **the shared envelope + that reviewer file's body**:

**Shared envelope** (prepend to every reviewer's role brief, verbatim intent):
- The PR identifier (`<org>/<repo>#<N>`) and URL
- The local clone path (so they can read surrounding files), or a note that no clone is available
- Your Step 3-4 project understanding (paste it; don't paraphrase to one line). **EXCEPTION - the independent reviewer:** if the reviewer's file has `independent: true` in its frontmatter, do NOT paste your project understanding and do NOT hand it any agent memory, external notes, or knowledge-base context. Instead tell it plainly: "No project brief is provided on purpose. Build your own understanding from the repo (you may read the diff, the source, and in-repo docs like README/CLAUDE.md), but do not consult any external notes, memory, or vault." Its file body already states the cold-eyes constraints; the envelope just withholds the context the others get.
- An explicit instruction to fetch the diff themselves and read surrounding files in the local clone where useful. **GitHub:** tell them to run `gh pr view` + `gh pr diff`. **Azure DevOps:** subagents can't use `gh` and shouldn't juggle tokens - instead give them the clone path plus the two commit SHAs (`target` and `source` from the PR JSON, already fetched into the clone in Step 2) and tell them to run the merge-base (three-dot) diff `git -C <clone> diff <targetSha>...<sourceSha>` for the diff. **Use three dots, not two** - a two-dot `diff <targetSha> <sourceSha>` includes target-branch commits the feature lacks (reversed) whenever the branch is behind its target, polluting the diff; the three-dot form diffs from the merge-base so it shows only what the PR introduces. This keeps you (main agent) diff-blind until Step 6 while still letting each reviewer self-serve.
- **Severity tiering (every reviewer).** Tell each reviewer to assign one tier to every issue it raises, with a one-line justification:
  - **Blocking** - a correctness bug, security hole, data loss, broken build/tests, or an approach that should not merge as-is. Concrete, demonstrable harm.
  - **Material** - should be fixed before merge but does not invalidate the approach: a missing test for new logic, a real unhandled edge case, a meaningful maintainability problem, a clear written-rule violation.
  - **Optional** - polish, nits, naming, light duplication, forward-looking debt, stylistic preference. Fine to merge without.
  Instruct them: **default to Optional** unless you can name concrete harm that lifts a finding to Material or Blocking. Do not inflate tiers to seem thorough. (You re-tier everything yourself in Step 6; their tags are signal, not the final call.)
- A request for a markdown report, ~500 words, structured with `##`/`###` headings and bullet lists, with quoted `file:line` and a `[Blocking]`/`[Material]`/`[Optional]` tag on each finding where applicable. The report is shown **verbatim** in this reviewer's own tab, so it must read well standalone.

**Role brief**: paste the body of the reviewer's `.md` file (everything after the frontmatter). That's the reviewer's personality and focus; don't rewrite it.

The roster is a mix of lenses: doomsayer hunts blocking problems, positive feeds the green flags, nitpicker/architect/rule-stickler each sharpen one axis, and the independent reviewer brings a context-free cold read. Every report is preserved verbatim in its own tab, and its findings are also folded into the consolidated severity-tier buckets in Step 6, tagged by the reviewer's `slug`.

**5.4 — Collect.** Wait for all selected reviewers to finish. Keep each full markdown report in context — you'll write them to disk in Step 7. Note each report's `slug` (from its reviewer file) so you can name the files correctly.

---

### Step 6: Read the diff and synthesize (main agent)

**This is the first time you (the main agent) look at the diff.** Bring every subagent report with you.

**GitHub:**
1. `gh pr view <N> --repo <org>/<repo> --json title,body,state,author,files,additions,deletions,commits,baseRefName,headRefName,url`
2. `gh pr diff <N> --repo <org>/<repo>`
3. `gh pr checks <N> --repo <org>/<repo>` (CI status)

**Azure DevOps** (see Provider reference for the exact commands):
1. The PR GET endpoint for metadata (title, description, status, isDraft, createdBy, source/target refs, the two merge-commit SHAs).
2. `git -C <clone> diff <targetSha>...<sourceSha>` for the diff (three-dot / merge-base, same command the subagents used - see the note in the Diff row of the Provider reference).
3. The policy-evaluations endpoint for CI/branch-policy status - it's optional and can be fiddly (needs the project GUID); if it doesn't resolve cleanly, set `ci_status` to `"unknown"` and move on rather than blocking the review.

Then, for both providers:
4. Read all the subagent reports with the diff open. Where they disagree, use your own judgement.
5. **Preserve each report verbatim.** In Step 7 you'll write each report to `$REPORT_DIR/reports/<slug>.md` unchanged — assemble.py renders each into its own tab. Don't summarise them away; the tabs are meant to show each agent's own voice.
6. **Re-tier every finding, then derive the verdict from the tiers.** The reviewers each proposed a tier; with the diff open, you make the final call. The three tiers (same definitions the reviewers used):
   - **Blocking** - correctness bug, security hole, data loss, broken build/tests, or an approach that should not merge as-is. Demonstrable harm.
   - **Material** - should be fixed before merge but does not invalidate the approach: a missing test for new logic, a real unhandled edge case, a meaningful maintainability problem, a clear written-rule violation. The author would reasonably be expected to address it.
   - **Optional** - polish, nits, naming, light duplication, forward-looking debt, stylistic preference. Genuinely fine to merge without. **This is the default tier** - a finding stays Optional unless you can articulate concrete harm that lifts it.

   Then pick exactly one verdict, derived **mechanically from the worst-populated tier** (this is what makes the verdict reproducible rather than a vibe):
   - 🔴 **Reject** - a Blocking item that makes the whole approach wrong or unneeded.
   - 🟠 **Request changes** - one or more (fixable) Blocking items.
   - 🟡 **Approve with minor changes** - no Blocking, but at least one Material item.
   - 🟢 **Approve and merge** - no Blocking and no Material. Optional items and green flags never hold up a merge.
7. Bucket your synthesized findings into the four buckets, which **are** the tiers, drawing from **all** the reviewers (not one bucket per reviewer):
   - **Blocking** / **Material** / **Optional** - your re-tiered negative findings.
   - **Green flags** - the positive reviewer's strongest points you agree with. Never affects the verdict.
   - **Tag every finding with its source reviewer** (the reviewer's `slug`) so the human can trace it back to the tab — this becomes the `src` field in `synthesis.json` (e.g. `architect`, `nitpicker`, `rules`, `doomsayer`, `positive`, `independent`). If you reached a finding by combining reviewers or via your own read, tag it `synthesis`. A reviewer's `feeds` frontmatter is a soft hint for its usual tier, not a rule - tier by actual severity.

Don't pad. Empty buckets are fine (assemble.py renders a single "None" row). A finding raised by a reviewer that you disagree with after reading the diff should be dropped from the buckets (it still lives in that reviewer's tab) - or surfaced as a Callout if the judgement is worth explaining.

**On "when is it done".** 🟢 means no Blocking and no Material issues remain - NOT that the PR is flawless. A later fresh review (one with no memory of this chat, e.g. the independent reviewer, or a brand-new `/review-pr` run) will almost always surface new Optional-tier nits, because any reviewer asked to find issues will find some. That is expected and is **not** a reason to keep iterating. Once a pass turns up only Optional items, the PR is merge-ready; treat the long tail of nits as the author's discretion, not a gate. Specifically: do **not** promote an Optional nit to Material just because it is the only thing left to flag. The tiers exist precisely so the loop converges instead of finding fresh things to "fix" forever.

---

### Step 7: Write the reports + synthesis.json, assemble, and open

The report is **assembled by a script**, not by hand-editing HTML. You produce two kinds of input — the verbatim per-reviewer reports and a single `synthesis.json` — then run `assemble.py`.

**7.1 — Create the report dir.**

```bash
TS=$(date +%Y%m%d-%H%M%S)
REPORT_DIR=~/.cache/review-pr/<org>-<repo>-<N>-$TS
mkdir -p "$REPORT_DIR/reports"
```

**Remember the value of `$REPORT_DIR` for the rest of the chat** — re-review mode (below) re-runs assemble against this same dir.

**7.2 — Write each reviewer's report verbatim.** For every reviewer that ran, write its full markdown report to `$REPORT_DIR/reports/<slug>.md` (use the `Write` tool; `<slug>` is the reviewer's frontmatter slug, e.g. `doomsayer.md`, `rules.md`). Raw markdown, exactly as the subagent returned it — no HTML, no escaping, no trimming. assemble.py renders it. Only reviewers that have a file here get a tab, so this is also how a skipped reviewer simply doesn't appear.

**7.3 — Write `synthesis.json`.** Write `$REPORT_DIR/synthesis.json` with the `Write` tool. Schema:

```json
{
  "pr": {
    "title": "Add typed retry policy",
    "url": "https://github.com/acme/widgets/pull/412",
    "repo": "acme/widgets",
    "number": "412",
    "author": "octocat",
    "additions": 318,
    "deletions": 42,
    "files": 7,
    "base_ref": "main",
    "head_ref": "feat/retry-policy",
    "ci_status": "passing",
    "timestamp": "2026-05-28 16:40"
  },
  "verdict": {
    "class": "verdict-minor",
    "text": "🟡 Approve with minor changes",
    "rationale": "One or two sentences explaining the call.",
    "purpose": "Your one-sentence recap of what the PR does and why."
  },
  "buckets": {
    "blocking": [],
    "material": [ {"src": "architect", "label": "Retry logic in the wrong layer.", "body": "Belongs in transport, not jobs.", "loc": "src/jobs/runner.ts:77"} ],
    "optional": [ {"src": "nitpicker", "label": "Backoff math duplicated 3x.", "body": "Extract a shared helper.", "loc": "src/net/retry.ts:30"} ],
    "green":    [ {"src": "positive", "label": "Closes a real reliability gap.", "body": "Transient 503s no longer reach users.", "loc": ""} ]
  },
  "pushback": [],
  "callouts": []
}
```

Field rules:
- The four `buckets` keys are exactly `blocking`, `material`, `optional`, `green` - they are the severity tiers plus the positive bucket. (Older `red`/`minor`/`future` keys are gone.)
- `verdict.class` is one of `verdict-merge` / `verdict-minor` / `verdict-changes` / `verdict-reject`; `verdict.text` is the matching label (`🟢 Approve and merge`, `🟡 Approve with minor changes`, `🟠 Request changes`, `🔴 Reject`). Derive it from the buckets per Step 6: any `blocking` -> `verdict-changes` (or `verdict-reject` if fundamental); else any `material` -> `verdict-minor`; else (only `optional`/`green`) -> `verdict-merge`.
- Each finding object: `src` (reviewer slug, or `synthesis`), `label` (short bold lead, ends with a period), `body` (one or two sentences), `loc` (a single `file:line` or `""`). assemble.py escapes all of these and renders `loc` as inline code — so put plain text in `label`/`body`, no HTML or markdown.
- Empty bucket → `[]` (assemble.py renders "None.").
- `pushback` and `callouts` are arrays of `{label, body, loc}` (no `src`). First-review runs leave both as `[]`. See the Callouts and Re-review sections for when to fill them.
- No em/en dashes anywhere — regular hyphens.

**7.4 — Assemble and open.**

```bash
python3 ~/.claude/skills/review-pr/assemble.py "$REPORT_DIR"
open "$REPORT_DIR/report.html"
```

assemble.py reads the reviewer roster (for each tab's name/emoji/order/role), every `reports/<slug>.md`, and `synthesis.json`, then writes `report.html`. If it errors (e.g. malformed JSON), fix the input and re-run — don't hand-edit the HTML.

Then tell the user, in one or two sentences: the verdict, that the report is open at `<path>` (Synthesis tab plus one tab per reviewer that ran), and to let you know when they're done. Also remind them the report can be fed to `/action-feedback` if they want the author agent to action it.

---

### Step 8: Wait for the human, then offer to post + clean up

The user will come back when they've finished their own review (signals: "done", "finished", "all good", "you can clean up", etc.).

When they do:

1. **Offer to post a synthesized comment.**
   - **GitHub** - ask exactly:
     > Would you like me to post a synthesized `## 🤖 Agent Review` comment on the PR before I clean up?
   - **Azure DevOps** - comment-posting is intentionally not wired for ADO. Instead say:
     > Heads-up: I don't post comments on Azure DevOps PRs. I can render the synthesized `## 🤖 Agent Review` comment here for you to paste into the PR yourself - want that?

2. **If yes**, render the comment in the format below and show it in chat. **GitHub:** ask for confirmation, then post:
   ```bash
   gh pr comment <N> --repo <org>/<repo> --body "$(cat <<'EOF'
   <the rendered review>
   EOF
   )"
   ```
   **Azure DevOps:** just print the rendered comment in a copy-friendly block - do not attempt to post it.

3. **Always clean up**, even if they declined the comment:
   ```bash
   rm -rf "$REPORT_DIR"
   ```
   Confirm cleanup to the user in one line.

#### Comment format (Step 8 only)

```markdown
## 🤖 Agent Review
### 🚨 Must Fix / Address
- ✌️ **None** - <reason> _(or real items if any Blocking items held up merge)_

### ⚠️ Notable Changes
- <emoji> **<bold label>** - <one or two sentences>
- **<bold label>** - <one or two sentences>

### 📞 Callouts
- **<bold label>** - <one or two sentences>

### 🤏 Small Feedback
- **<bold label>** - <one or two sentences>
```

Formatting rules (strict):
1. One emoji per section, on the most important bullet only. Other bullets in the section have no leading emoji.
2. For "Must Fix" with nothing to block merge, the lone "None" bullet gets ✌️.
3. One or two sentences per bullet, no paragraphs.
4. No blank lines between bullets within a section.
5. No em dashes anywhere — use a regular hyphen.
6. No `Made by @<user>` footer in the comment — the user adds that themselves.
7. Map the severity tiers to sections: **Blocking -> Must Fix / Address**, **Material -> Notable Changes**, **Optional -> Small Feedback**; Callouts stay in Callouts. If there are no Blocking items (verdict 🟢 or 🟡), the Must Fix section is just the ✌️ None bullet.

---

## Callouts (optional, both modes)

The Callouts section is for **your own** notes to the human reviewer — judgement calls you made during synthesis that you want surfaced rather than absorbed silently. It is most useful in re-review mode but also valid in first-review.

Use Callouts when:
- A subagent flagged something and you decided NOT to escalate it to Pushback / Blocking / Material, but you want the human to see your reasoning rather than have to guess at it.
- Two or more subagents disagreed and you sided with one — explain which and why, especially if a future maintainer reading the report wouldn't otherwise know there was disagreement.
- You made a deliberate scope cut (e.g. "didn't review the test fixture changes because they're regenerated from a tool") and want the human to verify the cut.
- You couldn't access something (offline clone, gated diff, missing tool) and the synthesis is partial.

Do NOT use Callouts as a dumping ground. If a point belongs in Blocking / Material / Optional / Pushback, put it there. Callouts is specifically for **meta-commentary about the synthesis itself**, not for additional findings.

To use Callouts, fill the `callouts` array in `synthesis.json` with `{label, body, loc}` objects (same shape as a finding, minus `src`). If there are no callouts, leave it `[]` — assemble.py renders no section at all rather than an empty box.

```json
"callouts": [
  {"label": "Architect vs author.", "body": "Architect wants retry moved to transport; flagging so you can weigh the refactor cost.", "loc": ""}
]
```

Both Pushback and Callouts render at the top of the **Synthesis tab** (below the always-visible PR header and tab strip, above the verdict rationale). Callouts renders below Pushback when both are present.

---

## Re-review mode

After a `/review-pr` run, the user may paste the output of `/action-feedback` back into the same chat. That output looks like:

```
### Actioned
- ...

### Not actioned
- <item> — <reasoning for not actioning>
```

When you see this **in the same chat** that already ran `/review-pr` (i.e. you still have the original verdict, the subagent reports, and `$REPORT_DIR` in your context), treat it as a re-review trigger. Don't ask whether to proceed — just go.

### Re-review flow

1. **Fetch fresh PR state** — the head ref likely has new commits:
   ```bash
   gh pr view <N> --repo <org>/<repo> --json title,body,state,author,files,additions,deletions,commits,baseRefName,headRefName,url
   gh pr diff <N> --repo <org>/<repo>
   gh pr checks <N> --repo <org>/<repo>
   ```
   If the clone is on the PR branch, `git -C <clone-path> pull` to sync.

   **Azure DevOps:** re-hit the PR GET endpoint for the new merge-commit SHAs, re-fetch both into the clone with the token-authed `git fetch` (Provider reference), then `git -C <clone> diff <newTargetSha>...<newSourceSha>` (three-dot / merge-base). The SHAs move between iterations, so always re-read them from the fresh PR JSON rather than reusing the first pass's.

2. **Re-spawn the same roster in parallel** (the same reviewers that ran the first time — respect any earlier skips, and any new skip the user voices now). Rebuild each prompt as the shared envelope + that reviewer's file body (the independent reviewer's envelope still withholds project context per Step 5.3; it may see its own prior report, just not external context), plus four extra inputs:
   - That reviewer's own original report from the first pass (the doomsayer gets the doomsayer report, the architect gets the architect report, etc.)
   - The `/action-feedback` output (Actioned + Not actioned)
   - The fresh diff
   - An explicit instruction: "For each 'Not actioned' item in your domain, decide whether you still agree the change was warranted. If yes, list it as **pushback** with reasoning. Also reassess your original points against the new diff — drop anything that's now addressed."

3. **Re-read the fresh diff yourself** with all the new reports in hand.

4. **Identify Pushback items** — these are items where:
   - The action-feedback agent declined to make a change, AND
   - At least one subagent re-flagged it, AND
   - You (main agent) agree the original concern still stands after reading the new diff and the author's reasoning

   If the reasoning was actually convincing, drop it. Don't insist for insistence's sake.

   **If a subagent re-flagged something but you decided not to escalate it to Pushback**, that is exactly the case for a Callout (see Callouts section above). Drop-vs-pushback is a judgement call; surfacing the judgement to the human is better than absorbing it silently. Example wording: "Doomsayer re-flagged X on the second pass. I'm holding it as Optional rather than Pushback because the failure mode is unobserved and the author's scope cut is sound - but flagging here so you can override if you disagree."

5. **Re-derive the verdict from the current tiers** using Step 6's mechanical rule. If the only findings left are Optional, the verdict is 🟢 - say so plainly and do NOT invent Material items to justify another round. Converging on 🟢 with a short Optional list (or none) is the expected, correct end state of the loop, not a sign you missed something.

6. **Rebuild the report in place** — same `$REPORT_DIR` from the original run:
   - Overwrite each `$REPORT_DIR/reports/<slug>.md` with that reviewer's **fresh** second-pass report (verbatim).
   - Rewrite `$REPORT_DIR/synthesis.json` with the new verdict, re-bucketed findings, and the `pushback` array filled. Each pushback item is `{label, body, loc}` — `label` a short lead, `body` what the author said + why you still disagree. Fill `callouts` too if you held something back (see Callouts).
   - Re-run `python3 ~/.claude/skills/review-pr/assemble.py "$REPORT_DIR"`.

   Pushback renders as a highlighted section at the top of the Synthesis tab. Leave `pushback` as `[]` if there's nothing to push back on (no section rendered).

7. **Re-open the report**:
   ```bash
   open "$REPORT_DIR/report.html"
   ```
   Tell the user one or two sentences: the new verdict, any pushback / callouts count, and that the report has been refreshed.

8. **Continue with Step 8** (wait for "done", offer to post comment, clean up) when the user is finished.

If `$REPORT_DIR` is no longer in your context (e.g. the conversation was compacted), create a fresh timestamped directory under `~/.cache/review-pr/` like a normal run.

---

## Provider reference (GitHub vs Azure DevOps)

The review engine is provider-agnostic. Only the plumbing differs. GitHub uses the `gh` CLI as shown inline in each step. This section is the **Azure DevOps** equivalent for every `gh`-coupled touchpoint. Use it whenever `PROVIDER` is `azure-devops`.

### Auth (run once, reuse for the whole review)

ADO has no `gh`. Authenticate with a short-lived bearer token for the universal Azure DevOps resource (the GUID `499b84ac-1321-427f-aa17-267ca6975798` is the same for every ADO org):

```bash
# Default az context first.
TOKEN=$(az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv 2>/dev/null)
```

**Guest-tenant fallback:** if your account is a *guest* in the ADO org you're reviewing, the default `az` context often can't see it, and the call above returns 401/403. Re-mint the token against that org's tenant (find the tenant id in the Azure portal, or via `az account list`):

```bash
TOKEN=$(az account get-access-token --tenant <TENANT_ID> --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv 2>/dev/null)
```

Keep the token in a shell variable only - **never write it to a file**. Use it inline as `-H "Authorization: Bearer $TOKEN"`. The token expires in ~1h; if a later call 401s, just re-run the line. (The token is also the credential for the git operations below, via `http.extraHeader` - a `visualstudio.com` URL drops the header on redirect, so always use the `dev.azure.com` form.)

### Command map

`$O`=org, `$P`=project, `$R`=repo, `$N`=PR number, `$BASE="https://dev.azure.com/$O/$P/_apis/git/repositories/$R"`.

| Touchpoint (step) | GitHub | Azure DevOps |
|---|---|---|
| **PR metadata** (3, 6, re-review) | `gh pr view ... --json ...` | `curl -s -H "Authorization: Bearer $TOKEN" "$BASE/pullRequests/$N?api-version=7.1"` |
| **File list** (3) | `gh pr view --json files` | `curl -s -H "Authorization: Bearer $TOKEN" "$BASE/pullRequests/$N/iterations?api-version=7.1"` -> take the latest iteration id, then `.../pullRequests/$N/iterations/<id>/changes?api-version=7.1` |
| **Clone** (2) | `gh repo clone $O/$R <path>` | `git -c http.extraHeader="Authorization: Bearer $TOKEN" clone "https://dev.azure.com/$O/$P/_git/$R" <path>` |
| **Fetch PR commits** (2) | (n/a - `gh pr checkout`) | `git -C <clone> -c http.extraHeader="Authorization: Bearer $TOKEN" fetch origin <sourceBranch> <targetBranch>` (branch names = `sourceRefName`/`targetRefName` minus `refs/heads/`) |
| **Diff** (5 subagents, 6) | `gh pr diff $N` | `git -C <clone> diff <targetSha>...<sourceSha>` (THREE dots) where `targetSha=lastMergeTargetCommit.commitId`, `sourceSha=lastMergeSourceCommit.commitId` from the PR JSON. Three-dot diffs from the merge-base, so it shows only the PR's changes; a two-dot `diff <targetSha> <sourceSha>` is WRONG when the branch is behind its target (it folds in target-ahead commits, reversed) and can balloon the file count. |
| **CI / checks** (6) | `gh pr checks $N` | *optional* - branch-policy evaluations: `curl -s -H "Authorization: Bearer $TOKEN" "https://dev.azure.com/$O/$P/_apis/policy/evaluations?artifactId=vstfs:///CodeReview/CodeReviewId/<projectId>/$N&api-version=7.1"`. Needs `<projectId>` (= `repository.project.id` from the PR JSON). If it doesn't resolve, set `ci_status` to `"unknown"`. |
| **Repo metadata** (3) | `gh repo view ... --json ...` | `curl -s -H "Authorization: Bearer $TOKEN" "$BASE?api-version=7.1"` (name, defaultBranch, webUrl; no languages/topics) |
| **Post comment** (8) | `gh pr comment` | **Not supported** - render the comment for manual paste; never post. |

### Mapping the ADO PR JSON to `synthesis.json`

The `pr` block fields come from the PR GET response plus a `git diff --numstat`:

- `title` <- `.title`
- `number` <- `.pullRequestId`
- `author` <- `.createdBy.displayName` (or `.createdBy.uniqueName`)
- `base_ref` <- `.targetRefName` minus `refs/heads/`
- `head_ref` <- `.sourceRefName` minus `refs/heads/`
- `url` <- `https://dev.azure.com/$O/$P/_git/$R/pullrequest/$N` (the web URL, NOT the `_apis` URL)
- `repo` <- `$O/$R` (include the project if useful: `$O/$P/$R`)
- `additions` / `deletions` / `files` <- derive from `git -C <clone> diff --numstat <targetSha>...<sourceSha>` (three-dot; sum columns 1 and 2; count lines for files)
- `ci_status` <- from policy evaluations if resolved, else `"unknown"`
- If `.isDraft` is `true`, note "(draft)" in `verdict.purpose` so the human knows it's not merge-ready.

Everything downstream (reviewer reports, buckets, assemble.py, the HTML) is identical to the GitHub path.

---

## Reviewer roster

Reviewers live as individual files in `~/.claude/skills/review-pr/reviewers/`, one `.md` per reviewer. This is how you enable, disable, add, or edit a perspective without touching the workflow.

**File format** — frontmatter + body:

```markdown
---
name: Code Quality Nitpicker      # display name on the tab
slug: nitpicker                   # tab id + report filename (reports/<slug>.md) + finding `src` tag
emoji: "🔬"                        # shown on the tab and the panel header
order: 30                         # tab order (lower = further left)
enabled: true                     # false = never spawned unless the user explicitly asks for it
feeds: optional                   # soft hint for the usual severity tier (blocking|material|optional|green|any); optional
independent: false                # optional; true = spawned with NO project context (see below)
role: duplication, abstraction, tidiness   # short subtitle under the panel header; optional
---

Role: <the reviewer's personality and what it cares about>.

Cover:
- **<focus area>** - ...
- ...

Reinforce: <the guardrails - e.g. "be reasonable, not pedantic">.
```

The **body is the role brief** pasted into the subagent prompt in Step 5 (after the shared envelope). Keep it self-contained; the subagent never sees this skill or the other reviewers.

- **Disable one**: set `enabled: false` (or tell the agent to skip it for a single run).
- **Add one**: drop a new `.md` in the folder with a unique `slug` and a sensible `order`. It's picked up automatically next run — no other file needs editing.
- **Reorder**: change `order`. Synthesis always renders as the first/default tab regardless.
- `slug` must be unique and filesystem-safe (it's used for `reports/<slug>.md`, the tab/panel id, and the finding `src` tag).
- **`independent: true`**: marks a context-free reviewer. In Step 5.3 its prompt is built WITHOUT your project understanding and without any agent memory, external notes, or knowledge-base context - it only gets the PR, the clone, and the cold-eyes instruction. assemble.py ignores this field; it only changes how the prompt is assembled. The default `independent.md` uses it. There is normally no reason to have more than one independent reviewer.

## Notes

- If CI is failing, treat the failure as a **Blocking** item only if it's caused by the PR itself (not flaky infra). Otherwise note it as **Optional** or skip.
- Don't restate the PR description in the report. The author wrote it; reflect it back only as one sentence in `verdict.purpose` in `synthesis.json`.
- If you couldn't read the local repo (only the diff), say so explicitly in chat before generating the report, and flag it in `verdict.rationale` — the user should know the synthesis is diff-only.
- The report directory under `~/.cache/review-pr/` is meant to be ephemeral. Don't reuse old ones across separate `/review-pr` invocations. Re-review mode is the only time you overwrite an existing report.
- `assemble.py` and `report-template.html` are stdlib-only / no-build. If you change the report's look, edit the template's CSS; if you change its structure, edit `assemble.py`. The report uses an editorial/print aesthetic (Fraunces + Newsreader + IBM Plex Mono) loaded from Google Fonts, with a system serif/mono fallback if the machine is offline.
