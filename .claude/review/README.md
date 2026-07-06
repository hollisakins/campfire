# Periodic repo review system

A workflow for periodically reviewing the whole CAMPFIRE repo for bugs, UI quirks,
docs gaps, dead code, and missing functionality — outside of individual feature/fix
PRs. Designed to be run on a cadence (monthly, or before a release).

## Pieces

| File | Role |
|------|------|
| `.claude/commands/campfire-review.md` | Entry slash command. Takes stock of the repo (version, recent commits, open issues/PRs), runs the workflow, writes the report. |
| `.claude/workflows/campfire-review.js` | The engine. Fans out discovery agents, clusters, applies four lenses, adversarially verifies, dedups. Returns findings JSON. |
| `.claude/review/persona.md` | Shared astronomer instincts injected into every agent. The single source of truth for "does this help real science?" |
| `scripts/render_review_report.py` | Renders findings JSON → self-contained interactive triage HTML. |
| `.claude/commands/campfire-review-file-issues.md` | Turns a triaged `triage.json` export into GitHub issues. |

## Flow

```
/campfire-review [focus]
   │  stock-take (version, commits, open issues/PRs)
   ▼
Workflow: campfire-review.js
   ├─ Discover : 5 agents — basic user · power user · admin · hygiene · consistency
   ├─ Cluster  : group raw findings into themes (1 theme ≈ 1 future issue)
   ├─ Synthesize: 4 lenses per theme — diagnostician · field medic · rehab · innovator
   ├─ Verify   : adversarial skeptic refutes each theme (cuts false positives)
   └─ Dedup    : cross-reference open issues/PRs
   ▼
reports/review-<date>/findings.json  +  report.html   (gitignored)
   ▼
triage in browser → mark valid/invalid + notes → Export triage JSON
   ▼
/campfire-review-file-issues triage.json  →  one GitHub issue per valid theme
```

## Design choices

- **Code-reading only.** Agents reason from source, schemas, and docs — they do not
  run the app or pipeline. (The pipeline can't run here anyway: no JWST data / CRDS /
  conda env.) A future variant could add live web + Python client runs against a
  seeded local Supabase for higher-signal behavioral findings.
- **Astronomer-first.** Every agent reads `persona.md` and `AGENTS.md` first, applies
  the G/D/D bar (generalizable / defensible / documented), and respects intentional
  decisions (e.g. `deploy/` is deprecated on purpose) so they aren't reported as bugs.
- **Adversarial verification** before anything surfaces — astronomers' trust in the
  tool dies fast on false positives. Refuted themes are shown separately, not deleted,
  so you can overrule the skeptic during triage.
- **Dedup is built in** so reruns don't regenerate the existing backlog. Degrades to
  best-effort if GitHub is unavailable, and says so in the report.
- **Triage round-trip is explicit.** A static HTML file can't call the GitHub API, so
  decisions are saved in `localStorage`, exported as JSON, and filed by a second
  command — keeping a human in the loop before anything outward-facing is created.
- **History.** Each run lands in `reports/review-<date>/` so you can compare passes
  over time. `reports/` is gitignored.

## Running on a cadence

Invoke `/campfire-review` manually monthly, or wire it to a schedule (a cron job or a
scheduled GitHub Action that runs the command headless). Pass a focus argument to scope
a run, e.g. `/campfire-review pipeline provenance`.
