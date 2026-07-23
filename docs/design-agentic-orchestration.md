# Design — Agentic Distributed Orchestration (jobs plane, runner, QA bundles)

**Status:** draft for review (core decisions recorded — see §2)
**Scope:** Add a distributed-reduction control plane to CAMPFIRE: a `jobs` /
`job_events` / `runners` schema in the production Supabase project, a
`campfire runner` daemon that claims declarative job specs on compute sites
(primary site: CANDIDE, via Slurm) and wraps `cfpipe` invocations, an
agent-friendly `campfire jobs` CLI surface (`--json`, blocking `wait`), per-stage
QA bundles (metrics JSON + plots) addressable through `campfire-layout` and moved
through the existing storage plane to OSN, a human-approval (`awaiting_review`)
state for fix-verification runs, and a repo-resident orchestrator skill governing
LLM-driven operation. `cfpipe` itself stays local-only with zero cloud
dependencies; all cloud awareness lives in `python/campfire`.
**Driver:** The admin's current workflow — a Claude Code remote-control session in
`tmux` on the CANDIDE login node, hand-orchestrating NIRCam reduction (submitting
jobs, watching status, pulling up diagnostics) — works but fights the tooling: the
LLM session is simultaneously the queue, the state store, and the monitor, all
jobs it is structurally bad at. This design moves state and execution into
deterministic substrate and narrows the LLM to observation, diagnosis, and
proposal, with the human holding all visual-quality judgment.
**Context:** Informed by a comparative read of
[davecoulter/diffpype_claude](https://github.com/davecoulter/diffpype_claude)
(a FastAPI + Celery + Postgres orchestration skeleton for JWST difference
imaging). We deliberately do **not** adopt its Celery/Redis/docker-compose stack
(see §3); we do adopt two of its patterns — job provenance that records the exact
reproducing command, and upstream-staleness bookkeeping (deferred, §12).
**Related:** supersedes-in-part D12 of
[design-nircam-deploy-overhaul.md](design-nircam-deploy-overhaul.md) (see §4.1);
builds on the multi-reducer concurrency protocol of
[design-intermediate-products.md](design-intermediate-products.md) §9; deploy
auth model per closed issue #250 is treated as settled architecture; `--json`
precedent per `campfire verify --json` and issue #369.
**Repos:** `hollisakins/campfire` only (all four arms: `pipeline/`, `python/`,
`layout/`, `web/` + `supabase/`).
**Method:** six-agent parallel deep-dive over the subsystems (pipeline CLI/stages,
python client/auth/storage, layout contract, Supabase schema/RLS/RPC conventions,
web admin patterns, docs/issue conventions), with load-bearing claims verified
against source. No code changed by this doc.
**Date:** 2026-07-23

---

## 1. Executive summary

CAMPFIRE already has every hard part of a distributed-orchestration system
*except the orchestration*: an authenticated identity layer (device-flow
`campfire login` → RLS-scoped Supabase access via `AutoRefreshClient`), a
resumable content-addressed storage plane (`campfire push`/`pull` + the
`storage_objects` registry), a zero-dependency addressing contract
(`campfire-layout`, with a golden fixture enforced in both Python and TS CI
arms), free idempotency at the pipeline layer (CFP\_\* header stamps + outlier
manifests mean a crashed run can simply be re-invoked), audited SECURITY DEFINER
RPC conventions, and an admin portal with an established QA-review idiom
(`review_status` pending/approved/excluded, presigned diagnostic PNGs).

What is missing, and what this design adds, is a thin layer on top:

1. **Control plane** — `jobs`, `job_events`, `runners` tables in the production
   Supabase project, with a `claim_job` RPC (`FOR UPDATE SKIP LOCKED`, the
   repo's first use) and a `log_job_event` append path cloned from
   `deploy_events`. Postgres-as-queue, no broker (§3, §5).
2. **Runner** — `campfire runner`, a daemon in `python/campfire` that claims
   declarative job specs, translates them through a local `site.toml`
   (executor = `local` | `slurm`, resource profiles, capability labels), spawns
   `cfpipe` as a subprocess (never imports it), and reports events + heartbeats
   (§6, §7).
3. **CLI surface** — `campfire jobs submit/list/status/logs/wait/approve/cancel`
   with `--json` everywhere, designed agent-first (§8).
4. **QA bundles** — per-stage metrics JSON + diagnostic plots registered as
   layout product types under dedicated `qa/` subdirectories, pushed to OSN via
   the existing engine, so any client anywhere (phone, laptop, portal, agent)
   can fetch them (§9).
5. **Human gate** — jobs flagged `review_required` terminate in
   `awaiting_review`; only a human transition (`campfire jobs approve`, later a
   portal button) completes them. The LLM never renders visual verdicts on
   imaging diagnostics — enforced in the state machine, the event vocabulary,
   and a repo-resident skill (§10).
6. **Portal** — `/admin/jobs` following the deployments-page idiom, polling
   freshness in v1 (§11).

The build order is substrate-first (§13): schema + CLI, then runner (local, then
Slurm), then QA bundles, then the skill, then the portal page.

## 2. Decisions recorded

Resolved with the admin (2026-07-23) before drafting:

- **D1 — Control plane lives in the production Supabase project.** New tables
  alongside the science tables, reusing auth, RLS conventions, and (later)
  portal integration. Not a separate project; not local-first SQLite.
- **D2 — Approval gate scope: fix-verification only.** Routine production runs
  auto-complete on clean exit; jobs submitted as part of a debug/fix loop carry
  `review_required = true` and terminate in `awaiting_review`. `campfire deploy`
  keeps its own independent warn-and-confirm gate.
- **D3 — Build order: substrate first.** Jobs schema + `campfire jobs` CLI +
  minimal runner before the skill and QA bundles.
- **D4 — QA bundles push to OSN per stage.** Small metrics-JSON + plot bundles
  become first-class, layout-registered, cloud-backed products at stage
  completion — not on-demand, not local-only.
- **D5 — No external broker.** Postgres (`FOR UPDATE SKIP LOCKED`) is the queue.
  Celery/Redis (the diffpype stack) is rejected for CAMPFIRE's scale and
  serverless posture (§3).
- **D6 — `cfpipe` stays cloud-free.** The pipeline gains QA *emission* (writes
  to local disk via `dir_for`); all keying, upload, and control-plane traffic
  stays in `python/campfire`. This preserves the boundary AGENTS.md and every
  prior design doc restate.
- **D7 — Job specs are declarative and whitelisted.** A job names a job type
  and parametric overrides; the runner constructs the invocation. No shell
  strings ever cross the control plane (§6.2).
- **D8 — The LLM is a client, not a component.** Nothing in the substrate knows
  or cares whether a submitter is human or agent; agents are governed by a
  skill and distinguished only in attribution metadata (§10).

## 3. Why not Celery (the diffpype comparison)

Diffpype's walking skeleton (FastAPI + Celery Canvas + Redis + Postgres +
Flower/Jaeger/Prometheus, all docker-compose) is well built, and its PRD's core
thesis — the value is orchestration and bookkeeping, not the algorithms — is the
same thesis as this doc. But its infrastructure shape is wrong for CAMPFIRE:

- **Scale.** CAMPFIRE's unit of work is a multi-hour, multi-core reduction of an
  observation or field; a busy season is dozens of jobs, not millions of tasks.
  Broker infrastructure earns its keep at the latter.
- **Topology.** Celery workers must reach a broker. CANDIDE can only dial out
  over HTTPS; a publicly reachable Redis is a new, always-on, security-sensitive
  service in a stack that is otherwise Vercel + Supabase + object storage.
- **Reuse.** Everything Celery would provide (durable queue, status, retry
  bookkeeping, audit) maps onto tables and RPCs in a database we already run,
  behind auth we already have, with conventions (`deploy_events`,
  `claim_deploy_scope`, SECURITY DEFINER gating) already established.

What we take from diffpype instead: its `JobConfiguration` pattern (persist the
exact kwargs **and** the literal reproducing shell command per run — adopted in
`job_events`, §5.3) and its staleness/cascade framing (deferred, §12). If job
volume ever genuinely outgrows Postgres-as-queue, the runner's
claim/execute/report seam is where a broker could be swapped in without touching
the schema, the CLI, or `cfpipe`.

## 4. Relationship to prior decisions

### 4.1 D12 of the NIRCam deploy overhaul ("no gates, no distributed locks")

The most recent recorded decision on cluster coordination
(design-nircam-deploy-overhaul.md, D12) rejected a combine gate and distributed
locks in favor of a lightweight optimistic-version guard at deploy time. This
design does **not** silently reverse that posture; it partitions it:

- **Data-plane concurrency** (two reducers, or a reducer and a deploy, racing
  over the same tree) remains governed by D12's optimistic guards and the
  intermediate-products §9 protocol (CAS scope versions, checksum-PUT +
  verify-then-register). The jobs plane adds no locks there.
- **Work-plane coordination** (which machine runs which stage next; has a human
  looked at the result) is new territory D12 never claimed. The claim RPC's row
  lock is over the *job row*, held only inside the RPC — it is queue mechanics,
  not a distributed lock over data. `awaiting_review` gates *job completion*,
  not file writes; a human can always bypass the jobs plane entirely and run
  `cfpipe` by hand, exactly as today.

Stated plainly: **D12 stands for data; the jobs plane is advisory orchestration
above it.** The doc that owns D12 should gain a one-line cross-reference when
this design is approved.

### 4.2 The intermediate-products concurrency protocol

The `runners` table and heartbeats introduced here are deliberately *not* an
extension of `deploy_scope_state` (whose own comment says "no leases, no
heartbeats"). Deploy-scope CAS answers "did someone else deploy this scope since
I planned?"; runner heartbeats answer "is the process that claimed job X still
alive?". Different questions, different tables, no shared state.

### 4.3 Deploy auth (#250, settled)

The runner must fit the existing two-axis model: Supabase auth mode chosen
explicitly (login default / service-role opt-in), object-store creds resolved
independently, presigned-PUT-only in login mode. §7.3 maps the runner onto it;
open question A covers the one genuine gap (refresh-token rotation for a
long-lived daemon sharing a machine with interactive CLI use).

## 5. Control plane

### 5.1 Schema sketch

Follows house conventions exactly: declarative definition in
`supabase/schemas/` (tables → functions → policies ordering), `supabase db
reset` + `db diff -f <name>` to generate the migration, seed regeneration if
needed, admin RLS via the initplan-wrapped `(SELECT public.is_admin())` pattern,
append-only event table writable only through a SECURITY DEFINER RPC.

```sql
-- tables.sql (sketch — final DDL in the implementation PR)

CREATE TABLE public.jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        text NOT NULL,          -- CHECK against whitelist, §6.2
    -- exactly one scope, mirroring deployments' one-of constraint
    observation     text,
    field           text,
    spec            jsonb NOT NULL DEFAULT '{}'::jsonb,  -- parametric overrides only
    requires        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- capability labels, §7.2
    hints           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- work-size facts (n_exposures, ...)
    priority        smallint NOT NULL DEFAULT 0,
    depends_on      uuid[] NOT NULL DEFAULT '{}',
    review_required boolean NOT NULL DEFAULT false,      -- D2
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status = ANY (ARRAY['queued','claimed','running',
                           'awaiting_review','succeeded','failed','cancelled'])),
    claimed_by      uuid REFERENCES public.runners(id),
    slurm_job_id    text,
    cfpipe_version  text,                   -- resolved CMPFRVER, recorded by runner
    created_by      uuid,                   -- auth.uid(); NULL under service_role
    created_agent   text,                   -- e.g. 'claude/<session>', free text
    created_at      timestamptz NOT NULL DEFAULT now(),
    claimed_at      timestamptz,
    started_at      timestamptz,
    finished_at     timestamptz,
    reviewed_at     timestamptz,
    reviewed_by     uuid,
    CONSTRAINT jobs_one_scope CHECK (num_nonnulls(observation, field) = 1)
);

CREATE TABLE public.runners (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name              text NOT NULL UNIQUE,   -- e.g. 'candide-login1'
    site              text NOT NULL,
    labels            text[] NOT NULL DEFAULT '{}',
    executor          text NOT NULL,          -- 'local' | 'slurm'
    last_heartbeat_at timestamptz,
    registered_by     uuid,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.job_events (              -- structural clone of deploy_events
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      uuid NOT NULL REFERENCES public.jobs(id),
    action      text NOT NULL,                -- CHECK: submitted|claimed|started|
                                              -- heartbeat_lost|stage_done|qa_pushed|
                                              -- finished|failed|approved|cancelled|note
    actor       uuid,
    runner_id   uuid,
    host        text,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
```

RLS: all three tables admin-only for every verb (`admin_<verb>_<table>` policies,
`TO authenticated USING ((SELECT public.is_admin()))`), plus `FOR ALL TO
service_role` where the runner needs it — matching `deploy_events` /
`map_layers` precedent. `job_events` is INSERT-only via `log_job_event`
(SECURITY DEFINER), never direct client insert.

### 5.2 RPCs

All SECURITY DEFINER plpgsql, `SET search_path = public, pg_temp`, `p_`-prefixed
params, jsonb returns with a status key, dual-gated
(`is_admin OR auth.role() = 'service_role'`), `GRANT EXECUTE ... TO
authenticated, service_role` — the `claim_deploy_scope` / `log_deploy_event`
shape throughout.

- `claim_job(p_runner_id, p_labels text[])` — the queue's heart, and the repo's
  first `FOR UPDATE SKIP LOCKED`:

```sql
  SELECT id INTO v_job FROM public.jobs j
  WHERE j.status = 'queued'
    AND j.requires_labels(j.requires) <@ p_labels          -- label-subset match
    AND NOT EXISTS (SELECT 1 FROM unnest(j.depends_on) d
                    JOIN public.jobs dj ON dj.id = d
                    WHERE dj.status <> 'succeeded')        -- deps terminal-success
  ORDER BY j.priority DESC, j.created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1;
```

  Returns `{claimed: true, job: {...}}` or `{claimed: false}`. Note the
  existing `FOR UPDATE` precedent is `redeem_access_code`; `SKIP LOCKED` is new
  but standard Postgres.
- `heartbeat_job(p_job_id, p_runner_id)` — bumps `runners.last_heartbeat_at`;
  a companion `release_stale_jobs()` (invocable by admins, and from a scheduled
  check) flags `claimed`/`running` jobs whose runner heartbeat is stale.
  Default policy per §10: **flag for review, don't silently re-queue** —
  half-written trees deserve a human glance, and CFP-stamp idempotency makes
  the eventual re-run safe.
- `set_job_status(p_job_id, p_status, p_metadata)` — explicit allowed-transitions
  map, modeled on `set_deployment_status`. The `awaiting_review → succeeded`
  transition requires `auth.uid()` (a human admin), **never** `service_role` —
  this is where the trust boundary lives in SQL rather than in a prompt.
- `get_admin_jobs(...)` / `get_job_events(...)` — windowed-count list RPC for
  the portal (the `get_admin_deployments` pattern) and keyset-paginated event
  history (respecting `api.max_rows = 50000`).
- `log_job_event(...)` — clone of `log_deploy_event`.

### 5.3 Provenance in `job_events`

Adopting the diffpype pattern: the `submitted` event's metadata records the
job's full spec; the `started` event records the **exact reconstructed command
line** the runner executed (e.g.
`cfpipe nircam run --field cosmos --process --filters f444w -p 32 --config /tmp/job-<id>.toml`)
plus the resolved `cfpipe_version` (from `get_reduction_version`, i.e. what
lands in `CMPFRVER`), CRDS context, and the effective-config snapshot path
(`<obs>_config.toml` is already written by `metadata/summary.py` — the jobs
plane links to it rather than duplicating it). Every portal- or agent-initiated
run is thereby re-runnable verbatim from any terminal.

### 5.4 Freshness: polling in v1

Supabase Realtime is enabled-by-default in `config.toml` but completely unused:
the `supabase_realtime` publication has zero member tables and `web/` has no
channel subscriptions. Adding tables to the publication is also in migra's
blind-spot class (like materialized views), needing a hand-authored migration.
**v1 therefore polls**: `campfire jobs wait` polls an RPC server-side with
backoff, and the portal uses `refetchInterval` on the existing
`useAdminTableQuery` hook. Realtime remains a clean later upgrade (open
question B) — nothing in the design depends on push delivery.

## 6. Job model

### 6.1 Job types

The whitelist maps 1:1 onto seams that already exist as plain Python functions /
CLI verbs, and encodes the stage-order invariants the pipeline enforces socially
today (e.g. `detect-stuck` before stage2b):

| job_type | maps to | scope | spec fields (all optional) |
|---|---|---|---|
| `nirspec_stage1` … `nirspec_zfit` | `cfpipe nirspec run --obs X --stageN` | observation | `source_ids`, config overrides |
| `nirspec_all` | `--all` | observation | config overrides |
| `nircam_process` | `run_process` | field | `filters`, config overrides |
| `nircam_combine` | `run_combine` | field | `filters`, `tiles`, `epoch`, overrides |
| `nircam_step` | `run_step(step_name, ...)` | field | `step` (∈ `known_steps`), `filters`, `tiles`, `epoch` |
| `nircam_all` | `--all` | field | `filters`, overrides |

Note NIRSpec/NIRCam asymmetry is preserved, not papered over: NIRSpec jobs are
coarse stages keyed by `--obs`; NIRCam jobs are fine steps keyed by `--field`
with `filters/tiles/epoch` scoping. Multiple observations = multiple jobs (the
CLI's serial multi-`--obs` loop is exactly the decomposition boundary the
control plane wants). Push/deploy job types are deliberately out of v1 scope
(open question D).

### 6.2 Spec → invocation

`spec.config_overrides` is a TOML-shaped jsonb fragment the runner writes to a
temp file and passes via `--config`, riding the existing
`deep_merge(defaults, user_config)` resolution — the job spec is literally just
another config layer, honoring the "config is parametric only" rule.
`--overwrite` and `--processes` remain runtime-only: `overwrite` is a spec
boolean the runner threads through; `processes` comes from the **site profile**,
never the spec (the submitter doesn't know the node shape). The runner validates
`job_type` and every spec field against the whitelist before constructing
argv — unknown keys are a hard failure, and no string from the control plane is
ever passed through a shell.

### 6.3 Idempotency and failure

Nothing new is needed: CFP\_\* stamps + `StepStatus` pre-scan + outlier
manifests + `cfpipe nircam check` staleness give file/visit/tile-granular
skip-if-done on re-run. A re-claimed job after a crash is just a re-invocation.
Worker exceptions abort the `cfpipe` process with a nonzero exit; the runner
maps that to `status = failed` with the tail of captured output in the event
metadata. The `NOT_ALIGNED` quarantine sentinel (align step) is prior art for
"pipeline halts for human review" and stays exactly as is — an `nircam_step:
align` job that quarantines exposures still *succeeds* as a job; the quarantine
is surfaced through QA, not job status.

## 7. Runner

### 7.1 Placement in the package

A new lazily-registered Click group in `python/campfire`, following
`_register_deploy_group()` exactly (try-import, `cli.add_command`, install-hint
stub on ImportError). It needs supabase-py, so it lives behind the `[deploy]`
extra (or a thin `[runner]` extra aliasing it). The daemon **never imports
`campfire_pipeline`** — `dispatch()` builds its forkserver context at import
time, and Slurm execution is a separate process anyway; `cfpipe` is always a
subprocess.

The runner loop: register/heartbeat → `claim_job` with its labels → materialize
config → execute via its executor → capture stdout/stderr to a per-job log →
emit events (`BatchFlusher`-style durable incremental reporting) → on stage
completion, push the QA bundle (§9) → terminal status per §10. Concurrency cap
and poll interval come from `site.toml`.

### 7.2 Site profiles: `site.toml`

Requirements stay abstract in the control plane (labels + hints); the site owns
the translation. `$CAMPFIRE_ROOT/config/site.toml`, gitignored like
`deploy.toml`, with curated examples in-repo. This replaces the existing
anti-pattern of hostname-sniffing with hardcoded paths
(`generate_sed.py`, `cosmos_inspec.py`):

```toml
[site]
name = "candide"
executor = "slurm"                    # local | slurm
labels = ["nirspec", "nircam", "heavy", "light"]
max_concurrent_jobs = 4
poll_interval_s = 30

[environment]
setup = ["module load intel", "conda activate campfire"]
campfire_root = "/n23data2/campfire"
crds_cache = "/n23data2/crds_cache"

[executor.slurm]
partition = "n03"
# sbatch stdout/err to node-local scratch per nfs_audit.md L10
output_dir = "local_scratch"

[profiles.heavy]
cpus = 32
mem = "128G"
walltime = "12:00:00"
walltime_per_exposure = "5m"          # scaled by hints.n_exposures

[profiles.light]
cpus = 4
mem = "16G"
walltime = "2:00:00"
```

A workstation's file is four lines (`executor = "local"`, labels, cap). The
claim RPC does label-subset matching (GitHub-Actions-runner style) —
membership, not placement; on CANDIDE, Slurm remains the scheduler and the
profile only knows how to *ask* (partition + per-tier request). If a
`site.toml` starts modeling individual node types, that's the signal it's
rebuilding Slurm — add a profile or an sbatch constraint instead.

The pipeline's CANDIDE hardening is inherited for free and stays where it is:
BLAS caps (`_thread_caps.py`), forkserver + preload, EXDEV-safe TMPDIR
promotion, CRDS-race retry — all engaged the moment the runner execs `cfpipe`.

`campfire runner check` ships in v1: validate `site.toml`, confirm the conda
env resolves and `cfpipe --version` matches expectations, check CRDS cache and
`$CAMPFIRE_ROOT` access, `sbatch --test-only` a probe script, verify
storage-plane connectivity (reusing `verify` machinery), and print `--json`.
Site config drifts; a runner that fails `check` refuses to claim.

### 7.3 Executors

Interface: `submit(spec) → handle`, `poll(handle)`, `cancel(handle)`,
`logs(handle)`.

- **`LocalExecutor`** — subprocess; profile `cpus` → `-p N`.
- **`SlurmExecutor`** — renders an sbatch script (profile resources +
  `environment.setup` + the constructed `cfpipe` argv), submits, records
  `slurm_job_id` on the job row (portal → `sacct -j` traceability), polls
  `sacct`/`squeue`, maps Slurm states onto job events. The runner process
  itself stays on the login node doing sbatch + HTTPS; compute happens on the
  nodes. Greenfield — the repo contains zero Slurm code today.

### 7.4 Runner auth

Login mode is the default posture (per #250: RLS-scoped, presigned uploads, no
object-store write keys on the machine), and `AutoRefreshClient` already
survives multi-hour operations across the ~1 h Supabase JWT TTL. Two known
gaps, both handled explicitly rather than discovered later:

- **Refresh-token rotation race.** OAuth refresh tokens are single-use; a
  daemon and interactive CLI sharing `~/.campfire/credentials` can race the
  rotation and strand one of them. v1 mitigation: the runner takes a
  `--credentials <path>` (or `CAMPFIRE_CREDENTIALS`) override and runs on its
  own credential file from a dedicated `campfire login`. Whether a first-class
  machine-account flow is wanted instead is open question A.
- **Attribution under service-role.** Unattended service-role runners have
  `auth.uid() = NULL` (the `deployed_by = NULL` gap). The `runners` row +
  `job_events.runner_id` carry identity regardless of auth mode, so the audit
  trail does not depend on the Supabase principal.

The API-key (`sk_…`) credential type authenticates the web REST API but carries
no Supabase JWT, so it cannot drive the claim RPC directly; the runner therefore
uses login or service-role mode. (A future web-API jobs facade could change
this; not v1.)

## 8. CLI surface: `campfire jobs`

Agent-first means `--json` on every subcommand (generalizing the
`verify --json` precedent: suppress progress bars, structured payload,
meaningful exit codes), stable field names, and no interactive prompts outside
`approve`.

```
campfire jobs submit --type nircam_process --field cosmos \
    [--filters f444w,f277w] [--override key=val ...] [--requires heavy] \
    [--depends-on <id> ...] [--review-required] [--priority N] [--json]
campfire jobs list   [--status queued,running,awaiting_review] [--obs X | --field Y] [--json]
campfire jobs status <id> [--json]          # row + latest events + QA keys
campfire jobs events <id> [--follow] [--json]
campfire jobs logs   <id> [--tail N]        # captured cfpipe output (via presigned log artifact)
campfire jobs wait   <id> [--timeout S] [--json]   # blocks until terminal or awaiting_review
campfire jobs approve <id> [--note "..."]   # human-only transition (login mode enforced by RPC)
campfire jobs cancel <id>
campfire runner start [--site-config PATH] [--once] [--credentials PATH]
campfire runner check [--json]
```

`jobs wait` is the agent's idle primitive: launched as a background task, it
returns exactly when something happens, eliminating poll-narration turns. The
`jobs` group registers lazily like `deploy`; the read-only subcommands
(`list/status/events/wait`) should work in plain login mode without the
`[deploy]` extra if feasible, since they only need supabase-py — worth deciding
at implementation time whether to vendor a minimal RPC client for the base
install or gate the whole group behind the extra (v1: gate it; revisit on
demand).

## 9. QA bundles

### 9.1 What exists today (and doesn't)

Today "QA" is: NIRSpec per-source PDFs and the summary/shutters/pointings ECSVs;
NIRCam per-step PDFs, preview/full PNGs (the one diagnostic family already
deployed, for admin triage), mosaic thumbs, expmap products. Two hard facts from
the recon shape the design: **no metrics-as-numbers artifact exists anywhere**
(quantities like striping amplitude or alignment residuals are computed inside
steps and rendered straight into plots), and most diagnostic PDFs are not merely
unregistered but **actively unparseable** by the layout bijection (LayoutError
in the greedy obs/filter directories) — invisible to push, pull, verify, and the
presign allowlist.

### 9.2 New product family

Per-stage bundle = one `qa_metrics` JSON + zero or more plot files, in
**dedicated subdirectories** (the greedy `*.fits` fallbacks and reserved
`mosaic*`/`expmap*` prefixes make dropping new suffixes into existing dirs
fragile; `nirspec_manual_mask`'s scoped subdir + own parse branch is the
precedent):

```
products/nirspec/<obs>/qa/<stage>/<obs>_<stage>_qa.json
products/nirspec/<obs>/qa/<stage>/<obs>_<stage>_<plotname>.png
products/nircam/<field>/<filt>/qa/<step>/<...>_qa.json
products/nircam/<field>/<filt>/qa/<step>/<...>_<plotname>.png
```

Stage vocabulary: NIRSpec stage names + NIRCam `known_steps`. Registration is
the standard four-edit set enforced by CI: `ProductSpec` in
`layout/campfire_layout/products.py` (reserved → canonical `data/` keys under
both schemes, no legacy handling, unaffected by the OSN cutover),
`parse_relpath` branches in `bijection.py`, golden-fixture rows in
`layout/conformance/layout_golden.json` (both CI arms fail without them), and
the `web/lib/layout.ts` mirror. Lifecycle `CLOUD_PRODUCT` (re-fetchable,
drop-local-eligible). Plus one migration extending the
`storage_objects_product_type_check` CHECK, and a discovery clause in
`push.py` (push discovery is an explicit whitelist, not a tree walk). Once
registered, pull placement, presign allowlisting, and registry-row scoping are
automatic.

### 9.3 Pipeline emission

`cfpipe` gains a small `qa` module: stages emit a metrics dict (initial metric
set per stage is open question C — start minimal: counts, RMS-class statistics,
alignment residuals, per-step timing) written via `dir_for('<qa product>',
scope)`, plus plots. Two constraints from production reality: CANDIDE runs
`plot = false` because plot generation on NFS is expensive — QA plots should
render to node-local `$TMPDIR` and promote EXDEV-safely (the `jhat.py` pattern)
— and metrics emission must be unconditional and cheap even when plots are off
(the JSON is the layer agents triage on; plots are for human eyes). Emission is
pixel-neutral: **Infrastructure / PATCH** changelog entry.

The runner (not the pipeline) pushes the bundle at stage completion through
`plan_remote_push` → presigned `upload_files_parallel` → registry flusher, and
emits a `qa_pushed` event carrying the keys. Web-side, `presignExposurePngs` is
the exact serving pattern for the portal and for `jobs status --json` returning
fetchable URLs.

## 10. The human gate and the LLM layer

### 10.1 State machine enforcement

`review_required` jobs terminate in `awaiting_review`; the
`awaiting_review → succeeded` transition in `set_job_status` demands a real
`auth.uid()` and rejects `service_role`. An agent — which either holds no admin
login of its own or operates through the runner's identity — **cannot** close
the loop. This encodes, in SQL, the operating rule this design inherits from
the admin's direct experience: *the LLM is not to be trusted with visual
inspection of imaging diagnostics.* Two failure modes stack — vision-model
coarseness on low-contrast, stretch-dependent artifacts, and self-grading bias
(an agent that just applied a fix is structurally inclined to see success in
the after-plot). The entity that made a change never judges the change.

Stale-heartbeat jobs likewise land in a flagged state for human decision rather
than silently re-queueing (§5.2).

### 10.2 Event vocabulary: alarms are one-way

Agents (and metrics) may *raise* attention — "striping amplitude 3× sibling
tiles, plot at key K" — via `note` events and QA comparisons. Nothing but a
human `approve` *clears* it. Metrics are triage, not absolution: a reduction
can pass every number and still be garbage in a way only the admin's eyes
catch, which is why the gate sits on transitions, not on thresholds.

### 10.3 The orchestrator skill

Lives in-repo (`.claude/skills/orchestrate-reduction/` — the repo has
`.claude/commands/` and the injected reviewer persona
`.claude/review/persona.md` as precedent; no skills dir exists yet). Contents:

- The workflow: submit via `campfire jobs` (never raw `sbatch`/`squeue` when a
  job type exists), idle on `jobs wait`, reconstruct state from `jobs list
  --json` at session start (the jobs table is the agent's memory — never from
  scrollback).
- The triage checklist (walltime kill vs. CRDS miss vs. real bug; what
  `job_events` metadata to read first), accreting knowledge that currently
  evaporates with each tmux session.
- The obligation to set `--review-required` on any job submitted as part of a
  fix/debug loop (the state machine backstops a forgotten flag only socially —
  the skill is the primary enforcement; if this proves leaky, a later
  heuristic can force the flag server-side for resubmissions of a failed
  scope).
- The visual-inspection protocol, verbatim:

```markdown
## Visual inspection protocol (non-negotiable)
You are functionally blind to image quality. Never assert that a diagnostic
image shows a problem is fixed, improved, or acceptable. This applies to all
imaging diagnostics: mosaics, residuals, difference images, backgrounds.
You MAY report QA metrics and compare them to baselines/siblings, flag plots
for human attention, and describe what a metric implies (labeled as such).
You MUST NOT declare a fix verified from any rendering; use "resolved",
"clean", "looks good/better" about an image; or treat your own reading of a
plot as evidence for your own change.
Presenting plots: full resolution, never thumbnails; before/after on identical
stretch/scale/colorbar; include the storage key so the reviewer can open the
raw data. Then stop. "Ready for your inspection" is your terminal state —
approval comes from `campfire jobs approve`, not from you.
```

- Provenance for experimental runs: agent-proposed parameter experiments set
  `[pipeline].version` overrides (via spec `config_overrides`) so `CMPFRVER`
  marks them and the deploy warn-and-confirm gate catches them downstream.

Attribution: agent-submitted jobs carry `created_agent` (session identifier),
so the audit trail distinguishes human and agent submissions without the
substrate treating them differently (D8).

## 11. Portal (`/admin/jobs`)

Minimal-diff application of established idioms: nav entry under **Reduction**
in `adminNavSections`; `web/lib/actions/jobs.ts` with its own copy-pasted
`requireAdmin()` (the deliberate house convention); `get_admin_jobs` RPC with
windowed counts; `JOBS_SORT_KEYS` in `lib/admin/sort-keys.ts` (constants cannot
live in `'use server'` modules); `AdminTable`/`AdminFilterBar` +
`useTableUrlState`/`useAdminTableQuery` with a `refetchInterval` for liveness;
`statusBadge()` state chips; a "needs attention" tile on `/admin` for
`awaiting_review` count. The review detail view is modeled on the
`/admin/nircam/[id]` exposure-review pages (`review_status`
pending/approved/excluded is the existing approval idiom; prev/next neighbor
navigation), displaying QA plots via presigned GETs and metrics JSON inline,
with the approve button calling the same `set_job_status` RPC as the CLI.
Client-side layout gating is cosmetic; the RLS policies from §5.1 are the
security boundary. Timestamp normalization needs the same `fmt()` shim the
deployments page carries.

FitsGL note: PNG plots via presigned URLs are the cheap, established path and
all v1 needs. Stages whose diagnostics are FITS (residual images) can later
deploy small pyramids and get interactive full-res inspection through the
existing `@fitsgl/core` cutout/viewer machinery with zero new viewer work —
which also serves the "high resolution, from my phone" requirement better than
any chat-embedded image.

## 12. Deferred / explicitly out of v1

- **Staleness/cascade tracking** (diffpype's "out of sync" flags): when a
  re-run supersedes products downstream consumers saw. CAMPFIRE's existing
  answers (CFP stamps, `nircam check`, deploy scope versions) cover the acute
  cases; a jobs-plane generalization waits for real need.
- **Push/deploy job types** (open question D) and cross-site data staging.
- **Supabase Realtime** for events (open question B).
- **Web-API jobs facade** for `sk_` API-key runners.
- **A standing steward agent** (headless, attached to the runner, pushing
  phone notifications on `awaiting_review`): the natural endgame once the
  substrate is proven, but the tmux/interactive workflow plus §8–§10 captures
  most of the value first. No web push infrastructure exists today; the
  dashboard tile is the v1 notification.

## 13. Phased plan

Each phase is a PR-sized unit and maps to a future GitHub phase issue under a
tracking epic (the #337 pattern). Only P3 touches `pipeline/**` (one
Infrastructure changelog entry).

- **P0 — Control plane** *(deps: none)*: schema (`jobs`/`job_events`/`runners`),
  RPCs, RLS, migration + seed check. *Accept:* `claim_job` under two concurrent
  claimers never double-claims (SQL test); `awaiting_review → succeeded`
  rejected for `service_role`.
- **P1 — `campfire jobs` CLI** *(deps: P0)*: submit/list/status/events/wait/
  approve/cancel, `--json` throughout. *Accept:* an agent can drive a full job
  lifecycle (with a stub runner) purely via `--json` output.
- **P2 — Runner, local executor** *(deps: P1)*: daemon loop, `site.toml`,
  `runner check`, log capture, heartbeats, credential isolation flag.
  *Accept:* a real `nirspec_stage1` job runs end-to-end on a workstation from
  `jobs submit` to `succeeded`, with the reproducing command in `job_events`.
- **P3 — Slurm executor on CANDIDE** *(deps: P2)*: sbatch rendering, sacct
  polling, `slurm_job_id` traceability, node-local output. *Accept:* a
  `nircam_process` job for one filter completes via Slurm with correct state
  mapping, submitted from a phone.
- **P4 — QA bundles** *(deps: P2; parallel with P3)*: layout four-edit set +
  CHECK migration + push discovery + pipeline emission (metrics-first,
  node-local plot rendering). *Accept:* golden-fixture CI green in both arms;
  `jobs status --json` returns fetchable QA URLs for a completed stage.
- **P5 — Orchestrator skill** *(deps: P1, better after P4)*: skill +
  visual-inspection protocol; retire ad-hoc tmux instructions. *Accept:* a
  fresh Claude session on the login node reconstructs full reduction state
  without scrollback and submits a correctly-flagged fix-verification job.
- **P6 — Portal page** *(deps: P0; QA display needs P4)*: `/admin/jobs` +
  review detail + dashboard tile. *Accept:* approve-from-phone works.

## 14. Open questions

- **A — Runner credential model.** Is `--credentials <path>` isolation of a
  second device-flow login acceptable for the CANDIDE daemon, or do we want a
  first-class machine-account/service-account flow (new credential type, no
  rotation race by construction)? Service-role remains the unattended fallback
  per #250 either way.
- **B — Realtime.** Adopt Supabase Realtime for `job_events` (hand-authored
  publication migration, first realtime consumer in the codebase) once polling
  chafes, or stay with polling indefinitely?
- **C — Initial QA metric set.** Which numbers per stage earn a place in v1
  `qa_metrics`? (Astronomer judgment; suggest starting from what the existing
  plots already compute: striping amplitude, background RMS, alignment
  residuals/n\_matched, outlier fractions, per-step wall time.)
- **D — Publication job types.** Should `campfire push`/`deploy` become job
  types in the plane (enabling reduce→push→deploy chains via `depends_on`), or
  stay interactive-only until the reduction path is proven?
- **E — Runner privilege model.** Runners currently ride `is_admin` (login
  mode) or `service_role`. Is a dedicated `user_profiles` boolean +
  `public.can_run_jobs()` helper (the established column+helper pattern) worth
  it in v1, or deferred until there's a runner that shouldn't be an admin?
