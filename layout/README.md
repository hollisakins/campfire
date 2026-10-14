# campfire-layout

The single authority for the CAMPFIRE directory/key contract — issue #213, PR-2 of
epic #210 (intermediate products & cloud-as-source-of-truth).

The `$CAMPFIRE_ROOT/` tree is a three-way contract between the **pipeline** (which
creates it), the **CLI/deploy client** (which mirrors it on download and builds
storage keys on deploy), and the **cloud + web** (whose keys must round-trip to
it). Historically that contract was re-derived independently in each codebase and
nothing forced agreement. This package collapses it into one tested module.

## What it owns

- **Local paths** — `local_path`/`local_relpath`/`dir_for` for any product under
  `$CAMPFIRE_ROOT`, plus the tree helpers (`reference_dir`, `shared_reference_dir`,
  `raw_dir`, `cache_path`) the pipeline workspace setup uses.
- **Storage keys** — `storage_key`/`key_prefix`/`bucket_for`, with two schemes:
  `LEGACY` (today's bare `spectra/<obs>/…` keys; the default through the F0/pre-OSN
  window) and `CANONICAL` (`data/` + local relpath; flipped at the OSN cutover).
- **The bijection** — `key_to_relpath` / `relpath_to_key` (total + reversible per
  scheme), plus `parse_key`, `parse_relpath`, `derive_sibling`, and `is_known_key`
  (the presign/proxy allowlist).
- **Lifecycle** — `tree_class` classifies any path/key as cloud-product /
  user-state / shared-calibration / external-MAST / regenerable / cli-local,
  resolving via the product registry first (so e.g. NIRSpec manual masks are
  user-state even though they live under `products/`).

The whole contract is data: see `campfire_layout/products.py`, the declarative
`PRODUCTS` registry. Everything else reads it.

## Design constraints

- **Zero runtime dependencies** (stdlib only), `requires-python >= 3.11`, so both
  the local-only pipeline (`campfire-pipeline`, py≥3.12) and the client
  (`campfire`, py≥3.11) can depend on it without coupling reduction to the cloud
  client.
- **Mirrored in TypeScript** at `web/lib/layout.ts` for the web portal. The shared
  golden fixture `conformance/layout_golden.json` is checked by **both** the python
  test (`pipeline/tests/test_layout.py`) and the TS test (`web/lib/layout.test.ts`),
  so python↔TS drift fails CI.

## Regenerating the web mirror

The TS side does not hand-copy the product table. `web/lib/layout.ts` builds its
`PRODUCTS` registry and the two suffix-dispatch tables from
`web/lib/layout-products.json`, which `scripts/sync_layout_products.py` generates
from `campfire_layout/products.py` (plus the dispatch tables in `bijection.py`):
one entry per `ProductSpec` with the `subdir` / `filename` / `legacy_prefix`
builders flattened to `{obs}`-style templates, in registration order. **After any
edit to `products.py` or to those tables, run `python scripts/sync_layout_products.py`
and commit the JSON** (it is stdlib-only and reads the in-repo `layout/`, so no
install is needed). CI runs the same script with `--check` — and
`pipeline/tests/test_layout.py` repeats the check — so a stale copy fails. The
imperative parsers in `layout.ts` (`parseRelpath` / `parseLegacyKey`, the tile
regex, the in-directory `mosaic*` / `expmap*` / bare-`.fits` rules) mirror
*code* in `bijection.py`, not registry data, and remain hand-written; the golden
fixture pins every branch of both.

## Install (editable dev)

```bash
conda activate campfire
pip install -e ./layout      # this package first
pip install -e ./pipeline
pip install -e ./python
```

## Test

```bash
conda run -n campfire python -m pytest pipeline/tests/test_layout.py -q   # python arm
cd web && npx vitest run lib/layout.test.ts                                # TS arm
```

## Roots (P1, data releases)

A parsed key or relpath carries a `root` (`ParsedKey.root` / TS `ParsedKey.root`):

| root | shape | parses from | storage key? | writable? |
|---|---|---|---|---|
| `live` | today's keys and relpaths | `parse_key`, `parse_relpath` | yes (`storage_key`) | yes |
| `archive` | `archive/<sha256 hex>/<relpath>` — the content-addressed store every published NIRSpec final / sidecar / mosaic is retained in | `parse_key` | itself (`archive_key`, `key_for_parsed`) | **never** (written once, server-side) |
| `dr` | `dr/<tag>/products/<relpath>` — a release's local tree | `parse_relpath` | **none** (its bytes come from `archive/` via `dr_manifest`) | n/a |

Rules the goldens pin (`archive_keys`, `archive_rejected_keys`, `dr_relpaths`,
`dr_rejected_relpaths`, `foreign_root_keys`): a rebuild always goes through
`key_for_parsed` / `keyForParsed`, which honours the root, so an archive copy can never be
mapped back onto the live key; `derive_sibling` refuses archive keys (each archived sidecar
sits under its own hash; look siblings up by hash); `is_readable_key` admits live and
archive, `is_writable_key` live only (the deploy presign route uses it; `is_known_key` is
the read alias); only `cloud-backed-product` lifecycle products can be archived or
released; `tree_class` of anything under `archive/` or `dr/` is `release`
(immutable, never purged by the live mirror). `data/archive/…`, `data/dr/…` and `dr/…` as
cloud keys stay rejected.
