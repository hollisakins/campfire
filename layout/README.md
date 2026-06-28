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
