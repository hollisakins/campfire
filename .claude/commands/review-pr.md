---
description: End-to-end CAMPFIRE PR review with auto-fix and a short triage report
argument-hint: [PR number — optional, defaults to current branch]
allowed-tools: Bash(gh:*), Bash(git:*), Bash(npm:*), Bash(pnpm:*), Bash(ruff:*), Bash(uv:*), Read, Grep, Glob, Edit, Write
---

## Context

- PR metadata: !`gh pr view $ARGUMENTS --json number,title,body,baseRefName,headRefName,files,additions,deletions 2>/dev/null || ([ -n "$ARGUMENTS" ] && echo '{"error":"PR not found"}') || git log main...HEAD --oneline | head -20`
- Full diff: !`gh pr diff $ARGUMENTS 2>/dev/null || git diff main...HEAD`
- Issue comments: !`gh pr view $ARGUMENTS --json comments 2>/dev/null || echo '[]'`
- Reviews: !`gh pr view $ARGUMENTS --json reviews 2>/dev/null || echo '[]'`
- Inline review comments: !`PR_NUM=$(gh pr view $ARGUMENTS --json number -q .number 2>/dev/null); if [ -n "$PR_NUM" ]; then REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner); gh api "repos/$REPO/pulls/$PR_NUM/comments"; else echo '[]'; fi`
- Repo guide: @CLAUDE.md

## Task

You are reviewing a CAMPFIRE PR or branch. If no PR exists yet (pre-PR review), skip the "Prior discussion" section and note that this is a pre-PR review against `main`.

The goal is a **short, high-signal** report — not a long list of everything wrong. Most trivial issues should be fixed in place; most nitpicks should be dropped entirely. Only things that genuinely need the User's judgment belong in the final report.

### Step 0 — Read the existing discussion

If there is no existing PR (pre-PR review), skip this step entirely.

Before reviewing, read through any existing comments, reviews, and inline review comments. Respect what's already been discussed:

- If a reviewer already raised an issue, don't re-raise it. Note it as "already flagged by <reviewer>, unresolved" if it still matters, or skip it entirely if resolved.
- If the User explained *why* something is the way it is in a comment, treat that as authoritative and don't second-guess it.
- If there's an unresolved thread with a real technical disagreement, that's a strong signal for "needs your call" — summarize the state of the debate.
- Distinguish conversation-tab comments (high-level) from inline review comments (tied to specific file/line locations).

### Step 1 — Trace impact end-to-end

The diff is the starting point, not the scope. CAMPFIRE is a multi-component system and changes to one layer routinely break another — especially the Python client and data download pipeline. For every file touched, ask: what depends on this?

Walk through each layer and state whether it's affected. Grep or read to confirm — do not guess:

1. **Database / Supabase** — schema, migrations, RLS policies, generated TS types
2. **Next.js API routes** — request/response shapes, auth, error handling
3. **Web frontend** — components consuming changed APIs or types
4. **Python client & CLI** — models, endpoints, data download, CLI output
5. **Docs** — README, API docs, Python client docs

Example: if a PR adds a field to `spectra`, the Python `Spectrum` model, the download function, and any CLI display code all need updating. Check explicitly.

### Step 2 — Triage into three buckets

**FIXED** — fix directly, don't report individually. Criteria:
- Typos, dead imports, missing type hints, obvious style
- Missing null checks where the pattern is clear
- Stale comments, one-line bugs with unambiguous intent
- Python client / CLI updates to match a changed API, when the right change is obvious

Make the edits. Run fast checks on changed files (syntax checks, `npm run lint`, etc.) where applicable. Do NOT commit — leave the edits staged for the User to review.

**NEEDS YOUR CALL** — keep SHORT. Only include:
- Real correctness, security, or data-integrity risks
- Design trade-offs worth discussing
- Downstream breakage where the right fix isn't obvious
- Unresolved threads from existing discussion where a decision is needed

**SKIPPED** — nitpicks not worth raising, and anything already resolved in the discussion. Don't list individually; just note the count.

### Step 3 — Report in this exact format

```
## PR #<num> review: <title>

### Prior discussion
<1–2 lines summarizing what's already been raised, or "none" if fresh>

### Scope traced
- DB/Supabase: <ok | changed | broken — one line>
- API routes: ...
- Frontend: ...
- Python client: ...
- Actions: ...
- Docs: ...

### Fixed in place (<N>)
- <file:line> — what and why (one line)
- ...

### Needs your call (<N>)
1. **<short title>** (<file:line>)
   <2–3 sentences: what, why it matters, options. If from an existing thread, say so.>
2. ...

### Skipped
<brief note or omit>
```

Do not restate what the PR does. Keep items terse. Link file:line so the User can jump straight to relevant code.