# python/ — `campfire` client, CLI and deploy tools

One package with three roles: the read-side client (`Campfire`, `campfire sync` / `pull`), the storage and config planes (`push`, `verify`, `config`), and publication (`campfire deploy`, `campfire dr`). User-facing usage is in `README.md`; each subsystem's rationale is in its module docstring (see Pointers).

```bash
pip install -e ./layout && pip install -e ./python     # base client (science stack included)
pip install -e "./python[deploy]"                       # deploy machines; also pulls in campfire-pipeline
CAMPFIRE_ROOT=<scratch dir> pytest python -q            # what CI's `python` check runs
```

## Where things are

| Path | What |
|---|---|
| `campfire/client.py`, `models.py`, `api/` | `Campfire` client, object and spectrum models, HTTP layer |
| `campfire/cli.py`, `modality.py` | The `campfire` CLI; `pull` / `push` / `verify` take a modality (`spec` / `im`) first |
| `campfire/sync.py`, `snapshot.py`, `db/store.py` | Catalog mirror (`$CAMPFIRE_ROOT/meta/campfire.db`) and snapshot bootstrap |
| `campfire/storage/`, `deploy/push.py` | The transfer engine shared by `push`, `pull` and `deploy` |
| `campfire/deploy/` | `campfire deploy`, config plane (`config_sync.py`, `config_cli.py`), NIRSpec flag sync (`nirspec_flags.py`), leases, retain gate |
| `campfire/dr*.py`, `archive.py`, `identify.py`, `cite.py`, `provenance.py` | Data releases: freeze/export/archive, the local release store, identify / cite / provenance |
| `campfire/transient.py` | Retry policy for idempotent requests |

## Rules

**Renaming a path helper.** Tests patch path helpers by name, so after a rename,
grep the tests for the old name before running them. A missed patch writes into
whatever `CAMPFIRE_ROOT` resolves to (another reason to run them against a scratch root).

**Deploy auth mode is explicit, never inferred.**
- `login` is the default (device-flow OAuth via `campfire login`). It runs under
  RLS and uploads through presigned URLs only, with no silent boto3 fallback.
- `service-role` needs `--service-role` or `CAMPFIRE_DEPLOY_MODE=service-role`.
  A service-role key merely present in the environment must not win.
- `local` targets the local Supabase stack.
- Object-store credentials (`CAMPFIRE_S3_*`) resolve separately. In login mode
  they're needed only by the direct-boto3 maintenance paths (bucket LIST / HEAD /
  DELETE / cross-bucket COPY). Resolution lives in `deploy/backend.py`.

**Every deploy command that writes runs under a scope lease.** A data-release
freeze refuses while any lease is live, and a lease refuses while a freeze runs,
so a freeze never snapshots a half-written multi-request catalog write. A command
that writes, over several requests, anything the freeze reads (spectra, objects,
line fits, photometry, registry rows of release members, `nircam_images`) goes
through `_under_scope_lease` (`deploy/cli.py`). `tests/test_deploy_freeze_contract.py`
classifies every deploy command, so a new command fails it until you classify it.

**Retry only what is idempotent.** `campfire/transient.py` retries transport
errors (three attempts, backoff). It never retries an answer such as a
precondition failure or a refusal. It never retries a call bound to a claim the
call itself takes (a new archive claim, an export's per-file marks), because a
resend after a lost answer would be refused as another run's.

**Storage plane invariants.**
- All data products live on OSN; map tiles are the only R2 exception.
- Dedup is by content identity: whole-file hash, except NIRCam exposures, which
  need both `sci_dq_hash` and `wcs_hash` to match (`align` moves astrometry
  without touching a pixel).
- `campfire push` moves bytes only (no catalog writes, no publication) and fetches
  live registry rows itself. Never make it depend on the local mirror being complete:
  the mirror holds finals only.
- New instruments are vocabulary entries in `modality.py`, not new CLI flags.

**The local mirror schema is versioned.** Bumping `SCHEMA_VERSION` in
`db/store.py` forces every user into a full resync, so batch shape changes into one bump.

**Config plane: the cloud is the source of truth, and nothing is deleted by
inference.** `campfire config push/pull` reconciles three-way per section against
the last-synced hash. Push refuses to clobber a cloud change and pull refuses to
clobber a local edit unless forced. Rows are never deleted: removal is an explicit
`campfire config retire`. A section missing locally never means "delete".

**NIRSpec stuck-shutter / background-override lists** sync per entry (one channel
of one exposure root × source), not per file:
- A person's entry (`# hand` / `# web`) always beats an auto detection (`# auto`).
- A pull restores a person's entry that a file lost, and a missing file is not an empty list.
- A removal travels only on an explicit yes (`--prune`). `--force` / `--theirs` are not a yes.
- `12345 = []` is a real entry: vetted, none stuck.
- Every write goes through the guarded `nirspec_review_apply` RPC, which carries
  the value the writer read.
- The table is admin-only, so check `can_sync()` first: zero rows must never read
  as "the cloud has no entries".

These rules exist because a pull once wiped hand-set overrides. Read the module
docstring of `deploy/nirspec_flags.py` before changing it;
`tests/test_nirspec_flags.py` holds the rules as pure-function tests.

**Data releases.**
- Never build user-facing sentences in SQL. RPCs return facts, and Python
  phrases them (`identify.py`, `cite.py`).
- Never store per-program rosters on `dr_releases`: every account and the public
  SQL login read that table whole.
- Import the pipeline's `science_hash` (`campfire_pipeline.metadata`); never copy it.

**API compatibility.** The server floors the client at the version in
`/api/v1/version` (`minimum`). A client change that needs a new endpoint shape must
keep working against the deployed server, or move the floor deliberately.

## Pointers

- Transfer engine and dedup: `campfire/storage/plan.py`, `deploy/push.py`
- Sync and snapshot bootstrap: `campfire/snapshot.py`, `_bootstrap_from_snapshot` in `sync.py`
- Config plane and NIRSpec flag lists: `deploy/config_sync.py`, `deploy/nirspec_flags.py`
- Retain-on-supersede and archive collection: `deploy/retain.py`, `archive.py`
- Release store: `dr_store.py`; release file format: `dr-format-v1.md` (this directory)
