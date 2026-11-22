---
name: pipeline-release
description: Cut a campfire-pipeline release (pipeline-vX.Y.Z) from the Unreleased changelog section, with a written summary as the tag body, and optionally publish it.
argument-hint: "[X.Y.Z — optional; recommended from the changelog if omitted]"
disable-model-invocation: true
allowed-tools: Bash(git:*), Bash(gh:*), Bash(bash scripts/release-pipeline.sh:*), Bash(date:*), Read, Write
---

# Pipeline release

`scripts/release-pipeline.sh` does the release: pre-flight checks, the changelog
rollover, the release commit and the annotated tag. This skill adds the parts
that need judgment: choosing the version, writing the tag summary, and
confirming before anything public happens. Releases pin scientific output, so
don't skip a confirmation step.

Target version: **$ARGUMENTS** (empty = recommend one).

1. **Pre-flight, read-only.** On `main`, clean tree, `main` equal to `origin/main`
   after `git fetch origin main`, non-empty `## Unreleased` in
   `pipeline/CHANGELOG.md`. If any check fails, report it and stop; don't
   auto-fix. Unrelated dirty files can be stashed (`git stash push -- <path>`) and
   restored afterwards. Uncommitted changelog edits belong in a PR first.
2. **Summarize the Unreleased section**: the categories present (Calibration /
   Algorithm / Infrastructure) and the scope (NIRSpec, NIRCam, shared).
3. **Version.** Apply the bump table in `pipeline/AGENTS.md`: Calibration →
   MINOR (MAJOR if also a breaking format change), Algorithm → MINOR if additive,
   MAJOR if breaking, Infrastructure only → PATCH. Start from
   `git describe --abbrev=0 --tags --match 'pipeline-v*'`. If a version was
   given, use it but check it fits the categories.
4. **Write the tag body** to a scratch file with the Write tool: first line
   `pipeline vX.Y.Z`, a blank line, then the summary grouped under
   `### Calibration` / `### Algorithm` / `### Infrastructure`.
5. **Confirm**: show the version, tag name, commit message
   (`release(pipeline): vX.Y.Z`) and the tag body, and wait for a yes.
6. **Release locally**: `bash scripts/release-pipeline.sh X.Y.Z --message-file <file>`.
   Check `git show --no-patch pipeline-vX.Y.Z` (headings and backticks intact). If
   something is wrong, `git tag -d pipeline-vX.Y.Z` and redo: nothing is pushed yet.
7. **Publish only on a fresh, explicit yes at this step**, even if the user said
   yes earlier:
   ```
   git push origin main
   git push origin pipeline-vX.Y.Z
   gh release create pipeline-vX.Y.Z --title "pipeline vX.Y.Z" --notes-from-tag
   ```
   Report the release URL.
8. **Remind the user**: reinstall `cfpipe` to pick up the clean version.
   Reductions run from a `.dev` build still carry the `.dev` string; they deploy
   behind a confirmation prompt, but rerunning from the tag gives clean provenance.

Never amend a release commit or tag, never force-push `main`, and never bypass a
failed pre-flight check.
