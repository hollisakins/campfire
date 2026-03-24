---
description: Explore and optionally implement a feature request
argument-hint: <issue_number>
allowed-tools: "*"
---

Evaluate the feature request in GitHub issue #$ARGUMENTS. Your behavior depends on the complexity of what's being asked.

## 1. Read & Understand

- Run `gh issue view $ARGUMENTS --json title,body,labels` to read the full feature request.
- Identify the **component** (Web Portal, Pipeline NIRSpec, Pipeline NIRCam, Deployment/Infrastructure) from the issue metadata.
- Parse the description: what the user wants and why they think it would be useful.

## 2. Explore & Assess

- Explore the relevant area of the codebase to understand the current state.
- Determine the scope of the request. Classify it:
  - **Trivial**: A small, well-defined change (e.g., adding a UI element, exposing an existing value, tweaking a default). No design decisions needed. → Proceed to Step 3.
  - **Non-trivial**: Requires architectural decisions, has multiple valid approaches, touches many files, or has implications you're unsure about. → Proceed to Step 3-ALT.

## 3. Implement (trivial features only)

- Run `git checkout main && git pull origin main`
- Create a branch: `git checkout -b feat/issue-$ARGUMENTS-<short-kebab-description>`
- Implement the feature. Keep it focused on exactly what was requested.
- Run relevant tests. If the component is the Web Portal (Next.js), run `npm run build`.
- Add tests if the feature introduces testable logic.
- Commit: `feat: <concise description> (closes #$ARGUMENTS)`
- Push: `git push -u origin HEAD`
- Open a PR with `gh pr create`:

```
Closes #$ARGUMENTS

## What was requested?
<1-2 sentence summary of the feature>

## What I implemented
<Bullet list of changes>

## Design decisions
<Any choices made and rationale, even for trivial features>

## Testing
<What tests were run or added>
```

Then stop. I will review the PR.

When the feature lands on a branch and is in a coherent state, run /session-log. Capture the design rationale in the "key decisions" section — especially any API shape choices or data model decisions, since those are the things hardest to reconstruct later.


## 3-ALT. Report back (non-trivial features)

Do NOT implement anything. Do NOT comment on the GitHub issue. Instead, report your analysis directly to me here containing:

- **Summary**: What the user is asking for in your own words
- **Current state**: What the codebase does now in this area
- **Possible approaches**: 2-3 concrete implementation options with tradeoffs (effort, complexity, maintainability)
- **Recommendation**: Which approach you'd suggest and why
- **Open questions**: Anything you need me to decide before you proceed

Then stop and wait for my direction. Once I pick an approach, implement it on a branch (following the same branch naming, commit, and build-check conventions from Step 3) but do NOT open a PR. Just push the branch and tell me it's ready. I'll review the diff and tell you when to open the PR.