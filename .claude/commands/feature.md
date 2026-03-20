---
description: Explore and optionally implement a feature request
argument-hint: <issue_number>
allowed-tools: "*"
---

Evaluate the feature request in GitHub issue #$ARGUMENTS. Your behavior depends on the complexity of what's being asked.

## 1. Read & Understand

- Run `gh issue view $ARGUMENTS --json title,body,labels,comments` to read the full feature request.
- Identify the **component** (Web Portal, Pipeline NIRSpec, Pipeline NIRCam, Deployment/Infrastructure) from the issue metadata.
- Parse the description: what the user wants and why they think it would be useful.
- Check any existing comments for additional context or decisions from the maintainer.

## 2. Explore & Assess

- Explore the relevant area of the codebase to understand the current state.
- Determine the scope of the request. Classify it:
  - **Trivial**: A small, well-defined change (e.g., adding a UI element, exposing an existing value, tweaking a default). No design decisions needed. → Proceed to Step 3.
  - **Non-trivial**: Requires architectural decisions, has multiple valid approaches, touches many files, or has implications you're unsure about. → Proceed to Step 3-ALT.

## 3. Implement (trivial features only)

- If you're already on a dedicated worktree branch (not `main`), rename it to something descriptive: `git branch -m feat/issue-$ARGUMENTS-<short-kebab-description>`
- Otherwise, run `git checkout main && git pull origin main` and create a branch: `git checkout -b feat/issue-$ARGUMENTS-<short-kebab-description>`
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

## 3-ALT. Report back (non-trivial features)

Do NOT implement anything. Instead, comment on the issue using `gh issue comment $ARGUMENTS --body "<your analysis>"` with the following structure:

- Begin with "*Analysis by Claude Code via `/feat-request`*"
- **Summary**: What the user is asking for in your own words
- **Current state**: What the codebase does now in this area
- **Possible approaches**: 2-3 concrete implementation options with tradeoffs (effort, complexity, maintainability)
- **Recommendation**: Which approach you'd suggest and why
- **Open questions**: Anything you need the maintainer to decide before proceeding
- End the comment body with: "Let me know which direction to go and I'll implement it.

Then stop and tell me you've posted the analysis to the issue.