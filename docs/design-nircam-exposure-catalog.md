# Design: NIRCam exposure catalog — publishing the frames behind the mosaics

**Status:** draft for review — revision 2 (non-science decisions resolved, D7–D16; §13 lists what
revision 1 got wrong)
**Date:** 2026-09-15
**Driver:** [#562](https://github.com/hollisakins/campfire/issues/562) (Ivo Labbé) — record the
corrected WCS of every frame that went into a mosaic, so that for any mosaic pixel a user can
determine which frames covered it and drizzle a theoretical PSF (or a simulation) through the
identical geometry.
**Related:** [#309](https://github.com/hollisakins/campfire/issues/309) (exposure footprints +
spatial overlap queries, deferred on `pg_sphere`/self-hosting),
[design-intermediate-products.md](design-intermediate-products.md),
[design-nircam-deploy-overhaul.md](design-nircam-deploy-overhaul.md),
[design-public-mirror.md](design-public-mirror.md) §5.3 (share-link accounts — every NIRCam
policy carries a link branch; this one must too).

---

## 1. The gap, in one line

**The exposure bytes are public; the index to them is admin-only.**

- Canonical exposure FITS are uploaded to OSN (`build_fits_upload_tasks`,
  `python/campfire/deploy/nircam.py:318`), registered in `storage_objects` as `nircam_exposure`,
  and a *published NIRCam field deployment* is visible to **everyone** — no program scoping
  (`get_storage_objects_for_sync` / `filter_accessible_storage_keys`, the `d.field IS NOT NULL`
  branch, `supabase/schemas/functions.sql:4481,4656`).
- `nircam_exposures` — the table that says which exposures exist — is gated by
  `admin_select_exposures` RLS (`supabase/schemas/policies.sql:905`), and its only read path,
  the `get_admin_exposures` RPC, raises on non-admins (`functions.sql:5638`).

So a user can download any exposure whose key they can guess, and has no way to learn the keys.
Issue #562 is one symptom. The others: no exposure search, no mosaic↔exposure cross-match, no
footprint, no WCS.

## 2. What already exists

Worth stating plainly, because most of this design is *exposure*, not construction.

| Capability | Where | Status |
|---|---|---|
| Corrected WCS (gwcs in ASDF + refreshed SIP FITS keys) | `nircam/align/apply.py:372` (`_write_solution` → jwst's `update_fits_wcsinfo`, called with defaults) | On every canonical exposure |
| Canonical exposure FITS on OSN | `deploy/nircam.py:318` | Uploaded + registered |
| Public read of those bytes | `get_storage_objects_for_sync`, `filter_accessible_storage_keys` | Works today |
| Pull path | `campfire pull --field X --intermediate` (`INTERMEDIATE_PRODUCT_TYPES`, `db/store.py:35`) | Works today |
| Exposure rows (field/filter/detector/visit/date_obs/ra_center/dec_center/stage/review) | `nircam_exposures`, written by `_upsert_exposures` (`deploy/nircam.py:790`) | Admin-only |
| Unique key on exposure rows | `nircam_exposures_unique UNIQUE (field, filter, filename)` (`tables.sql:1958`) — `_upsert_exposures` already upserts on it | **Exists** (rev. 1 said it did not) |
| Astrometric identity per exposure | `storage_objects.wcs_hash` (`tables.sql:1190`), computed by `campfire.storage.hashing.wcs_hash` over the `_WCS_KEY_RE` card whitelist | In the registry today |
| Exposure browser UI (filters, sort, neighbors, related, filter options, progress) | `web/lib/actions/nircam-exposures.ts`, `/admin/nircam`, `/admin/intermediate-products` | Admin-only |
| Mosaic → input-frame list | `nircam/manifest.py:324` (`create_manifest`, `inputs[]` with `filename`, `file_hash`, `wcs_hash`) | On disk; **parsed by deploy and discarded** (`deploy/nircam.py:930`) |
| Per-pixel input bitmask (CON) | `nircam/drizzle.py:288` | In the i2d, which deploy deliberately withholds (`_UNDEPLOYED_MOSAIC_EXTENSIONS`, `deploy/nircam.py:889`) |
| Footprint parsing (S_REGION → polygon) | `nircam/geometry.py:59`, `nircam/expmap.py:102` | Local diagnostics only; `geometry.py` needs `shapely` (pipeline-only dependency) |
| The `sci` extension row as "the mosaic" in the web | `NircamTable.tsx:208` (thumbnail, view button hang off `extension === 'sci'`), `deploy/nircam.py:1098` | Established convention |

Two details that shape the design:

- `_read_exposure_metadata` (`deploy/nircam.py:205`) **already opens the SCI header** to pull
  `CRVAL1`/`CRVAL2`. Extending it to the full SIP WCS card set is nearly free — same open, same
  header.
- `discover_mosaics` **already parses every manifest** at deploy time. The mosaic→exposure
  mapping is in hand and thrown away.

## 3. What is actually missing

1. **No mosaic↔exposure relation.** Nothing joins `nircam_images` to `nircam_exposures`.
2. **No geometry on exposure rows.** Only a pointing centre — not a footprint. A NIRCam detector
   is ~2.2′ across, so a position can sit well inside an exposure whose centre is arcminutes away.
3. **No WCS on exposure rows.** This is what turns #562 from a file into a query.

## 4. Decisions

**D1 — A SIP summary table, not lossless gwcs.** There is no format that is both lossless gwcs and
readable by `astropy` alone (gwcs serializes to ASDF, which needs `asdf` + `gwcs`). We publish the
SIP representation `update_fits_wcsinfo` already writes, and record the **achieved fit residual**
per exposure so users can judge sufficiency rather than trust a blanket claim. Anyone who needs
exact geometry downloads the exposure, which is already possible. *(Decided: Akins, on #562.)*

**D2 — CON is not deployed, and should become optional to build.** For deep fields the context
array is the dominant memory cost of the drizzle — our own config comment measures an a2744
1.26 Gpix tile with 534 inputs at **17 planes = 80 GiB**, against 14 GiB for SCI+ERR+WHT combined
(`data/config_default.toml:945-950`); in practice this has reached >400 GB. Publishing it is out
of the question. Separately, `compress_context` only controls how it is *written* —
`drizzle.py:618` allocates `outctx` unconditionally even though nothing in the repo reads CON and
the underlying `Drizzle` already accepts `disable_ctx=True` (we pass it for the variance pass,
`drizzle.py:633`). A `[nircam.resample].context = false` knob is a pure memory win. **Filed
separately** — it is unrelated to #562 and worth more. Note the default backend is
`implementation = "jwst"` (`config_default.toml:968`), so the stcal-side equivalent needs its own
check.

**D3 — The relation lives in the database; the CSV is a rendered export.** Not a per-mosaic
sidecar product. A sidecar would need a layout entry, a deploy path, a staleness story, and would
answer only the per-tile question. A join table plus WCS-bearing exposure rows answers per-tile
*and* per-field, is always current, and serves exposure search at the same time. The DJA-style
`wcs.csv` becomes a formatting of `exposures ⋈ mosaic_inputs`, generated on demand.

**D4 — WCS as `jsonb`, not columns.** SIP for NIRCam runs to a high enough order that `A_i_j` /
`B_i_j` / `AP_i_j` / `BP_i_j` would add ~100 columns of mostly-null. One `wcs` jsonb keyed by FITS
card name round-trips into `astropy.wcs.WCS(fits.Header(row['wcs']))` directly. The handful of
fields we filter or sort on (roll, footprint, exposure time) are promoted to real columns.

**D5 — `manifest.json` stays local.** It is pipeline rebuild bookkeeping (hashes, mtimes, config
hash) that happens to contain the input list, not a user-facing provenance artifact. Deploy reads
it to populate the relation; it is not itself published. This also gives the backfill path: the
manifests on the reduction cluster are the source for populating the relation for every mosaic
already deployed. *(Decided: Akins.)*

**D6 — Access model: published field ⇒ public exposure catalog.** Admin-only intermediates is a
*management* constraint, not a security one — while a reduction is in progress we do not want the
portal serving half-reduced exposures. Once a field's deployment is published, any user may see
the full exposures table for that field. This makes the catalog's visibility gate identical to the
one already governing the bytes, which is the property worth having: one predicate, not two.
*(Decided: Akins.)*

> This supersedes `design-intermediate-products.md` §1 ("No public access to intermediate
> products; intermediates are admin-only for now") for the NIRCam exposure catalog. That document
> and the implemented storage RLS already disagreed — the RLS does not gate on product type for
> field deployments. D6 resolves the disagreement in favour of what the RLS does.

The following were open questions in revision 1 and are now decided (rationale inline; none is a
science call):

**D7 — The mosaic FK targets the `sci` row of `nircam_images`.** `nircam_images` is
extension-major (one row each for `sci` / `err` / `wht` / `srcmask`), and the repo already treats
the `sci` row as the mosaic's identity: the web hangs the thumbnail, quicklook and "view" action
off `extension === 'sci'` (`NircamTable.tsx:208,352`) and deploy documents the same
(`deploy/nircam.py:1098`). A mosaic-identity table would be cleaner but touches the field page,
FitsGL datasets, the upsert path and the storage-key contract for one FK. If an identity table
lands later, `mosaic_id` is re-pointed by one migration; nothing in the API surface exposes the
extension. Deploy resolves `mosaic_id` by `(field, tile, filter, pixel_scale, epoch,
extension='sci')` after `_upsert_nircam_images`; a mosaic whose `_sci.fits` upload failed has no
row that run and its relation is written on the re-run that heals it (same rule as the row itself).

**D8 — Lifecycle columns mirror `nircam_images` exactly, including the draft semantics.**
`deploy_status` on the row (no join to `deployments` in the predicate) and `deployment_id` for
provenance. `set_deployment_status` (`functions.sql:6225`) gains one more `UPDATE ... WHERE
deployment_id = p_deployment_id` for exposures beside the one it runs for `nircam_images`, so
`campfire deploy publish --field` flips both in one transaction. Deploy-side, `_upsert_exposures`
copies `_deploy_field_mosaics`' draft rule (`deploy/nircam.py:1136-1141`): on a **published**
deploy every discovered exposure gets `published` + this `deployment_id`; on a **`--draft`**
deploy only the exposures whose FITS the push plan uploaded (`plan.to_upload`, keyed back to the
record through `fits_tasks`) get `draft` + this `deployment_id`, and unchanged rows keep their
live status — otherwise a draft re-deploy would take a published field's catalog dark, the exact
failure the mosaic path already guards against. The column default is `'draft'` because a row is
written on every deploy including in-progress reductions; the migration itself sets the status of
existing rows (D14), so nothing already public goes dark.

**D9 — RLS: the `nircam_images` policy, verbatim, with the link-account branch.** Revision 1's
predicate omitted share-link accounts. Every NIRCam policy carries one (`policies.sql:868-877`),
and `supabase/tests/check_share_link_scoping.sql` exists precisely because a missed branch is a
silent successful read for a link — a field link would otherwise see every published field's
exposures. Mirroring the row-level `deploy_status` also keeps the predicate a one-column test,
which is why D8 keeps the status on the row rather than joining `deployments`. Full text in §5.3.

**D10 — Footprint as a native PostgreSQL `polygon`, GiST-indexed; no bbox columns.** Postgres'
built-in `polygon` / `point` types with a GiST index give an indexed bbox prefilter (`&&`) and an
exact planar containment test (`@>`) with **no extension**, which is what #309 deferred on. At
NIRCam scales the planar approximation is far below any relevant tolerance (a 2.2′ great-circle
edge deviates from its RA/Dec chord by ~10 µas; no CAMPFIRE field is near a pole or the RA=0
wrap). `ra_min/ra_max/dec_min/dec_max` are `box(footprint)` and are dropped. The vertices are
computed **deploy-side from the published SIP WCS** (`astropy.wcs.WCS(hdr).calc_footprint()`,
deploy already depends on astropy), not copied from `S_REGION`: nothing in `align` or
`wcs_shift` refreshes `S_REGION` after correcting the gwcs (no `update_s_region_imaging` call
outside `drizzle.py:321`, which is the *mosaic*), so the header value is the ground-system
footprint, offset from the corrected WCS by the alignment shift. `S_REGION` still rides inside the
`wcs` jsonb because it is in the card whitelist; the docs say which one is which. Deploy unwraps
a polygon straddling RA=0 into a continuous range and the query tests the point at `ra` and
`ra±360`, so the guard costs nothing even though no current field needs it.

**D11 — Orientation column is `roll_ref` (`ROLL_REF`, SCI header).** Not `PA_V3` / `PA_APER`.
`ROLL_REF` is per-detector, is already in the WCS card whitelist (so it is in the jsonb anyway —
promoting it is a copy, not a new read), and `align` already depends on it existing
(`apply.py:348`, `wi['roll_ref']`). All three keywords live on the SCI extension per the
stdatamodels `wcsinfo` schema (fetched 2026-09-15). It is the *nominal* pre-alignment roll; the
exact post-alignment orientation is the CD matrix in the jsonb.

**D12 — Exposure time is `EFFEXPTM` (primary header).** That card is `meta.exposure.exposure_time`,
which is exactly what the campfire drizzle reads for exposure-time weighting
(`drizzle.py:495`), so a user re-weighting by our number reproduces ours. `XPOSURE` (SCI,
`effective_exposure_time`) is what DJA exports; it differs from `EFFEXPTM` only by readout
bookkeeping and can be added to the CSV later without a schema change if anyone asks.

**D13 — The card whitelist is `campfire.storage.hashing._WCS_KEY_RE`, which already exists.**
Revision 1 asked where the regex should live so deploy could use it without importing the
pipeline; it already lives client-side (`python/campfire/storage/hashing.py:120`) as the mirror of
`manifest._WCS_KEY_RE`, and `python/tests/test_nircam_deploy.py::test_exposure_identity_matches_pipeline_recipe`
pins the two together. The jsonb carries those cards from the SCI header, with the primary
header filling only keys the SCI lacks (the digest scans both, `_WCS_HEADER_EXTS = (0, 'SCI')`),
plus `NAXIS1`/`NAXIS2`, which are outside the whitelist but required for `calc_footprint` and the
CSV. `wcs_hash` on the row is computed from the same open via `_wcs_digest_from_hdul`
(`hashing.py:140`) so it is bit-identical to the registry's.

**D14 — Backfill in two halves: lifecycle by SQL in the migration, WCS + relation by re-deploy.**
Which exposures are public is already knowable server-side: an exposure row is published iff its
`nircam_exposure` registry object (`storage_objects.product_type = 'nircam_exposure'`, `status =
'active'`, matched on `(field, filter, exposure_ref = filename)`) hangs off a `published`
deployment. A hand-authored data migration (per the AGENTS.md convention for what `db diff`
cannot generate) sets `deploy_status` and `deployment_id` from that join; rows with no active
object stay `draft`, which is correct — their bytes are not public either. The WCS columns and the
relation need the files, and `campfire deploy --field X` already reads every exposure header and
every manifest and dedup-skips every byte, so **the backfill is a re-deploy per field**, not a
new script. That keeps one code path and one set of tests.

**D15 — API: three GET routes backed by three RPCs that take the visibility scope explicitly.**
`/api/v1/*` routes run on the service client (RLS bypassed) and pass scope into RPCs
(`/api/v1/objects/route.ts`, `filter_accessible_storage_keys`), so the exposure RPCs take
`p_include_unpublished` (route sets it only for admins) and `p_link_field` (the link account's
field, from `getAccessContext`), and filter `deploy_status = 'published'` otherwise — the same
one-column predicate as the RLS, restated once. The cookie-authenticated web read path
(`/api/nircam/exposures`, D-C) calls the same RPC through the user's RLS client via a shared
`web/lib/server/nircam-exposures.ts`. The list route pages with the D-F keyset cursor. Shapes in
§8.1.

**D16 — The CSV is DJA's column set, verbatim, plus CAMPFIRE columns appended.** A real DJA
`_wcs.csv` (fetched 2026-09-15: `abell1689-grizli-v7.4-f115w-clear_wcs.csv`) has header
`file,ext,exptime,` + `astropy.wcs.WCS.to_header(relax=True)` keys lowercased (`wcsaxes, crpix1,
…, a_order, a_0_2, …, b_order, …`) + `naxis,naxis1,naxis2,sipcrpx1,sipcrpx2`. We emit exactly that
prefix so anything that reads DJA's file reads ours, then append `exposure_id, detector, filter,
sip_max_resid, wcs_hash, stale_wcs`. Columns are the union over the rows returned (SIP order can
differ per exposure), blank where a card is absent; `file` is `filename + '.fits'` and `ext` is
`1`, as DJA writes them.

## 5. Schema

### 5.1 `nircam_exposures` — additions

```
deployment_id     integer REFERENCES deployments(id) ON DELETE SET NULL  -- as nircam_images
deploy_status     text NOT NULL DEFAULT 'draft'
                  CHECK (deploy_status IN ('draft','published','revoked'))
wcs               jsonb             -- whitelist cards + NAXIS1/2, keyed by FITS card name (D4, D13)
wcs_hash          text              -- same digest as storage_objects.wcs_hash (D13)
sip_max_resid     double precision  -- SIPMXERR: max |SIP − gwcs| over the fit grid, pixels; NULL = not recorded (§6.1)
sip_max_inv_resid double precision  -- SIPIVERR: inverse-fit counterpart; NULL likewise
footprint         polygon           -- corners from the SIP WCS, ICRS degrees, RA-unwrapped (D10)
roll_ref          double precision  -- ROLL_REF, degrees (D11)
exposure_time     double precision  -- EFFEXPTM, seconds (D12)
```

`deployment_id` + `deploy_status` mirror `nircam_images` (`tables.sql:852-853`, FK at `:2236`)
so `set_deployment_status` flips exposures with the same batch that flips mosaics — one lifecycle,
not two (D8).

`wcs` is `NULL` when the SCI header carries no celestial WCS or when the pipeline flagged the
SIP refresh as failed (§6.1). `footprint` is `NULL` whenever `wcs` is. Rows with a null `wcs` are
still listed — the user can still learn the exposure exists and download it — and the CSV leaves
the WCS columns blank for them rather than dropping the row.

Indexes: `USING gist (footprint)` (the #309 prefilter, free), `(deploy_status, field, filter)`
for the public list, and the existing `(field, filter)` / partial `review_status` indexes stay
(`indexes.sql:368-375`). No new unique constraint — `nircam_exposures_unique (field, filter,
filename)` exists (`tables.sql:1958`) and is what `_upsert_exposures` already upserts on
(`deploy/nircam.py:859-863`).

### 5.2 `nircam_mosaic_inputs` — new

```sql
CREATE TABLE nircam_mosaic_inputs (
  mosaic_id       integer NOT NULL REFERENCES nircam_images(id)    ON DELETE CASCADE,  -- the sci row (D7)
  exposure_id     integer NOT NULL REFERENCES nircam_exposures(id) ON DELETE CASCADE,
  input_wcs_hash  text,          -- manifest inputs[].wcs_hash: the WCS the drizzle actually consumed
  PRIMARY KEY (mosaic_id, exposure_id)
);
CREATE INDEX ON nircam_mosaic_inputs (exposure_id);   -- reverse direction; the PK covers the forward one
```

`input_wcs_hash` is the one provenance field worth carrying: the manifest records each input's
astrometric digest at drizzle time (`manifest.py:168`), and `align` can re-solve an exposure
without rebuilding every tile it touched. `input_wcs_hash IS DISTINCT FROM e.wcs_hash` is
therefore an exact "this frame moved since the mosaic was built" signal — surfaced as `stale_wcs`
in the API and CSV, the same shape as `stale_redshift` on the line-fit catalog. Manifests written
before `wcs_hash` existed carry `NULL`, which reads as "unknown", never as "fresh".

Deliberately **not** carrying a `ctx_id` column. The drizzle context bit is only meaningful
alongside a CON array we have decided not to publish (D2), and it is not simply the input's
ordinal — `drizzle_tile` skips inputs with no tile overlap (`drizzle.py:647`) and the bit is
assigned per successful `add_image`. Recording a number nobody can join against invites misuse.

The relation for a mosaic is **replaced**, not merged, on every deploy: delete the mosaic's rows
and insert the manifest's set in one batch. A re-combine that drops an input must remove it from
the relation; a bare upsert would let it linger.

### 5.3 RLS

Replace `admin_select_exposures` with the `nircam_images` SELECT policy, verbatim (D9):

```sql
CREATE POLICY "authenticated_select_exposures"
  ON nircam_exposures FOR SELECT TO authenticated
  USING (
    CASE WHEN (SELECT public.is_link_account()) THEN
      field = (SELECT public.link_field())
      AND (deploy_status = 'published'
           OR (deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
    ELSE
      deploy_status = 'published' OR (SELECT public.is_admin())
    END
  );
```

INSERT/UPDATE policies stay admin-only — triage writes and the login-mode deploy are unchanged.
`nircam_mosaic_inputs` gets a SELECT policy requiring **both** ends visible (`EXISTS` against
`nircam_exposures` and `nircam_images`, each under its own RLS): a draft mosaic's input list
would otherwise disclose that the mosaic exists. Admin-only INSERT/DELETE. Grants: `authenticated`
+ `service_role`, no `anon` — `nircam_exposures` has never been granted to `anon`
(`tables.sql:2604`) and every policy here is `TO authenticated`, so the floor stays "logged in",
matching the field page.

`get_admin_exposures`, `get_admin_exposure_neighbors` and `get_admin_exposure_facets` keep their
admin `RAISE`: they return the triage columns and back `/admin/nircam`, which is unchanged. The
public read path is the new RPCs (D15), which project only the public columns.

Add a case to `supabase/tests/check_share_link_scoping.sql`: a field link sees its own field's
published exposures and mosaic inputs, and nothing from another field.

Keep this in lock-step with `filter_accessible_storage_keys` and
`get_storage_objects_for_sync`; the whole point of D6 is that they agree. One known, accepted
divergence: the storage functions gate on `deployments.status` of the object's *current*
deployment while the catalog gates on the row's `deploy_status`; `set_deployment_status` and
`set_active_deployment` keep those equal on the deploy path, and D14's migration seeds them equal.

## 6. Pipeline & deploy

### 6.1 Populate the WCS at discovery

Extend `_read_exposure_metadata` (`deploy/nircam.py:205`) to carry, from the same open it already
does: the `_WCS_KEY_RE` card set (D13) + `NAXIS1`/`NAXIS2` as `wcs`, `wcs_hash` via
`_wcs_digest_from_hdul`, `roll_ref` (SCI `ROLL_REF`), `exposure_time` (primary `EFFEXPTM`), the
SIP residual cards (below), and `footprint` from `WCS(hdr).calc_footprint()` (D10). Parsing
`S_REGION` is not needed for the footprint; do **not** import `nircam/geometry.py` for it — that
module needs `shapely`, a pipeline-only dependency, and deploy must not import the pipeline.

**Error handling.** The function currently swallows every exception and returns a row with nulls,
which is right for `date_obs` and wrong for the WCS: a header that cannot be parsed must be a
counted warning, not a silent null. Deploy logs the number of exposures with `wcs IS NULL` and
the first few filenames, and continues — a bad header must not sink a multi-GB deploy, but it
must not vanish either.

**The SIP residual (D1).** Verified from source (fetched 2026-09-15): gwcs' `to_fits_sip` writes
`SIPMXERR` ("Max diff from GWCS (equiv pix)") and `SIPIVERR` (inverse) into the header it returns;
jwst's `update_fits_wcsinfo` (`max_pix_error=0.01`, `max_inv_pix_error=0.01`, `npoints=12`,
`degree=None` → lowest degree that meets the bound) copies that whole header into
`meta.wcsinfo.instance`, and `align` calls it with those defaults (`apply.py:411`). But the
stdatamodels `wcsinfo` schema has **no** `sipmxerr`/`sipiverr` attribute, so on `model.save` those
two keys should land only in the ASDF extension, not the SCI FITS header — which is not readable
by deploy. So:

- **Verification step (E4, first task):** open one canonical exposure aligned by the current
  pipeline and check the SCI and primary headers for `SIPMXERR`. *If present*, read it and skip the
  next bullet. *If absent* (expected), `_write_solution` stamps `SIPMXERR` / `SIPIVERR` from the
  header `update_fits_wcsinfo` returns onto the **primary** header via the `header_updates` path
  it already uses for `CFP_ALGN` (`apply.py:419`, `atomic_save` writes them to the primary), and on
  the `except (ValueError, RuntimeError)` branch — where the SIP cards are left **stale** relative
  to the corrected gwcs — stamps `SIPSTALE = T`. That is a header-only pipeline change:
  **Infrastructure**, PATCH.
- Deploy reads `SIPMXERR`/`SIPIVERR` from either header into `sip_max_resid` /
  `sip_max_inv_resid` (NULL when absent — every exposure aligned before the stamp existed) and
  publishes `wcs = NULL` when `SIPSTALE` is set: we never publish a SIP we know disagrees with the
  authoritative gwcs.
- The user-facing docs state the fit bound (0.01 px, the jwst default the pipeline uses) for every
  row and the achieved value where recorded. Rows with `NULL` residual predate the stamp; a
  re-align refreshes them.

This is the astrometric half of "SIP is an approximation" in §9, made concrete.

### 6.2 Populate the relation at deploy

`discover_mosaics` already loads every manifest. Carry `m['inputs']` forward (the manifest is read at
`deploy/nircam.py:932` and nothing past the name check touches it) and, after
`_upsert_nircam_images`, resolve each mosaic's `sci` row id (D7) and each input to an exposure
row, then replace that mosaic's `nircam_mosaic_inputs` rows (§5.2).

**Join-key gotcha:** `nircam_exposures.filename` strips the extension
(`basename = name.removesuffix('.fits')`, `deploy/nircam.py:196`), while manifest inputs store
`os.path.basename(f)` *with* `.fits` (`manifest.py:164`). Strip on the manifest side (the same
rule `registry._exposure_ref_for` applies, `registry.py:104`) and assert that every resolved key
matched an exposure discovered in the same run.

Resolve unmatched inputs loudly: an input in a manifest with no exposure row means the exposure
was never deployed (or was deployed under a different field/filter). Log the count and the first
few; do not drop silently. Do not fail the deploy — a stale manifest on disk must not block a
field.

Note the relation records the inputs the resample step **selected** for the tile (the
`S_REGION` pre-filter, `geometry.select_overlapping_by_sregion`), which is a superset of the
inputs `drizzle_tile` actually drizzled — it skips any whose exact pixel map misses the tile
(`drizzle.py:647`). The difference is edge frames only, and the per-pixel footprint test resolves
it, but the docs should say "selected for" rather than "drizzled into".

### 6.3 Backfill

Two halves (D14):

1. **Lifecycle** — in the migration: `deploy_status` / `deployment_id` from the
   `storage_objects` join. No files, no cluster, runs on merge. Verify the row counts in the
   migration's own `RAISE NOTICE` (published / draft / unmatched) against `campfire status`.
2. **WCS + relation** — `campfire deploy --field X` per published field on the reduction cluster.
   Bytes dedup-skip; the catalog rows come along. The manifests (D5) and exposure headers are the
   only sources, so **this must run before any `campfire drop-local --field X`** — a field whose
   tree has been pruned can recover its exposures with `pull --intermediate` but not its
   manifests, and its relation then waits for the next re-combine. Record which fields have been
   backfilled in the tracking issue.

## 7. Mosaic header provenance (adjacent, ships independently)

Users downloading a deployed mosaic today get a WCS and essentially no provenance:

- `pixfrac` / `kernel` / `weight_type` **are** recorded (`drizzle.py:241-243` →
  `PIXFRAC`/`KERNEL`/`WEIGHTYP`; `NDRIZ` from `pointings`, `:246`) into the i2d primary header —
  but the i2d is the one extension deploy withholds, and the split step copies only the SCI
  *extension* header (`steps/resample.py:444-460`), so `_sci/_err/_wht` lose `PIXFRAC`,
  `KERNEL`, `WEIGHTYP`, `NDRIZ`, `CMPFRVER`, `CAL_VER`, `CRDS_CTX`. Deploy's own comment
  acknowledges this (`deploy/nircam.py:884-886`).
- **`good_bits` is not recorded anywhere** by the campfire backend (no stamp in
  `campfire_pipeline/`; the stdatamodels `resample` schema has no such keyword either, so the
  jwst backend's i2d is not expected to carry one — verify on one jwst-backend i2d when E1 lands),
  despite determining which input pixels were eligible at all.

Fix: copy the informative primary-header cards onto the SCI header at split time (the split is
common to both backends), and stamp `GOODBITS` from config there. A handful of lines, and it is
what makes a downloaded mosaic self-describing enough to re-drizzle against. Worth doing
regardless of the rest of this design. The `SIPMXERR`/`SIPSTALE` stamp from §6.1 rides the same
pipeline PR.

Pipeline changelog category: **Infrastructure** (no pixel change). Do **not** add an `S_REGION`
refresh to `align` while in there: `S_REGION` is in the WCS digest whitelist, so rewriting it
would move every exposure's `wcs_hash` and mark every tile stale (`file_unchanged`,
`manifest.py:131`) — a field-wide re-drizzle for a cosmetic card.

## 8. Surface

### 8.1 API

Per decision D-C (#506), reads are `GET` route handlers, not server actions; per D15 they call
RPCs with explicit scope.

- `GET /api/v1/nircam/exposures` — RPC `get_nircam_exposures_paginated`. Filters: `field`,
  `filter`, `detector`, `visit`, `stage`, `date_min/max`; `ra`/`dec` alone = **contains** (exact
  `footprint @> point`, the #562 question); `ra`/`dec`/`radius` = **overlaps** a box of that
  radius (RA half-width scaled by `1/cos(dec)`) — a browse aid, documented as approximate.
  Sort: `date_obs` | `filename` | `exposure_time`, tiebreak `id`. Cursor pagination per D-F
  (#511): `p_after_sort_text` / `p_after_sort_num` / `p_after_tiebreak`, `pagination.next_cursor`,
  fingerprinted cursor, count on the first page only. Public columns only (no `review_status`,
  `correction`, `mask_regions`, `notes`, `png_path`); `include_unpublished=true` for admins.
- `GET /api/v1/nircam/mosaics` — the `sci` rows with their natural key (`field, tile, filter,
  pixel_scale, epoch`) and `n_inputs`, so a client can resolve an id from what a user actually
  knows (a tile name). Optional `field`/`tile`/`filter` filters; small, unpaginated per field.
- `GET /api/v1/nircam/mosaics/{id}/inputs` — RPC `get_nircam_mosaic_inputs`. `format=json`
  (default) returns exposure rows + `stale_wcs`; `format=csv` renders D16's table with
  `Content-Disposition: attachment; filename="<mosaic_name>_wcs.csv"`. The CSV is built in the
  route from the same rows — no second query shape.
- `GET /api/v1/nircam/exposures/{id}/mosaics` — RPC `get_nircam_exposure_mosaics`, the reverse
  direction, with `stale_wcs` per mosaic.

Response types exported from each route; `Cache-Control: private` (+ `Vary: Cookie` on the
cookie-path routes); the web client fetches via `fetchJson()` inside a `useQuery` keyed on *what*,
never on the viewer. `/api/v1/version` does not need a floor bump — no existing client call
changes shape.

### 8.2 Python client

`Campfire.query_exposures(...)` / `iter_exposures(...)` alongside `query_objects` /
`query_spectra` / `query_lines` (`python/campfire/client.py`), returning an astropy `Table` with
the pagination block in `Table.meta` and `wcs` as a dict column;
`Campfire.get_mosaic_inputs(field, tile, filter, pixel_scale=None, epoch='')` resolves through
`/nircam/mosaics` then `/inputs`. A new `campfire nircam` click group in `python/campfire/cli.py`
(registered like `deploy` / `fitsgl`, `cli.py:187-243`) with
`campfire nircam wcs --field X --tile Y --filter F [--pixel-scale S] [--epoch E] [-o out.csv]`
writing the D16 CSV — that is the shape #562 actually asked for, and it composes with
`campfire pull --field X --intermediate` for anyone who then wants the frames.

An `exposures` stream on `/api/v1/sync/*` is **not** in v1: the query API covers the use case,
the sync streams already run catalog-wide counts on their first page, and the local mirror has
no consumer for it yet. Revisit if `campfire pull` wants to plan by footprint.

### 8.3 Web

The admin exposure browser (`web/lib/actions/nircam-exposures.ts`, `/admin/nircam`) is
**unchanged** — it is a triage tool and keeps `get_admin_exposures`. The public surface is
additive: on the NIRCam field page a mosaic row (`NircamTable.tsx`, the `sci` row) gains
"Frames (N)" linking to a per-mosaic exposure list and a CSV download; the exposure list is a new
route-handler-fed table with the public columns, filter/sort, and the reverse link. No
un-gating of the existing admin actions is needed, which removes the "hide triage columns from
non-admins" work revision 1 planned.

## 9. What this gives you — and what it does not

State these in the user-facing docs, not just here.

- **Geometric coverage, not actual contribution.** The catalog says which frames *cover* a pixel
  (footprint test) and which frames were *selected* for a tile (relation, §6.2). It cannot say
  which frames *contributed* to it — DQ flags, `good_bits='~DO_NOT_USE'`, outlier rejection and
  manual masks all drop pixels after the geometry is decided, and edge frames selected by
  `S_REGION` can be skipped by the drizzle. CON is the only exact record and we are not publishing
  it (D2).
- **SIP is an approximation of the authoritative gwcs.** Fitted to a 0.01 px bound (jwst default,
  §6.1); the achieved maximum residual is published per exposure where the pipeline recorded it
  and is `NULL` for exposures aligned before the stamp existed. Sufficiency is the user's call
  (D1). ~0.01 px is 0.3 mas (SW) / 0.6 mas (LW) — not the limiting term for any PSF work we can
  imagine, but stated so nobody has to trust us.
- **The footprint is derived from the SIP WCS; `S_REGION` in the jsonb is the ground-system
  value** and is not refreshed by alignment (D10). They differ by the alignment shift.
- **`stale_wcs` means the frame was re-aligned after the mosaic was built.** The catalog's WCS is
  the current one; the mosaic was drizzled with the older one. The `input_wcs_hash` is published
  so the condition is checkable, but the older WCS itself is not recoverable from the catalog.
- **Weighting is not reconstructible from the catalog.** `weight_type='ivm'` means per-pixel
  inverse-variance maps that no table can carry. Exposure-time weighting (`EFFEXPTM`, D12)
  approximates it; exact re-drizzling needs the exposure files, which are downloadable.
- **Draft exposures of a published field are invisible.** A `--draft` re-deploy stages changed
  frames as `draft` (D8); until publish, the public relation for an affected mosaic omits them
  (both ends must be visible, §5.3). `n_inputs` on `/nircam/mosaics` is the visible count.

### Open questions (science — for Akins)

Everything SQL/Python/TypeScript-shaped is decided above. What remains needs an astronomer:

1. **Is a 0.01 px SIP bound acceptable to publish as "the" WCS?** The fit residual is bounded
   at 0.01 px (0.3–0.6 mas) by construction and reported per exposure where stamped. If the answer
   is "no, PSF drizzling needs the gwcs", D1 is wrong and this becomes a per-exposure ASDF
   sidecar. *Recommendation: yes — it is below the alignment accuracy by orders of magnitude; the
   exposure files remain downloadable for anyone who disagrees.*
2. **Is "selected for the tile" an acceptable relation semantics** given that the drizzle can
   skip edge frames (§6.2), or must the relation be exact? Exact needs the drizzle to write its
   actual `add_image` list into the manifest — a small pipeline change, but every tile must be
   re-combined before the relation is exact. *Recommendation: publish "selected", document it,
   and add the drizzled list to the manifest opportunistically so future rebuilds tighten it.*
3. **Orientation column:** nominal `ROLL_REF` (D11) is what users expect to see in a table;
   the exact post-alignment PA is in the CD matrix. Is a nominal value in a column labelled
   `roll_ref` acceptable, or should the promoted column be recomputed from the corrected WCS?
   *Recommendation: `roll_ref` as-is; the correction is sub-arcminute in angle.* (I could not
   verify the typical alignment rotation from the repo; if it is ever large enough to matter for
   display, recompute deploy-side.)
4. **Exposure time:** `EFFEXPTM` alone (D12), or also `XPOSURE` for DJA parity in the CSV?
   *Recommendation: `EFFEXPTM` only until someone asks.*

## 10. Work breakdown

| # | Unit | Depends on | Notes |
|---|---|---|---|
| E1 | Pipeline header PR: primary cards → SCI at split, stamp `GOODBITS`; `SIPMXERR`/`SIPIVERR`/`SIPSTALE` stamp in `align` (after the §6.1 verification) | — | §6.1, §7; one Infrastructure changelog entry; no `S_REGION` refresh |
| E2 | `[nircam.resample].context = false` knob | — | §D2, separate issue, memory win |
| E3 | Schema: exposure columns + `nircam_mosaic_inputs` + RLS (D9) + `set_deployment_status` flips exposures + the three RPCs (D15) + link-scoping test case | — | §5; declarative schema + generated migration, **plus** the hand-authored lifecycle data migration (D14) in the same PR |
| E4 | `_read_exposure_metadata` reads WCS/footprint/roll/exptime/residual; draft-aware `_upsert_exposures` (D8) | E3 | §6.1; starts with the `SIPMXERR` header check |
| E5 | Deploy carries `inputs[]` through `discover_mosaics` and replaces `nircam_mosaic_inputs` | E3, E4 | §6.2 |
| E6 | API routes + `web/lib/server/nircam-exposures.ts` + CSV renderer | E3 | §8.1; can ship before E4/E5 — rows are public from E3, WCS fills in as fields re-deploy |
| E7 | Backfill: re-deploy each published field on the cluster | E4, E5 | §6.3; before any `drop-local`; tracked per field |
| E8 | Python client + `campfire nircam wcs` CLI | E6 | §8.2 — closes #562 |
| E9 | Web: frames link + CSV download on the field page, public exposure list | E6 | §8.3 |

E1 and E2 are independent and can go first; E1 should land *before* E7 so the backfill re-deploy
picks up the residual stamps on any exposure re-aligned in between (older exposures stay `NULL`
either way). E3's RLS flip is safe to ship alone: the data migration marks published rows
published in the same deploy, and rows without a WCS are simply rows without a WCS.

## 11. Definition of done

- A logged-in user, with no admin flag, can list the exposures of a published field, filter them,
  and ask which exposures contain a sky position.
- From a mosaic they can retrieve the contributing frames with full SIP WCS as a DJA-shaped CSV,
  and from a frame the mosaics it contributed to, each row carrying `stale_wcs`.
- `campfire nircam wcs --field X --tile Y --filter F` writes that CSV locally.
- Exposures of an unpublished (in-progress) field, and draft-staged exposures of a published
  field, remain invisible to non-admins; a field share link sees only its field.
- `campfire deploy publish --field X` / `revoke` flip exposures and mosaics in one transaction.
- A deployed `_sci.fits` carries its drizzle parameters and `GOODBITS`.
- Every published exposure row has `deploy_status = 'published'` after the migration, with no
  re-deploy; the relation and WCS are populated for every already-deployed mosaic via E7.
- The three visibility predicates (exposure RLS, `filter_accessible_storage_keys`,
  `get_storage_objects_for_sync`) agree, and `check_share_link_scoping.sql` covers the new table.

## 12. Out of scope

- Publishing CON, in any form (D2).
- The indexed spherical-polygon overlap query — still #309 for anything the planar `polygon`
  GiST index cannot express (fields at the pole, all-sky joins). The `footprint` column here is
  its groundwork and already answers the point-in-footprint question.
- A lossless gwcs/ASDF bundle per mosaic (D1). Reconsider if science question 1 says the SIP is
  inadequate.
- An `exposures` sync stream (§8.2).
- NIRSpec exposure-level equivalents.

## 13. Revision-2 corrections to revision 1 (verified against the tree, 2026-09-15)

Recorded so reviewers do not re-derive them.

- **"`nircam_exposures` has no uniqueness on `(field, filter, filename)`"** — wrong.
  `nircam_exposures_unique` exists (`tables.sql:1958-1959`) and `_upsert_exposures` upserts on it
  (`on_conflict='field,filter,filename'`, `deploy/nircam.py:862`). The "check for duplicates"
  step is deleted.
- **"Where should `_WCS_KEY_RE` live so deploy can use it"** — it already lives in
  `python/campfire/storage/hashing.py:120` with a drift test (D13).
- **"Derive the bbox with `nircam/geometry.py`"** — that module imports `shapely`, which the
  client package does not depend on (`python/pyproject.toml`), and deploy must not import the
  pipeline. Replaced by `astropy.wcs` deploy-side (D10).
- **The §5.3 predicate omitted share-link accounts** and joined `deployments` where every sibling
  policy reads the row's own `deploy_status`. Replaced by the `nircam_images` policy verbatim (D9).
- **"`S_REGION` — footprint, verbatim from the header"** would have published the pre-alignment
  footprint: no code path refreshes `S_REGION` after `align`/`wcs_shift` (D10).
- **`pa_v3`** — resolved to `ROLL_REF` (D11); `PA_V3` and `PA_APER` are also SCI-header cards per
  the stdatamodels schema, so none of the three needed a header sample to locate.
- **`wcs_residual`** — gwcs does compute it (`SIPMXERR`), jwst does copy it into `meta.wcsinfo`,
  but the stdatamodels schema does not map it to a FITS card, so it is not expected on disk; §6.1
  turns that into a verification step with a concrete pipeline fallback. The claim that
  `update_fits_wcsinfo` "fits SIP to a tolerance" is confirmed (0.01 px, `degree=None`).
- **"Copy only the SCI extension header (`resample.py:444`)"** — the file is
  `nircam/steps/resample.py`, lines 444-460; the claim itself is correct.
- **`drizzle.py:619` / `:634` / `:645`** — now `:618` / `:633` / `:647`; claims correct.
- **§8.3 "un-gate the browser, hide triage columns"** — unnecessary: the admin browser is a
  separate RPC and stays; the public surface is additive (§8.3).
- **§6.3 "a backfill script"** — replaced by the migration-side lifecycle backfill plus a
  re-deploy per field (D14); no new script.
- Not a correction but a hazard rev. 1 missed: **draft re-deploys** would have taken a published
  field's exposure catalog dark under a naive `deploy_status` column (D8), and a bare upsert of
  the relation would have kept dropped inputs forever (§5.2).
