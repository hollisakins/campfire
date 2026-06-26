---
description: Whole-repo astronomer-led health review — fans out role-play + four-lens agents, verifies, dedups against issues, and produces a triage HTML report
argument-hint: "[optional focus, e.g. 'web download flow' or 'pipeline provenance']"
allowed-tools: Bash(git:*), Bash(date:*), Bash(mkdir:*), Bash(python3:*), Bash(ls:*), Read, Write, Workflow, ToolSearch, mcp__github__list_issues, mcp__github__list_pull_requests
---

# CAMPFIRE periodic review

A comprehensive, outside-of-a-single-PR health pass over the whole repo. You take
stock of the project, fan out astronomer role-play + hygiene agents across the
codebase, synthesize through four lenses, adversarially verify, dedup against the
issue tracker, and emit an interactive HTML triage report.

Optional focus for this run: **$ARGUMENTS** (empty = full repo).

This is a **code-reading** review — do NOT start the app, the dev server, the
browser, or the pipeline. The depth comes from reading source, schemas, and docs.

## Step 1 — Take stock of the repo

Gather the context the workflow needs. Run these and read the results:

- Pipeline version: !`git describe --tags --match 'pipeline-v*' 2>/dev/null || echo 'no pipeline tag'`
- Recent commits: !`git log --oneline -15`
- Current branch / status: !`git status -sb | head -5`
- Report date: !`date +%Y-%m-%d`

Then fetch the open issues and PRs for `hollisakins/campfire` so findings can be
deduplicated:

- Use `mcp__github__list_issues` (state OPEN) to get `[{number, title}]`. If those
  tools are unavailable (load via ToolSearch first if needed), continue without
  them — the workflow will degrade dedup to best-effort and say so in the report.
- Use `mcp__github__list_pull_requests` (state OPEN) for `[{number, title}]`.

## Step 2 — Run the review workflow

Call the **Workflow** tool with `name: "campfire-review"` and pass `args` as a JSON
object built from Step 1:

```
{
  "version":        "<pipeline version string>",
  "recent_commits": "<the git log --oneline output>",
  "open_issues":    [{"number": N, "title": "..."}, ...],   // or omit if unavailable
  "open_prs":       [{"number": N, "title": "..."}, ...],   // or omit if unavailable
  "focus":          "$ARGUMENTS"                            // or omit if empty
}
```

Pass `open_issues` / `open_prs` as real JSON arrays, not stringified. The workflow
runs five discovery agents (basic user, power user, admin, hygiene, consistency),
clusters findings into themes, applies the four lenses, runs an adversarial skeptic
per theme, and dedups. It returns a findings object.

## Step 3 — Persist and render

1. Create the report dir: `mkdir -p reports/review-<date>`
2. Write the workflow's returned object verbatim to
   `reports/review-<date>/findings.json` (use the Write tool with pretty JSON).
3. Render the HTML:
   `python3 scripts/render_review_report.py reports/review-<date>/findings.json -o reports/review-<date>/report.html`

## Step 4 — Report back to the user

Give a short summary, no wall of text:

- Counts: raw findings → themes for triage, how many refuted, dedup mode.
- The top 3–5 themes by severity, one line each (title + severity + components).
- The path to `reports/review-<date>/report.html` and a one-line how-to:
  > Open it in a browser, mark each theme Valid/Invalid and add notes (saved
  > locally), click **Export triage JSON**, then run
  > `/campfire-review-file-issues` on the downloaded `triage.json` to file the
  > valid ones as GitHub issues.

Do NOT create any GitHub issues in this command — triage happens in the browser
first. `reports/` is gitignored; nothing here is committed automatically.
