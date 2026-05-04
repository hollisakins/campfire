# Pipeline release

Tag and (optionally) push a new release of the `campfire-pipeline` subpackage.

Optional argument: target version (e.g. `0.4.0`). If omitted, recommend one.

Target: $ARGUMENTS

## Process

Walk through the steps below in order. Do NOT skip the inspection/confirmation
steps — releases are visible to anyone who pulls the repo and pin scientific
output values, so they warrant care.

### 1. Verify pre-conditions (read-only)

Without running the release script yet, confirm:

- Current branch is `main`
- Working tree is clean (`git status --porcelain` is empty)
- Local `main` matches `origin/main` (`git fetch origin main` then compare SHAs)
- `pipeline/CHANGELOG.md` exists with a non-empty `## Unreleased` section

If any check fails, surface the specific issue and stop. Do not attempt to
auto-fix (e.g. don't auto-commit dirty files; don't switch branches).

### 2. Show the user the Unreleased changelog content

Read `pipeline/CHANGELOG.md`, extract the `## Unreleased` section, and show
it verbatim with a `pipeline/CHANGELOG.md:<line>` reference so the user can
navigate. Note the categories present (Calibration / Algorithm / Infrastructure).

### 3. Recommend a version bump

Read the rules from `CLAUDE.md` (the "Pipeline Versioning" section, once it
exists — until then, use the rules in `pipeline/CHANGELOG.md`):

- **Calibration** present → MINOR bump (or MAJOR if it's also a breaking format change)
- **Algorithm** present → MINOR if additive, MAJOR if breaking
- **Infrastructure** only → PATCH

Find the most recent existing release tag:
```
git describe --abbrev=0 --tags --match 'pipeline-v*' 2>/dev/null
```
If no tag exists, the first release should be `0.4.0` (since `__version__`
was last manually set to `"0.3.0"` and we're moving to scm-driven versioning).

Propose a new version with a one-sentence rationale. If the user supplied a
version in `$ARGUMENTS`, use that instead and skip recommendation — but still
sanity-check it matches the changelog category.

### 4. Confirm with the user

Show the planned release inputs:
- New version number
- Resulting tag name (`pipeline-v<X.Y.Z>`)
- Commit message that will be used (`release(pipeline): vX.Y.Z`)
- Tag annotation body (the changelog entries)

Ask the user to confirm before running anything that mutates git state.

### 5. Run the release script (no push)

```
bash scripts/release-pipeline.sh <X.Y.Z>
```

This rewrites the changelog, commits, and tags **locally only**. Show the
script output. Verify with `git log --oneline -1` and
`git tag --list 'pipeline-v*' --sort=-creatordate | head -3`.

### 6. Confirm before pushing

Pushing `main` and a tag is publicly visible and effectively immutable.
Ask the user explicitly whether to push. Do not push without explicit confirmation
even if the user said "yes" earlier in the session — confirm at this step.

If confirmed:
```
git push origin main && git push origin pipeline-v<X.Y.Z>
```

(Or re-run the script with `--push`, but at this point the local commit and
tag already exist, so a direct push is cleaner.)

### 7. Post-release reminders

Tell the user:
- Re-installing `cfpipe` (`cd pipeline && pip install -e .`) will now report
  the clean version in `_version.py` and in `cfpipe info`.
- Any reductions in flight need to be re-run from a clean install of the new
  tag for their FITS headers to record the released version (vs. a `.dev` string).
- `campfire deploy` will accept FITS produced under the new version.

## Constraints

- Never amend an existing release commit or tag.
- Never push `--force` to `main`.
- If the script's preflight checks fail, do not bypass them by editing the
  script or the working tree — surface the actual issue to the user.
- If the user has uncommitted changelog edits, ask them to commit those on a
  PR first (the changelog rollover happens *during* the release, on a release
  commit; arbitrary unreleased edits should not be folded into it).
