---
name: action-feedback
description: Work through a feedback document (e.g. a `/review-pr` HTML report or any markdown feedback file), challenge each point on its merits, action what you agree with, and report back in an "Actioned / Not actioned" format that gets copied to the clipboard. Trigger ONLY via the slash command `/action-feedback <path-to-feedback-document>`.
argument-hint: <path-to-feedback-document>
---

# Action feedback: $ARGUMENTS

## When to use

User-invocable only. Trigger when the user types `/action-feedback <path>`. The path may point to:
- A `/review-pr` HTML report (e.g. `~/.cache/review-pr/<...>/report.html`)
- A markdown file with review feedback
- Any other text-based feedback document

Do **not** auto-trigger on natural-language requests. The user invokes this skill deliberately.

---

## Process

### 1. Read the feedback document

`Read` the file at `$ARGUMENTS`. If it's an HTML report (`.html`), extract the human-readable findings from the body and ignore the markup. A `/review-pr` report groups findings into severity tiers; surface them in priority order:
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

### 3. Action the items you agreed with

Make the actual code changes. Use the usual tools (`Edit`, `Write`, `Bash`). Keep changes scoped to what the feedback asked for — don't bundle in unrelated refactors. If a change requires a decision the user should make (e.g. naming, public API shape), pause and ask via `AskUserQuestion` before proceeding.

### 4. Report back in the structured format

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

### 5. Output + copy to clipboard

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
