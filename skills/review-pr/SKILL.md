---
name: review-pr
description: Multi-agent review of a GitHub or Azure DevOps PR. Build project context first, spawn a roster of specialist reviewers (defined as files under reviewers/ - doomsayer, positive, code-quality nitpicker, architect, rule stickler by default) in parallel, synthesize a verdict, then open a tabbed HTML report for a human reviewer. Supports a re-review mode when paired with `/action-feedback` output. Trigger when the user asks for a PR review, says "review this PR", "can you review", "give feedback on this PR", or shares a github.com or dev.azure.com PR URL and asks for thoughts.
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

The reviewer roster is **data, not hard-coded**: each reviewer is a file under `~/.claude/skills/review-pr/reviewers/`. The default roster is **doomsayer**, **positive reviewer**, **code-quality nitpicker**, **architect**, and **rule stickler**. They run in parallel and each forms an independent view. See the "Reviewer roster" section near the end for the file format and how to add/disable one.

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
- Your Step 3-4 project understanding (paste it; don't paraphrase to one line)
- An explicit instruction to fetch the diff themselves and read surrounding files in the local clone where useful. **GitHub:** tell them to run `gh pr view` + `gh pr diff`. **Azure DevOps:** subagents can't use `gh` and shouldn't juggle tokens - instead give them the clone path plus the two commit SHAs (`target` and `source` from the PR JSON, already fetched into the clone in Step 2) and tell them to run `git -C <clone> diff <targetSha> <sourceSha>` for the diff. This keeps you (main agent) diff-blind until Step 6 while still letting each reviewer self-serve.
- A request for a markdown report, ~500 words, structured with `##`/`###` headings and bullet lists, with quoted `file:line` where applicable. The report is shown **verbatim** in this reviewer's own tab, so it must read well standalone.

**Role brief**: paste the body of the reviewer's `.md` file (everything after the frontmatter). That's the reviewer's personality and focus; don't rewrite it.

The roster is a mix of lenses: doomsayer and positive feed the Red/Green flags; nitpicker, architect, and rule stickler each sharpen one axis. Every report is preserved verbatim in its own tab, and its findings are also folded into the consolidated buckets in Step 6, tagged by the reviewer's `slug`.

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
2. `git -C <clone> diff <targetSha> <sourceSha>` for the diff (same commits the subagents used).
3. The policy-evaluations endpoint for CI/branch-policy status - it's optional and can be fiddly (needs the project GUID); if it doesn't resolve cleanly, set `ci_status` to `"unknown"` and move on rather than blocking the review.

Then, for both providers:
4. Read all the subagent reports with the diff open. Where they disagree, use your own judgement.
5. **Preserve each report verbatim.** In Step 7 you'll write each report to `$REPORT_DIR/reports/<slug>.md` unchanged — assemble.py renders each into its own tab. Don't summarise them away; the tabs are meant to show each agent's own voice.
6. Decide on a **verdict** — pick exactly one:
   - 🟢 **Approve and merge** — solid PR, ship it
   - 🟡 **Approve with minor changes** — green-light pending small fixes
   - 🟠 **Request changes** — needs material rework before re-review
   - 🔴 **Reject** — fundamentally wrong approach or unneeded
7. Bucket your synthesized findings into the four consolidated buckets, drawing from **all** the reviewers by severity (not one bucket per reviewer):
   - **Red flags** — the strongest blocking points you agree with after seeing the diff. Often the doomsayer's, but an architect structural objection or a hard rule violation can be a red flag too.
   - **Green flags** — the positive reviewer's strongest points you agree with.
   - **Minor issues** — nits worth fixing before merge. Most nitpicker findings and soft rule deviations land here.
   - **Future pitfalls** — not blocking, but worth flagging for later. Architectural debt often lands here.
   - **Tag every finding with its source reviewer** (the reviewer's `slug`) so the human can trace it back to the tab — this becomes the `src` field in `synthesis.json` (e.g. `architect`, `nitpicker`, `rules`, `doomsayer`, `positive`). If you reached a finding by combining reviewers or via your own read, tag it `synthesis`. A reviewer's `feeds` frontmatter is a soft hint for its usual bucket, not a rule — bucket by actual severity.

Don't pad. Empty buckets are fine (assemble.py renders a single "None" row). A finding raised by a reviewer that you disagree with after reading the diff should be dropped from the buckets (it still lives in that reviewer's tab) — or surfaced as a Callout if the judgement is worth explaining.

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
    "class": "verdict-changes",
    "text": "🟠 Request changes",
    "rationale": "One or two sentences explaining the call.",
    "purpose": "Your one-sentence recap of what the PR does and why."
  },
  "buckets": {
    "red":    [ {"src": "architect", "label": "Retry logic in the wrong layer.", "body": "Belongs in transport, not jobs.", "loc": "src/jobs/runner.ts:77"} ],
    "green":  [ {"src": "positive", "label": "Closes a real reliability gap.", "body": "Transient 503s no longer reach users.", "loc": ""} ],
    "minor":  [ {"src": "nitpicker", "label": "Backoff math duplicated 3x.", "body": "Extract a shared helper.", "loc": "src/net/retry.ts:30"} ],
    "future": []
  },
  "pushback": [],
  "callouts": []
}
```

Field rules:
- `verdict.class` is one of `verdict-merge` / `verdict-minor` / `verdict-changes` / `verdict-reject`; `verdict.text` is the matching label (`🟢 Approve and merge`, etc.).
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
- ✌️ **None** - <reason> _(or real items if any red flags blocked merge)_

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

---

## Callouts (optional, both modes)

The Callouts section is for **your own** notes to the human reviewer — judgement calls you made during synthesis that you want surfaced rather than absorbed silently. It is most useful in re-review mode but also valid in first-review.

Use Callouts when:
- A subagent flagged something and you decided NOT to escalate it to Pushback / Red Flag / Minor Issue, but you want the human to see your reasoning rather than have to guess at it.
- Two or more subagents disagreed and you sided with one — explain which and why, especially if a future maintainer reading the report wouldn't otherwise know there was disagreement.
- You made a deliberate scope cut (e.g. "didn't review the test fixture changes because they're regenerated from a tool") and want the human to verify the cut.
- You couldn't access something (offline clone, gated diff, missing tool) and the synthesis is partial.

Do NOT use Callouts as a dumping ground. If a point belongs in Red Flags / Pushback / Minor Issues, put it there. Callouts is specifically for **meta-commentary about the synthesis itself**, not for additional findings.

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

   **Azure DevOps:** re-hit the PR GET endpoint for the new merge-commit SHAs, re-fetch both into the clone with the token-authed `git fetch` (Provider reference), then `git -C <clone> diff <newTargetSha> <newSourceSha>`. The SHAs move between iterations, so always re-read them from the fresh PR JSON rather than reusing the first pass's.

2. **Re-spawn the same roster in parallel** (the same reviewers that ran the first time — respect any earlier skips, and any new skip the user voices now). Rebuild each prompt as the shared envelope + that reviewer's file body, plus four extra inputs:
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

   **If a subagent re-flagged something but you decided not to escalate it to Pushback**, that is exactly the case for a Callout (see Callouts section above). Drop-vs-pushback is a judgement call; surfacing the judgement to the human is better than absorbing it silently. Example wording: "Doomsayer re-flagged X on the second pass. I'm holding it in Future Pitfalls rather than Pushback because the failure mode is unobserved and the author's scope cut is sound — but flagging here so you can override if you disagree."

5. **Pick a new verdict** based on the current state.

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
| **Diff** (5 subagents, 6) | `gh pr diff $N` | `git -C <clone> diff <targetSha> <sourceSha>` where `targetSha=lastMergeTargetCommit.commitId`, `sourceSha=lastMergeSourceCommit.commitId` from the PR JSON |
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
- `additions` / `deletions` / `files` <- derive from `git -C <clone> diff --numstat <targetSha> <sourceSha>` (sum columns 1 and 2; count lines for files)
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
feeds: minor                      # soft hint for the usual synthesis bucket (red|green|minor|future); optional
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

## Notes

- If CI is failing, treat the failure as a red flag only if it's caused by the PR itself (not flaky infra). Otherwise mention it under future pitfalls or skip.
- Don't restate the PR description in the report. The author wrote it; reflect it back only as one sentence in `verdict.purpose` in `synthesis.json`.
- If you couldn't read the local repo (only the diff), say so explicitly in chat before generating the report, and flag it in `verdict.rationale` — the user should know the synthesis is diff-only.
- The report directory under `~/.cache/review-pr/` is meant to be ephemeral. Don't reuse old ones across separate `/review-pr` invocations. Re-review mode is the only time you overwrite an existing report.
- `assemble.py` and `report-template.html` are stdlib-only / no-build. If you change the report's look, edit the template's CSS; if you change its structure, edit `assemble.py`.
