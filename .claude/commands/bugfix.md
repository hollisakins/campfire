---
description: Diagnose and fix a GitHub bug report end-to-end
argument-hint: <issue_number>
allowed-tools: "*"
---

Fix the bug described in GitHub issue #$ARGUMENTS. Work autonomously through all steps below — do not ask for input unless you hit a genuine ambiguity that blocks progress.

## 1. Read & Understand

- Run `gh issue view $ARGUMENTS --json title,body,labels` to read the full bug report.
- Identify the **component** (Web Portal, Pipeline NIRSpec, Pipeline NIRCam, Deployment/Infrastructure) from the issue metadata.
- Parse the reporter's description: what happened, what they expected, and any error messages, logs, or screenshots provided.

## 2. Explore & Diagnose

- Based on the component, explore the relevant area of the codebase.
- Form a hypothesis about the root cause. Trace the code path that leads to the bug.
- If the reporter included error messages or logs, search the codebase for those strings to locate the origin.
- Confirm your diagnosis before proceeding — write a brief internal summary of what's wrong and why.

## 3. Branch

- Run `git checkout main && git pull origin main`
- Create a branch: `git checkout -b fix/issue-$ARGUMENTS-<short-kebab-description>`

## 4. Fix & Verify

- Implement the minimal, targeted fix. Prefer the smallest change that resolves the issue.
- Run any existing tests relevant to the component. If tests exist and any fail due to your change, fix them.
- If the bug is easily unit-testable, add a test that would have caught it.
- If the component is the Web Portal (Next.js), run `npm run build` to confirm no build errors.

## 5. Commit & Push

- Stage and commit with a message like: `fix: <concise description> (closes #$ARGUMENTS)`
- Push the branch: `git push -u origin HEAD`

## 6. Open PR

- Use `gh pr create` with:
  - A clear title: `Fix: <description of the bug>`
  - A body structured as:

```
Closes #$ARGUMENTS

## What was the bug?
<1-2 sentence summary of the reported issue>

## Root cause
<What was actually wrong in the code>

## What I changed
<Bullet list of changes made and why>

## Testing
<What tests were run or added to verify the fix>
```

Do NOT ask me to review intermediate steps. Deliver the PR and I will review it there.