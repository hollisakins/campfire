# Wisp template hosting — public R2 bucket setup

**Goal:** serve the NIRCam wisp templates from a *completely public* (anonymous,
no-auth) HTTPS endpoint so `cfpipe` can fetch them on any machine with no
campfire login, no cloud credentials, and no manual file copying. The pipeline
fetches each file with a plain `GET {base_url}/{filename}`, verifies it against a
checksummed manifest shipped inside the package, and caches it under
`$CAMPFIRE_ROOT/cache/wisps/`.

This doc is the **infra half** (stand up the bucket + upload). The **code half**
(manifest, fetcher, preflight, fail-loud step) is being built in parallel and
needs only the finished `base_url` from you.

---

## 1. What the pipeline expects from the endpoint

- **Anonymous HTTPS GET.** No signing, no headers, no auth. `curl -fSL <url>`
  from a fresh machine must return the file.
- **Flat filename namespace under one base URL.** The pipeline requests
  `{base_url}/{filename}` — no per-detector or per-filter subdirectories in the
  request. Put a version segment in the path (see §4) and treat everything below
  it as flat.
- **Exact filenames.** The step derives filenames from `(detector, filter)`; they
  must match **byte-for-byte**, uppercase:

  ```
  WISP_<DET>_<FILT>_CLEAR_masked.fits
  WISP_<DET>_<FILT>_CLEAR_masked_smoothed_1x1.fits
  WISP_<DET>_<FILT>_CLEAR_masked_smoothed_2x2.fits
  WISP_<DET>_<FILT>_CLEAR_masked_smoothed_3x3.fits
  ```

  - `<DET>` ∈ `NRCA3, NRCA4, NRCB3, NRCB4` (the only detectors that carry wisps).
  - `<FILT>` is the SW filter, uppercase, e.g. `F150W`, `F200W`, `F115W`.
  - **All four smoothing variants** must exist for every `(det, filt)` you
    publish — the fetcher validates the complete set up front and hard-fails if
    any variant is missing (this closes a latent bug where a missing smoothed
    variant used to crash mid-run).
  - Example: `WISP_NRCA3_F150W_CLEAR_masked_smoothed_2x2.fits`.

- **Content-Type is irrelevant** to the fetch (server-side `urllib`, not a
  browser) — `application/fits` or `application/octet-stream` is fine. **No CORS
  config is needed.**

---

## 2. Create the bucket

Cloudflare dashboard → **R2** → **Create bucket** (e.g. `campfire-wisps`).
Location: automatic. This bucket holds *only* the public wisp templates — keep it
separate from your private data bucket so "public" is a property of the whole
bucket, not per-object ACLs.

---

## 3. Make it public

R2 buckets are private by default. Pick **one**:

### Option A — Custom domain (recommended)
Bucket → **Settings → Custom Domains → Connect Domain**, e.g.
`wisps.your-domain.org`. Cloudflare provisions the cert and CDN. This gives a
clean, stable, cacheable URL you control and can keep alive independent of any
account the reducer uses.
→ `base_url` root becomes `https://wisps.your-domain.org`

### Option B — `r2.dev` managed subdomain
Bucket → **Settings → Public Development URL → Allow Access**. Cloudflare gives a
`https://pub-<hash>.r2.dev` URL. Zero DNS setup, but the URL is opaque and
rate-limited/not-for-production per Cloudflare's own guidance — fine to start,
worth migrating to a custom domain later.
→ `base_url` root becomes `https://pub-<hash>.r2.dev`

**Recommended cache header:** since templates at a versioned path are immutable
(see §4), set a long `Cache-Control` so the CDN and clients cache aggressively.
Add a bucket rule or set per-object on upload:
`Cache-Control: public, max-age=31536000, immutable`.

---

## 4. Version the path (immutable URLs on R2)

R2 has no built-in record versioning, so version by **path prefix** and never
overwrite a published file. Upload under:

```
wisps/<version>/WISP_...fits
```

e.g. `wisps/2026.1/WISP_NRCA3_F150W_CLEAR_masked.fits`. Then:

```
base_url = "https://wisps.your-domain.org/wisps/2026.1"
```

When you regenerate templates, upload under a **new** prefix (`wisps/2026.2/…`)
and bump `base_url` in the committed manifest. Old pipeline versions keep
resolving their pinned URLs; nothing is ever mutated in place. This is what gives
you reproducibility without R2 object versioning.

---

## 5. Upload the files

Upload the **exact** local template files you will run the manifest builder
against (§6) — the manifest's checksums must match what's served. Any S3-compatible
tool works against R2's S3 endpoint. Example with `rclone` (configure an R2 remote
with your S3 access keys first — these keys are for **upload only** and never
touch the pipeline):

```bash
rclone copy ./local_wisps/ r2:campfire-wisps/wisps/2026.1/ \
  --header-upload "Cache-Control: public, max-age=31536000, immutable" \
  --progress
```

or the AWS CLI against the R2 S3 endpoint:

```bash
aws s3 cp ./local_wisps/ s3://campfire-wisps/wisps/2026.1/ \
  --recursive --endpoint-url https://<accountid>.r2.cloudflarestorage.com \
  --cache-control "public, max-age=31536000, immutable"
```

Only upload the four detectors' files (`NRCA3/4`, `NRCB3/4`); nothing else is
requested.

---

## 6. Hand off to the code side

Once uploaded and public, do two things:

1. **Verify anonymous access** from a machine with no credentials:
   ```bash
   curl -fSL "https://wisps.your-domain.org/wisps/2026.1/WISP_NRCA3_F150W_CLEAR_masked.fits" \
     -o /tmp/wisp_test.fits && echo OK
   ```
   A `403` means the bucket/domain isn't actually public yet; a `200` with the
   file is what we need.

2. **Give me the `base_url`** (the full versioned prefix). The manifest builder
   (`scripts/build_wisp_manifest.py`, being added) computes `sha256` + byte size
   from your local template dir and writes
   `pipeline/campfire_pipeline/data/wisp_manifest.toml` with this `base_url`
   baked in. That manifest is the single committed source of truth the pipeline
   ships and validates against.

**Nothing about the reducer machine needs credentials** — the upload keys stay
with whoever publishes templates; consumers only ever do anonymous GETs.
