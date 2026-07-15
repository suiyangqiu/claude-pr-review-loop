---
name: action-feedback
description: Work through a feedback document (e.g. a `/review-pr` HTML report or any markdown feedback file), challenge each point on its merits, action what you agree with, and report back in an "Actioned / Not actioned" format that gets copied to the clipboard. Trigger ONLY via the slash command `/action-feedback <path-to-feedback-document>`.
argument-hint: <path-to-feedback-document> [plan]
---

# Action feedback: $ARGUMENTS

## When to use

User-invocable only. Trigger when the user types `/action-feedback <path>`. The path may point to:
- A `/review-pr` HTML report (e.g. `~/.cache/review-pr/<...>/report.html`)
- A markdown file with review feedback
- Any other text-based feedback document

Do **not** auto-trigger on natural-language requests. The user invokes this skill deliberately.

### Plan-first mode (configurable)

By default this skill **actions changes immediately** (decide -> edit -> report). If the user asks it to **plan first**, it instead proposes the changes and waits for sign-off before touching any code.

Turn on plan-first mode when **either** is true:
- `$ARGUMENTS` contains a `plan` token after the path (e.g. `/action-feedback ~/.cache/review-pr/.../report.html plan`).
- The user's invocation message says to plan first / propose first / don't make changes yet / "let me review the plan" / similar.

When neither applies, run normally (action immediately). When in doubt and the user's wording leans toward wanting a look before edits, prefer plan-first - it's the safer default. The `plan` token is config, not part of the path; strip it before resolving the file.

---

## Process

### 1. Read the feedback document

`Read` the file at `$ARGUMENTS` (if a trailing `plan` token is present, strip it first - that's the plan-first flag, not part of the path). If it's an HTML report (`.html`), extract the human-readable findings from the body and ignore the markup. A `/review-pr` report groups findings into severity tiers; surface them in priority order:
- **Blocking** - must fix before merge
- **Material** - should fix before merge
- **Optional** - polish / nits (action the cheap, clearly-good ones; fine to decline the rest)
- **Green flags** (skip — these are praise, nothing to action)
- **Pushback** (if a re-review report)
- Any verdict rationale

### 2. Go through each piece of feedback, on its merits

For every actionable item, do **not** take it on faith. Challenge it:
- Is this actually a problem in this codebase, or only in general?
- Does the proposed change conflict with project conventions or existing decisions?
- Is there a better fix than the one suggested?
- Is the cost (complexity / churn / risk) worth the benefit?

Decide for each item: **action it** or **decline it with reasoning**.

You can also choose to action it *differently* than suggested if you have a stronger fix. Note that explicitly in your response.

**Weight your effort by tier.** Blocking and Material items deserve a real fix or a genuinely strong reason to decline. **Optional items are optional** - action the ones that are cheap and clearly an improvement, but feel free to decline low-value churn with a one-line reason. Do not treat declining an Optional nit as a failure; a PR that is merge-ready apart from a few declined nits is the expected end state. Don't manufacture work to look thorough.

### 2b. Plan-first gate (only in plan-first mode)

**Skip this step entirely in default mode** - go straight to step 3 and start editing.

In plan-first mode, do NOT make any code changes yet. Instead, present a plan and wait:

1. For each item, state your decision (action / action differently / decline) with a one-line rationale, grouped under **Will action** and **Won't action**, each tagged with its tier (`[Blocking]`/`[Material]`/`[Optional]`). For "Will action" items, briefly say *what* the change will be and *where* (`file.ts:line`), not just that you'll do it.
2. Ask the user to confirm via `AskUserQuestion`: proceed as planned / adjust first. Make "Proceed" the first (recommended) option. The user can also reply free-form to redirect (e.g. "skip item 3", "action the nitpicker's point too", "fix it this other way").
3. If the user asks for adjustments, revise the plan and re-confirm. Loop until they approve.
4. Only once the user approves, continue to step 3 and make the changes exactly as agreed.

Do not start editing files until the user has signed off on the plan.

### 3. Set up an isolated worktree on the PR's branch

**Always action feedback inside a dedicated git worktree checked out to the PR's own branch - never edit the main checkout directly.** The fixes must land on the PR branch so they stay attached to the PR. Do this before making any code changes (in plan-first mode, after the user approves the plan).

1. **Identify the repo and PR branch.** Derive them from the feedback document - a `/review-pr` report names the repo, PR number, and source branch (the cache dir is named like `<org>-<repo>-<prNumber>-<timestamp>`). If the branch isn't in the report, query it: `gh pr view <n> --json headRefName,headRepository` (GitHub) or `az repos pr show --id <n>` (Azure DevOps). Locate the local clone for that repo.
2. **Reuse or create the worktree.** Run `git worktree list` in the clone. If a worktree is already checked out to the PR's branch, use it. Otherwise create one (e.g. under the repo's `.worktrees/`) checked out to the existing branch:
   ```bash
   git -C <clone> worktree add <clone>/.worktrees/<short-name> <pr-branch>
   ```
   Use the existing branch - do **not** create a new branch (that would detach the fixes from the PR). If the branch isn't present locally, fetch it first (`git -C <clone> fetch <remote> <pr-branch>`).
3. **Confirm before edits.** State in chat which worktree path and branch you're operating in, and verify `git -C <worktree> status` is on the expected branch. If the branch can't be determined or no matching clone exists, stop and ask the user rather than guessing.
4. Make all subsequent edits inside that worktree path.

### 4. Action the items you agreed with

Make the actual code changes **inside the worktree from step 3**. Use the usual tools (`Edit`, `Write`, `Bash`). Keep changes scoped to what the feedback asked for — don't bundle in unrelated refactors. If a change requires a decision the user should make (e.g. naming, public API shape), pause and ask via `AskUserQuestion` before proceeding.

### 5. Report back in the structured format

Compose a markdown report in **exactly this shape**:

```markdown
## Actioned

- **[Blocking] <short label>** - <one or two sentences on the implementation>. <file.ts:line> if relevant.
- **[Material] <short label>** - <implementation note>.

## Not actioned

- **[Optional] <short label>** - <why you disagree>. Be specific about the reasoning the author of the feedback would need to address to convince you.
- **[Material] <short label>** - <why you disagree>.
```

Rules:
- One bullet per item. No paragraphs.
- Lead with the item's **tier tag** (`[Blocking]` / `[Material]` / `[Optional]`) carried over from the report, then a bold short label, then a hyphen, then the explanation. The tier tells the re-review how hard to push: a declined Blocking/Material item is a real disagreement; a declined Optional is usually fine. No em dashes — use a regular hyphen.
- "Not actioned" items must include a concrete reason, not just "disagree". The `/review-pr` re-review will use this to decide whether to push back.
- If everything was actioned, render the "Not actioned" section as `_(nothing declined)_`. If nothing was actioned, render the "Actioned" section as `_(nothing actioned)_`.
- Skip "Green flags" items entirely — they're not actionable.

### 6. Output + copy to clipboard

1. Print the full markdown report to chat so the user can see it.
2. Pipe the same report through `pbcopy` so it's on the clipboard, ready to paste back into the `/review-pr` chat:
   ```bash
   pbcopy <<'EOF'
   <the rendered report>
   EOF
   ```
3. End your turn with one short line: "Report copied to clipboard — paste it into the `/review-pr` chat to trigger a re-review."

---

## Notes

- If the path doesn't exist or isn't readable, stop and tell the user the path you tried.
- If the document has no actionable items (e.g. all green flags, or a fully approving review), say so explicitly and skip the action phase — still copy a minimal "nothing to action" report to the clipboard so the re-review trigger still works.
- This skill makes code changes. If the repo has uncommitted changes when you start, mention them in chat before editing — the user may want to commit or stash first.
- Don't commit or push the changes you make. Leave that to the user.
