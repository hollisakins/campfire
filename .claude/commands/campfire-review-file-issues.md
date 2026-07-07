---
description: Turn a triaged campfire-review export (triage.json) into GitHub issues — one per finding marked valid
argument-hint: "[path to triage.json — defaults to ~/Downloads/triage.json]"
allowed-tools: Bash(ls:*), Bash(cat:*), Read, ToolSearch, mcp__github__list_issues, mcp__github__issue_write, mcp__github__add_issue_comment
---

# File triaged review findings as GitHub issues

Takes the `triage.json` exported from a `campfire-review` HTML report and opens one
GitHub issue per theme you marked **valid**. Themes marked invalid are skipped.

Triage file: **$ARGUMENTS** (default: `~/Downloads/triage.json`).

## Step 1 — Load and confirm

1. Read the triage JSON (Read tool). It has shape:
   `{report_key, version, triaged: [{theme_id, verdict, notes, title, summary, severity, components, is_structural, lenses, findings, dedup}]}`.
2. Keep only entries with `verdict === "valid"`.
3. Print a numbered preview: for each, the title, severity, components, dedup status,
   and whether it has notes. State the total count of issues you are about to create.
4. **Stop and ask the user to confirm** before creating anything — issue creation is
   outward-facing and hard to undo. If a finding's `dedup.status` is `duplicate` or
   `related`, call it out explicitly and ask whether to skip it or comment on the
   existing issue (`dedup.issue_refs`) instead of opening a new one.

## Step 2 — Create issues

After confirmation, for each approved finding use `mcp__github__issue_write` (load via
ToolSearch if needed) against `hollisakins/campfire`. Build the body from the theme so
the issue is self-contained:

```
**Severity:** <severity>  ·  **Components:** <components>  ·  <structural?>
_Filed from periodic /campfire-review (version <version>)._

## Summary
<summary>

## Findings
- <finding title> — <file:line evidence>   (repeat per finding)

## Analysis
- 🔍 **Root cause:** <lenses.diagnostician>
- 🩹 **Quick fix:** <lenses.field_medic>
- 🏗️ **Architectural:** <lenses.rehab>
- 💡 **Opportunity:** <lenses.innovator>

## Triage notes
<notes, if any>
```

Suggested labels if the repo uses them: severity (`blocking`/`significant`/`minor`),
the primary component, and a `review-pass` label. Use a concise, specific title —
prefer the theme title verbatim.

For `duplicate`/`related` findings the user chose to fold in, use
`mcp__github__add_issue_comment` on the referenced issue instead of opening a new one.

## Step 3 — Report

List each created issue as `#<number> — <title>` with its URL, and note any that were
skipped (invalid, or folded into an existing issue). Do not invent issue numbers — use
what the API returns.
