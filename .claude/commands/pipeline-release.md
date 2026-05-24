# Pipeline release

Tag and (optionally) push a new release of the `campfire-pipeline` subpackage.

Optional argument: target version (e.g. `0.4.0`). If omitted, recommend one.

Target: $ARGUMENTS

`scripts/release-pipeline.sh` exists as a faster path with a **verbatim**
Unreleased section as the tag body. This slash command performs the same
operations by hand so the tag annotation can be a **synthesized** summary
instead — useful when the Unreleased section is too long or too granular
to make a useful `git show` view.

## Process

Walk through the steps below in order. Do NOT skip the inspection/confirmation
steps — releases are visible to anyone who pulls the repo and pin scientific
output values, so they warrant care.

### 1. Verify pre-conditions (read-only)

Before mutating any state, confirm:

- Current branch is `main`
- Working tree is clean (`git status --porcelain` is empty)
- Local `main` matches `origin/main` (`git fetch origin main` then compare SHAs)
- `pipeline/CHANGELOG.md` exists with a non-empty `## Unreleased` section

If any check fails, surface the specific issue and stop. Do not attempt to
auto-fix (e.g. don't auto-commit dirty files; don't switch branches).

### 2. Synthesize the unreleased changes

Read `pipeline/CHANGELOG.md`, extract the `## Unreleased` section, and synthesize 
the unreleased changes to present to the user. Note the categories present 
(Calibration / Algorithm / Infrastructure) and the scope of the changes across 
the pipeline (e.g., NIRCam vs. NIRSpec). 

### 3. Recommend a version bump

Read the rules from `CLAUDE.md`:

- **Calibration** present → MINOR bump (or MAJOR if it's also a breaking format change)
- **Algorithm** present → MINOR if additive, MAJOR if breaking
- **Infrastructure** only → PATCH

Find the most recent existing release tag:
```
git describe --abbrev=0 --tags --match 'pipeline-v*' 2>/dev/null
```

Propose a new version with a one-sentence rationale. If the user supplied a
version in `$ARGUMENTS`, use that instead and skip recommendation — but still
sanity-check it matches the changelog category.

### 4. Confirm with the user

Show the planned release inputs:
- New version number
- Resulting tag name (`pipeline-v<X.Y.Z>`)
- Commit message that will be used (`release(pipeline): vX.Y.Z`)
- Tag annotation body (the synthesized changelog entries)

Ask the user to confirm before running anything that mutates git state.

### 5. Apply the release locally (no push)

Four sub-steps. Stop on the first failure and surface it to the user.

**a. Rewrite the changelog.** Keep `## Unreleased` as a permanent marker
and insert a `## v<X.Y.Z> — YYYY-MM-DD` heading immediately below it (date
in UTC, `date -u +%Y-%m-%d`). Use Edit anchored on `## Unreleased` plus the
first sub-heading underneath (typically `### Calibration` or `### Algorithm`)
to scope the change.

**b. Commit the changelog.**
```
git add pipeline/CHANGELOG.md
git commit -m "release(pipeline): v<X.Y.Z>"
```

**c. Write the synthesized tag annotation body to a temp file** (e.g.
`/tmp/pipeline-v<X.Y.Z>-tagmsg.txt`) via the Write tool. Using a file rather
than passing `-m` avoids two footguns observed previously:
- `git tag -a -m` defaults to `--cleanup=strip`, which removes lines
  beginning with `#` as comments — silently eating
  `### Calibration` / `### Algorithm` / `### Infrastructure` headings.
- Shell quoting of backticks inside a heredoc is fragile; a file sidesteps
  it entirely (single-quoted `'EOF'` heredocs leak escaped backticks
  through as literal `\``).

**d. Create the annotated tag and clean up.**
```
git tag -a pipeline-v<X.Y.Z> --cleanup=verbatim -F /tmp/pipeline-v<X.Y.Z>-tagmsg.txt
rm /tmp/pipeline-v<X.Y.Z>-tagmsg.txt
```

Verify with `git log --oneline -1`, `git tag --list 'pipeline-v*' --sort=-creatordate | head -3`,
and `git show --no-patch pipeline-v<X.Y.Z>`. Spot-check the rendered body:
section headings present, backticks render as backticks (not literal
`\``). If anything looks wrong, `git tag -d` and recreate — the tag is not
yet pushed and is freely re-doable.

### 6. Confirm before pushing and creating the GitHub Release

Pushing `main`, the tag, and the Release are all publicly visible and
effectively immutable. Ask the user explicitly whether to publish. Do not
proceed without explicit confirmation even if the user said "yes" earlier in
the session — confirm at this step.

If confirmed, run all three:
```
git push origin main
git push origin pipeline-v<X.Y.Z>
gh release create pipeline-v<X.Y.Z> --title "pipeline v<X.Y.Z>" --notes-from-tag
```

`--notes-from-tag` reuses the annotated tag's body as the Release notes,
keeping the tag as the single source of truth (no drift between the two
stores). The Release lands at
`github.com/hollisakins/campfire/releases/tag/pipeline-v<X.Y.Z>` —
report the URL back to the user.

### 7. Post-release reminders

Tell the user:
- Re-installing `cfpipe` (`cd pipeline && pip install -e .`) will now report
  the clean version in `_version.py` and in `cfpipe info`.
- Reductions in flight will still record a `.dev` string in their FITS headers
  unless re-run from a clean install of the new tag. Such data can still be
  deployed (with a warn-and-confirm prompt) but loses the clean release
  provenance — re-running is preferred when feasible.

## Constraints

- Never amend an existing release commit or tag (once pushed, the tag is
  effectively immutable; before push, prefer `git tag -d` + recreate over
  amending).
- Never push `--force` to `main`.
- If a preflight check fails, do not bypass it (e.g. by editing files or
  skipping checks) — surface the actual issue to the user. Unrelated dirty
  files can be stashed (`git stash push -- <path>`) and popped after the
  release.
- If the user has uncommitted changelog edits, ask them to commit those on a
  PR first (the changelog rollover happens *during* the release, on a release
  commit; arbitrary unreleased edits should not be folded into it).
