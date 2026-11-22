---
name: campfire-review
description: Whole-repo, astronomer-led health review of CAMPFIRE. Fans out role-play and hygiene agents, verifies findings adversarially, dedups against open issues, and writes an interactive HTML triage report; a second mode files the triaged findings as GitHub issues.
argument-hint: "[focus, e.g. 'web download flow'] | file-issues [path to triage.json]"
disable-model-invocation: true
allowed-tools: Bash(git:*), Bash(date:*), Bash(mkdir:*), Bash(python3:*), Bash(ls:*), Read, Write, Workflow, ToolSearch, mcp__github__list_issues, mcp__github__list_pull_requests, mcp__github__issue_write, mcp__github__add_issue_comment
---

# CAMPFIRE periodic review

A health pass over the whole repo, outside any single PR. Run it monthly or
before a release. The flow:

```
/campfire-review [focus]
   │  stock-take (version, commits, open issues/PRs)
   ▼
workflow.js: discover (basic user · power user · admin · hygiene · consistency)
             → cluster into themes → four lenses per theme → adversarial skeptic → dedup
   ▼
reports/review-<date>/findings.json + report.html    (gitignored)
   ▼
triage in the browser → Export triage JSON
   ▼
/campfire-review file-issues triage.json   → one GitHub issue per valid theme
```

Files in this skill: `workflow.js` (the engine), `persona.md` (the astronomer
instincts every agent reads first), `render_report.py` (findings JSON →
self-contained triage HTML).

Arguments: **$ARGUMENTS**

If the arguments start with `file-issues`, skip to **Mode B**. Otherwise run
**Mode A** with the arguments as the optional focus (empty = full repo).

## Mode A: run a review

This is a **code-reading** review: do not start the app, the dev server, a
browser or the pipeline.

1. **Take stock.**
   - Pipeline version: !`git describe --tags --match 'pipeline-v*' 2>/dev/null || echo 'no pipeline tag'`
   - Recent commits: !`git log --oneline -15`
   - Report date: !`date +%Y-%m-%d`
   - Fetch open issues and PRs for `hollisakins/campfire` with
     `mcp__github__list_issues` / `mcp__github__list_pull_requests` (state OPEN;
     load them via ToolSearch). If they're unavailable, continue: dedup degrades
     to best effort and the report says so.
2. **Run the workflow.** Call the Workflow tool with
   `scriptPath: ".claude/skills/campfire-review/workflow.js"` and `args` as a
   JSON object (arrays as real arrays, not strings):
   ```
   {"version": "...", "recent_commits": "...",
    "open_issues": [{"number": N, "title": "..."}], "open_prs": [...],
    "focus": "..."}            // omit keys you don't have
   ```
3. **Persist and render.**
   - `mkdir -p reports/review-<date>`
   - Write the returned object verbatim, pretty-printed, to `reports/review-<date>/findings.json`.
   - `python3 .claude/skills/campfire-review/render_report.py reports/review-<date>/findings.json -o reports/review-<date>/report.html`
4. **Report back briefly:** counts (raw findings → themes, refuted, dedup mode),
   the top 3–5 themes by severity (one line each), and the report path, with:
   > Open it in a browser, mark each theme Valid/Invalid with notes, click
   > **Export triage JSON**, then run `/campfire-review file-issues <path>`.

Create no GitHub issues in this mode.

## Mode B: file triaged findings as issues

Triage file: the path after `file-issues`, default `~/Downloads/triage.json`.

1. **Load and confirm.** Read the JSON
   (`{report_key, version, triaged: [{theme_id, verdict, notes, title, summary, severity, components, is_structural, lenses, findings, dedup}]}`)
   and keep `verdict == "valid"`. Print a numbered preview (title, severity,
   components, dedup status, notes yes/no) and the count. **Stop and ask the user
   to confirm** before creating anything. For `dedup.status` `duplicate` or
   `related`, ask whether to skip it or comment on the existing issue
   (`dedup.issue_refs`) instead.
2. **Create issues** with `mcp__github__issue_write` on `hollisakins/campfire`,
   titled with the theme title, body:
   ```
   **Severity:** <severity>  ·  **Components:** <components>  ·  <structural?>
   _Filed from /campfire-review (version <version>)._

   ## Summary
   <summary>

   ## Findings
   - <finding title> — <file:line evidence>

   ## Analysis
   - **Root cause:** <lenses.diagnostician>
   - **Quick fix:** <lenses.field_medic>
   - **Architectural:** <lenses.rehab>
   - **Opportunity:** <lenses.innovator>

   ## Triage notes
   <notes, if any>
   ```
   For findings folded into an existing issue, use `mcp__github__add_issue_comment`.
3. **Report** each created issue as `#<number> — <title>` with its URL (numbers
   from the API, never invented), and list what was skipped or folded in.
