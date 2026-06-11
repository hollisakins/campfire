# Design: Exposure-major NIRCam processing (NFS I/O consolidation)

**Status:** draft for review
**Date:** 2026-06-11
**Context:** [NFS audit](nfs_audit.md) — findings H4–H8, M1–M2, M5–M7
**Driver:** On CANDIDE (code + data on NFS), the process phase's step-major
dispatch turns each exposure into ~12 read → mutate → atomic-write cycles,
with the NFS file as the inter-step communication channel and `CFP_*` header
keywords as the resume protocol. The audit measured this as the single
largest concentration of NFS metadata traffic (~8,600 write cycles + as many
full reads + per-step side-opens for a 720-exposure run).

## 1. Goals / non-goals

**Goals**

- One NFS read and one NFS write per exposure per process phase, instead of
  ~12 of each.
- High-churn I/O (jhat scratch, matplotlib output, transient files) on
  node-local disk; NFS receives only finished products.
- Eliminate per-exposure CRDS resolution inside workers.
- Keep diagnostic plots available (not sacrificed for performance).
- Bit-identical science output. Same step functions, same order, same
  arithmetic — this is an Infrastructure change (PATCH bump), and the
  migration plan verifies that.

**Non-goals**

- The combine phase keeps its current shape (outlier is per-visit, resample
  per-tile, bkgsub/RGB per-mosaic — none of it chains per-exposure). Its
  audit fixes (H7, H8, M4) are separate local PRs.
- No change to the canonical-file model (one FITS per exposure on NFS) or to
  the `CFP_*` provenance vocabulary. Downstream consumers see the same files
  with the same stamps.
- No change to `cfpipe nircam <step>` single-step CLI semantics (see §3.6).

## 2. Current architecture (what we're changing and why it is how it is)

`run_process` (orchestrate.py:562) iterates `PROCESS_STEPS` in order, and
each step independently dispatches over all pending exposures:

```
for filt in filters:
    for step in PROCESS_STEPS:           # step-major
        dispatch(step_fn, pending_exposures, ...)
```

Every (step, exposure) pair is an independent Pool task. Consequences:

- **The NFS file is the inter-step channel.** A step must read the canonical
  FITS its predecessor wrote, and `atomic_save` it back (.tmp write +
  reopen-for-update to stamp `CFP_*`/append SRCMASK/CFMASK + rename) for the
  next step to see it.
- **Side-opens multiply.** Steps re-extract non-datamodel extensions
  (SRCMASK, CFMASK, WCS_BAK) with extra `fits.open`s before/after the
  datamodel load, because the jwst datamodel drops unknown extensions
  (audit M7 / nircam-steps#2).
- **Per-step skip checks and directory listings** (`get_exposure_files`
  re-globbed ~12–16× per filter per phase; audit M5).
- **Resume granularity is per-step**, which is the one genuine benefit of
  step-major — and the one we've agreed to trade away: a crash re-running a
  single exposure's full chain is cheap and rare.

A structural constraint discovered while designing this: **persistence is an
ensemble barrier.** `persistence_step` (steps/persistence.py:87) takes the
whole filter's exposure list — snowblind groups by detector and needs every
exposure's `_jump.fits` (persistence is flagged from temporally adjacent
exposures). It cannot live inside a per-exposure chain. This cleanly splits
the process phase into three stages:

| Stage | Unit | Steps |
|---|---|---|
| A — produce | per exposure | detector1 (uncal → canonical + `_jump.fits`) |
| B — barrier | per filter (serial, grouped by detector) | persistence (consumes all jump files, deletes them) |
| C — **the chain** | per exposure | wisp → striping → image2 → edge → sky → [diag_striping] → variance → [wcs_shift] → preview → jhat |

Stage C is 8–10 of the 12 steps and carries nearly all of the I/O churn.
Stages A and B are already minimal (detector1 is one read + one write;
persistence is one ensemble pass) and stay as they are.

## 3. Proposed architecture

### 3.1 Stage C becomes one dispatch: `process_exposure_chain`

```
for filt in filters:
    _run_detector1(...)            # unchanged (per-exposure dispatch)
    _run_persistence(...)          # unchanged (serial ensemble barrier)
    dispatch(process_exposure_chain, pending_exposures,
             field=field, chain=chain_spec, step_configs=cfgs,
             overwrite=overwrite, status=status)
```

One Pool task = one exposure pulled through every remaining chain step.
The worker:

1. **Reads the canonical FITS once** (`memmap=False`; full model + all
   sidecar extensions into a `ChainContext`).
2. Runs each pending step as an in-memory transform (§3.2).
3. **Writes once** via `atomic_save`, with *all* newly-earned `CFP_*` keys
   applied to the header *before* the staging write (killing the
   reopen-for-update on this path — audit M1) and the sidecar extensions
   attached from memory.

Per-exposure I/O for stage C drops from ~10 reads + ~10 (write + reopen +
rename) + side-opens to **1 read + 1 write + 1 rename**.

### 3.2 Step contract

All chainable steps already share the signature
`step(exposure_file, field, step_config, overwrite=False, status=None)` and
internally do open → compute → `atomic_save`. The refactor splits each into:

```python
def <name>_transform(ctx, field, step_config):
    """Pure in-memory transform. Mutates/replaces ctx.model and ctx.extras.
    No file I/O on the canonical path. May read reference data (cached, §5)
    and write diagnostics to ctx.scratch_dir (§6)."""
```

with a `ChainContext` carrying what today round-trips through the file:

```python
@dataclass
class ChainContext:
    model: ImageModel          # the datamodel, held open across steps
    extras: dict[str, fits.ImageHDU]   # SRCMASK / CFMASK / WCS_BAK, in memory
    header_updates: dict       # accumulated CFP_* stamps (+ STKSH-style cards)
    scratch_dir: str           # node-local per-exposure workspace
    rootname: str
    filtname: str
```

The existing `<name>_step(exposure_file, ...)` entry points are **kept** as
thin wrappers — read file → build ctx → call transform → `atomic_save` —
preserving the single-step CLI path (§3.6) with zero behavior change. The
transform is the new unit; the wrapper is the old contract.

Step-specific notes:

- **image2** — `Image2Pipeline.call` accepts an in-memory datamodel instead
  of a path (jwst `Step.call` dispatches on input type). The wrapper's
  current 3-opens-per-exposure pattern (SRCMASK extract + EXP_TYPE getval +
  pipeline open; image2.py:25,58,109) collapses to zero extra opens: EXP_TYPE
  comes from `ctx.model.meta`, SRCMASK from `ctx.extras`. **Verified
  2026-06-11 on the pinned stack (jwst 1.20.2 / stdatamodels 4.1.0 /
  crds 13.1.12, context jwst_1481.pmap)** — see §9 Q1: `.call(model)` is
  fully equivalent to `.call(path)`.
- **wcs_shift / diag_striping** — conditional steps: the driver builds the
  effective chain per exposure (rule-matched / config-enabled) exactly as the
  per-step runners filter today; absent steps simply contribute no stamp.
- **preview** — writes its 2 PNGs from `ctx.model` data; the PNGs compose on
  scratch and copy back (§6). Whether previews are a downstream contract
  (web/quick-look) and should stay in the canonical dir or move to
  `diagnostics/` is an open question (§8).
- **jhat (chain terminus)** — jhat is an external, file-based tool and
  already operates in `$TMPDIR` scratch (jhat.py:156). It slots in naturally
  as the *last* step: the chain writes `ctx.model` to a scratch FITS (this
  write was going to happen anyway — it replaces the final in-memory hop),
  runs jhat there, and the jhat-aligned output becomes the model for the
  single NFS `atomic_save`. The current scratch→NFS `copy2 + .tmp + replace`
  promotion logic (jhat.py:269-282) is the model for the final export.

### 3.3 Skip / resume semantics

The driver consults the existing `StepStatus` pre-scan (unchanged — one
header open per exposure per phase, audit-endorsed):

```python
effective = [s for s in CHAIN_STEPS if s.enabled(exposure, field, config)]
pending   = [s for s in effective if not status.has(f, s.cfp_key)]
if not pending: return                      # fully processed, zero I/O
# read once, run pending in order, write once stamping their keys
```

- **Crash mid-chain:** the canonical file is untouched (single atomic write
  at the end), so a re-run sees the same `pending` list and redoes the whole
  remaining chain for that exposure. Accepted trade-off (cheap, rare).
  This is *cleaner* than today: a file is either in its prior state or fully
  chained — never a half-stepped intermediate.
- **Legacy / mixed state:** an exposure stamped through, say, `CFP_SKY` by an
  old step-major run resumes mid-chain correctly — the on-disk file reflects
  the steps applied so far, exactly as the stamps say. The chain reads it and
  applies only the remainder. No migration of existing fields needed.
- **`reset --from` caveat (unchanged from today):** steps mutate the file in
  place, so re-running a mid-chain step on an already-processed file was
  never valid without re-running upstream (except steps with explicit
  backups, e.g. WCS_BAK). Chaining neither fixes nor worsens this; the doc
  notes it so nobody assumes otherwise.

### 3.4 Failure isolation

Today a step bug fails one (step, exposure) task; under chaining it fails
that exposure's whole remaining chain. `process_exposure_chain` wraps each
transform so the error report names the step, and `dispatch(retry=True)`
semantics still apply (re-running the chain task is idempotent given the
stamps). Net behavior for the operator is the same: the exposure is left
unstamped at the failing step and shows up in `cfpipe nircam status`.

### 3.5 Memory budget

A worker holds one open ImageModel (~6 detector-sized arrays ≈ 100–150 MB)
plus extras and step working memory; jwst Image2Pipeline peaks higher
(~2–4× transiently). Budget ~1 GB/worker to be safe — at 8–16 workers this
is well within a CANDIDE node. Same order as today's peak (steps already
materialize full arrays); the difference is holding *one* model across steps
rather than re-materializing it 10 times.

### 3.6 CLI compatibility

- `cfpipe nircam run` / `process` → exposure-major (stage A, barrier B,
  chain C).
- `cfpipe nircam <step>` (`run_step`) → unchanged: the kept `<name>_step`
  wrappers run the old read-transform-write per step. Ad-hoc re-runs,
  debugging, and `reset --from` workflows are untouched.
- `status` / `check` → unchanged (`CFP_*` vocabulary identical).

## 4. CRDS strategy

### 4.1 How CRDS resolution actually works (and what the env vars change)

This section exists because "set `CRDS_READONLY_CACHE=1`" was proposed
without explanation. Mechanics of a `crds.getreferences()` call:

1. **Context → mapping chain.** The pinned context (`jwst_1481.pmap`) names
   an instrument map (`.imap`), which names one rules file (`.rmap`) per
   reference type. These are small text files under
   `$CRDS_PATH/mappings/jwst/` — on NFS in our layout. **Loaded mappings are
   cached in memory per process** (`crds.rmap` keeps a module-level cache),
   so the NFS mapping reads are paid on the *first* resolution in each
   process, not per call.
2. **Client mode bookkeeping.** In the default `CRDS_MODE=auto`, each call
   decides local-vs-remote operation: it consults
   `$CRDS_PATH/config/jwst/server_config` and may attempt to contact
   `CRDS_SERVER_URL` (to refresh config / check for newer contexts / fetch
   the bad-files list). On a cluster this is at best extra NFS reads and at
   worst a hung or slow outbound HTTP attempt per worker.
3. **Cache write-readiness.** In the default read-write mode, the client
   checks whether it may need to download (cache-writability probes, lock
   handling around the download path) even when the file is already cached.
4. **Resolution + existence check.** Match parameters against the rmap,
   stat the resolved reference file in `$CRDS_PATH/references/jwst/`,
   download if missing.

So the per-call NFS cost in a *steady-state warm worker* is modest (in-memory
mappings + one stat); the real costs are (a) the first-call mapping-chain
load **per worker process** — and forkserver workers do *not* inherit the
parent's in-memory mapping cache, since the forkserver preloads modules, not
post-fork parent state — (b) mode/config bookkeeping per call, and (c) any
server-contact attempts. The audit's "10k+ round trips" figure was a guess
that likely overstates steady state; the fix hierarchy below doesn't depend
on which estimate is right.

What the env vars do:

- **`CRDS_READONLY_CACHE=1`** — the client treats the cache as immutable: no
  downloads, no config-refresh writes, no lock handling. Removes (3) and the
  write half of (2). Failure mode: a genuinely missing reference becomes a
  hard error instead of a download — which is *correct* for us, because the
  serial prefetch is supposed to have warmed everything; a miss means the
  prefetch is broken and we want to hear about it loudly, not have 16 workers
  race to download over NFS.
- **`CRDS_MODE=local`** — never contact the server; resolve purely from the
  local cache. Removes the network half of (2). Requires pinned context +
  warm cache, both of which we guarantee.

### 4.2 Recommended approach, in order of preference

**Level 1 — workers don't call CRDS at all (the real fix).**
`prefetch_process_references` already dedups exposures by
`(DETECTOR, READPATT, SUBARRAY)` / `(DETECTOR, FILTER, PUPIL)` and warms the
reference bytes. Extend it to also **record the resolved paths** —
`{(det, filt, pupil): flat_path, ...}` — and pass that dict through
`step_config` into the chain. `resolve_flat` (_flat.py:39-46, called per
exposure from wisp and striping) becomes a dict lookup; `getreferences`
disappears from worker code entirely. Combined with the `lru_cache` on the
reference *arrays* (audit H4), a worker touches each reference file at most
once, by plain path.

Residual: the jwst-internal pipelines (detector1, image2) resolve their own
references via the datamodel layer. Overriding every reftype through step
params is invasive; instead we accept their internal lookups — with Level 2
hardening they cost one mapping-chain load per worker process plus stats of
already-warm files.

**Level 2 — env hardening for whatever CRDS calls remain.**
After (and only after) the serial prefetch has run with write access:

```python
# in the worker entry / dispatch initializer, not blanket in setup_environment:
os.environ.setdefault('CRDS_READONLY_CACHE', '1')
os.environ.setdefault('CRDS_MODE', 'local')
```

Scoping matters: the prefetch itself must keep write access (it downloads on
cache miss), and forkserver inherits the environment from when the forkserver
process starts — so the clean mechanism is a `dispatch` worker-initializer
(or a `setdefault` at the top of the chain worker), not a global in
`setup_environment`. Gate behind `[environment]` config flags so a laptop run
with a cold cache behaves as today. Also fix: `prefetch_process_references`
currently early-returns when `n_processes <= 1` — it must run unconditionally
once readonly mode exists, since it's now load-bearing for correctness.

**Level 3 — measure before going further.** Before considering node-local
CRDS cache seeding (sync `$CRDS_PATH` to `$TMPDIR` per node), instrument one
worker (strace `-e trace=%file` or a crds logging hook) on CANDIDE and count
actual NFS ops attributable to CRDS for one chain task, before and after
Levels 1–2. My expectation is that Levels 1–2 reduce CRDS to noise and
Level 3 is unnecessary complexity; the measurement either confirms that or
tells us otherwise. (Flagged explicitly because the audit's CRDS cost
estimate was the least-grounded of the High findings.)

## 5. Reference-data caching in workers

Independent of CRDS resolution, the reference *arrays* must stop being
re-read per exposure (audit H4 — wisp templates read 5×/exposure today):

```python
@functools.lru_cache(maxsize=8)
def _load_template(path):
    a = fits.getdata(path, memmap=False)
    a[np.isnan(a)] = 0
    return a
# per exposure: w = _load_template(p).copy(); w[sci_before == 0] = 0
```

Same pattern for flat arrays (keyed on resolved path from §4.2) and
bad-pixel masks. Pool workers persist across tasks, so each worker pays one
read per reference file for its lifetime. Under exposure-major this matters
*more* — a worker processes many exposures back-to-back — so this lands in
the cache-tier PR *before* the chain refactor.

## 6. Diagnostics: keep the plots, move the churn

Decision: diagnostics stay on by default; what changes is **where they're
composed and where they land**.

- **Compose on scratch.** Each chain task owns
  `ctx.scratch_dir = $TMPDIR/<job>/<rootname>/` (the same workspace jhat
  uses). Steps write figures there — matplotlib's create/write/close churn
  happens on node-local disk.
- **Export in one batch per exposure.** At chain end (after the canonical
  `atomic_save`), copy the exposure's diagnostic files to
  `products/nircam/<field>/<filter>/diagnostics/` — a **new subdirectory**,
  so the ~5–7 files/exposure stop inflating the directory that
  `get_exposure_files`, resample, and `status` repeatedly glob (audit H6).
  Net NFS cost: same file count as today, but batched, sequential, and out
  of the canonical namespace. jhat's PDF/ECSV copies and the per-visit
  outlier PDFs route to the same place.
- **Optional `diagnostics_format = "tar"`** config: instead of copying
  individual files, append to one `<filter>_diagnostics_<jobid>.tar` per
  chain run (workers write per-exposure tars on scratch; parent concatenates,
  or each worker copies one tarball per exposure). This is the maximally
  NFS-friendly form (~1 file per exposure → 1 file per filter) at the cost of
  `tar -x` to view a single plot. Default stays loose files in
  `diagnostics/`; the tar mode exists for the largest fields.
- `field.diag_dir(filtname)` helper creates the subdir; `cfpipe nircam
  status`/`check` and `get_exposure_files` need no changes since they glob
  the canonical dir only.

## 7. What this design subsumes / what remains

| Audit item | Disposition |
|---|---|
| M1 atomic_save reopen ×12/exposure | Subsumed — one save, headers pre-applied; reopen survives only for the `extra_hdus` path in the legacy per-step wrappers |
| M7 per-step side-opens (SRCMASK/EXP_TYPE/WCS_BAK) | Subsumed — carried in `ChainContext` |
| M5 per-step `get_exposure_files` re-globs (process phase) | Subsumed — one dispatch per filter |
| M2 memmap'd full reads ×12 | Subsumed — one `memmap=False` read |
| H6 diagnostic small-file flood | Addressed — scratch compose + `diagnostics/` export (§6) |
| H4 reference re-reads | §5 (cache-tier PR, prerequisite) |
| H5 CRDS | §4 (Levels 1–2 in cache-tier PR; Level 3 measured) |
| H7 footprint-per-tile, H8 drizzle/outlier double-opens, M4 hash fast-path | **Not subsumed** — combine phase, separate local-fix PRs |
| M5 combine-phase + `check`/`status` globs, M9 lazy `--version` | **Not subsumed** — separate small PRs |

## 8. Migration plan

1. **PR 1 — cache tier** (no architecture change): reference-array
   `lru_cache` (§5), prefetch records resolved paths + `resolve_flat` lookup
   (§4.2 L1), CRDS env hardening with worker-scoped setdefault + prefetch
   unconditional (§4.2 L2), `memmap=False` sweep, lazy `--version`.
   Independently shippable and measurable.
2. **PR 2 — combine-phase local fixes**: footprint hoisting (H7),
   single-pass drizzle/outlier opens (H8), `_visit_up_to_date` stat
   fast-path (M4), prefetch single-pass headers.
3. **PR 3 — the chain**: `ChainContext`, `*_transform` extraction with kept
   `*_step` wrappers, `process_exposure_chain` driver, scratch workspace +
   diagnostics routing. Gated by a config flag
   (`[nircam].exposure_major = true`) for the first release so step-major
   remains one config line away.
4. **Validation for PR 3**: run a small real field both ways
   (`exposure_major` on/off) and diff: (a) canonical FITS bit-identical
   modulo header card order/HISTORY, (b) identical `CFP_*` stamp sets,
   (c) mosaics bit-identical through combine. Same functions, same order,
   same inputs → any pixel difference is a bug. Changelog: Infrastructure
   (PATCH) per the bump policy, *contingent on (a) holding*.
5. Measure on CANDIDE: wall-clock + NFS op counts (strace sample) for a
   representative filter, before/after each PR — so we know which tier
   bought what.

## 9. Open questions

1. **`Image2Pipeline.call(model)` parity** — **RESOLVED 2026-06-11: PASS.**
   Tested on jwst 1.20.2 / stdatamodels 4.1.0 / crds 13.1.12 (context
   jwst_1481.pmap) with a real canonical exposure
   (`rj0911/f090w/jw06882025001_02101_00001_nrca1.fits`), invoking
   `Image2Pipeline` with the exact production kwargs from `image2_step`
   once with the file path and once with a pre-loaded `ImageModel`:
   - **CRDS selection identical** — every `meta.ref_file` entry matches,
     including the image2-relevant ones (distortion 0265, filteroffset 0005,
     flat 0697, photom 0155).
   - **All 7 output arrays bitwise identical** (data, err, dq, var_poisson,
     var_rnoise, var_flat, area; NaN-aware).
   - **Photometry keywords and BUNIT identical**; **WCS evaluation identical
     to machine precision** at 4 sample pixels including SIP corners.
   - **`meta.filename` identical** in both modes (no special-casing needed).
   - **The caller's model is neither mutated nor returned** (`result is not
     model_in`; input data/dq unchanged) — the chain must treat the call as
     consuming its model and adopt the returned one, which the
     `ChainContext` design already does.
   Caveat: the test input was a fully-calibrated (cal-stage) file, so flat
   and photom were re-applied to already-calibrated pixels — meaningless
   scientifically but valid for parity since both modes received the same
   input, and CRDS selection parameters (detector/filter/pupil/date) are
   calibration-state-independent. Test script:
   `pipeline/scripts/test_image2_call_parity.py` (re-run against a
   rate-stage input during PR 3 validation for belt-and-braces).
2. **Preview PNGs** — diagnostics (→ `diagnostics/`) or a downstream
   contract (stay in canonical dir)? Affects only the export path.
3. **`$TMPDIR` on CANDIDE** — confirm it's node-local and sized for
   ~workers × (1 exposure FITS + diagnostics) on the relevant queues
   (assumed true; jhat already relies on it).
4. **diag_striping in-chain** — it's opt-in and heavy; confirm its transform
   doesn't need anything beyond `ChainContext` (it currently re-opens the
   file a third time for SRCMASK at save — subsumed if not).
5. **Tar mode for diagnostics** — wanted in v1, or defer until a field
   actually hurts with loose files in `diagnostics/`?
