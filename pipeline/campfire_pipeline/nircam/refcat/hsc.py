"""
HSC SSP PDR3 catalog client.

Thin wrapper around the public async-job API at
``https://hsc-release.mtk.nao.ac.jp/datasearch/api/catalog_jobs/``.

Submit, poll, download, cancel — all POST endpoints that take a JSON
body carrying the user's HSC SSP account credentials inline (the API
does not use HTTP basic auth despite what the docs sometimes imply).

Credential resolution order (first hit wins):
    1. Explicit ``user``/``password`` arguments
    2. ``HSC_SSP_USER`` / ``HSC_SSP_PASSWORD`` env vars
    3. ``~/.netrc`` entry for machine ``hsc-release.mtk.nao.ac.jp``

DUD vs Wide layer selection is handled by the caller via
:func:`select_schemas_for_cone`, which intersects the cone's tract
footprint (queried from ``public.skymap``) with the hard-coded DUD
tract ranges below.

References:
    https://hsc-release.mtk.nao.ac.jp/doc/index.php/database__pdr3/
    https://hsc-release.mtk.nao.ac.jp/rsrc/pdr3/stored_functions.html
"""

import json
import math
import netrc
import os
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from astropy.io import ascii
from astropy.table import Table

from campfire_pipeline.common.io import log


API_BASE = "https://hsc-release.mtk.nao.ac.jp/datasearch/api/catalog_jobs"
NETRC_MACHINE = "hsc-release.mtk.nao.ac.jp"
# NAO's official example script ships this as a bare number (e.g.
# ``version = 20170216.1``). Send it as a number, not a string —
# string values trigger an unhandled-exception 500 server-side.
CLIENT_VERSION = 20170216.1
DEFAULT_RELEASE_VERSION = "pdr3"

# PDR3 DUD field footprints. Tract ranges (inclusive) are sourced from
# the official PDR3 database doc page; bounding-box RA/Dec rectangles
# are deliberately loose envelopes around each field, generous enough
# that a small cone that "just touches" a DUD field still gets routed
# to pdr3_dud_rev. DUD and Wide tract numbers do not overlap, so
# straddling cones simply produce one query per layer with no dedup.
#
# Each entry:
#   tracts -- list of (lo, hi) inclusive tract ranges
#   bbox   -- (ra_lo, ra_hi, dec_lo, dec_hi) in degrees
DUD_FIELDS = {
    "COSMOS": {
        "tracts": [(9569, 9572), (9812, 9814), (10054, 10056)],
        "bbox": (149.0, 152.5, 0.5, 4.0),
    },
    "DEEP2-3": {
        "tracts": [(9219, 9221), (9462, 9465), (9706, 9708)],
        "bbox": (350.0, 354.5, -1.5, 2.5),
    },
    "ELAIS-N1": {
        "tracts": [(16984, 16985), (17129, 17131),
                   (17270, 17272), (17406, 17407)],
        "bbox": (240.0, 244.5, 52.5, 56.0),
    },
    "XMM-LSS": {
        "tracts": [(8282, 8284), (8523, 8525), (8765, 8767)],
        "bbox": (33.0, 37.5, -6.5, -2.5),
    },
}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def resolve_credentials(user=None, password=None):
    """Return ``(user, password)`` from arg / env / netrc, in that order.

    Raises ``RuntimeError`` if no credentials can be found.
    """
    if user and password:
        return user, password

    env_user = os.environ.get("HSC_SSP_USER")
    env_pw = os.environ.get("HSC_SSP_PASSWORD")
    if env_user and env_pw:
        return env_user, env_pw

    try:
        rc = netrc.netrc()
        entry = rc.authenticators(NETRC_MACHINE)
    except (FileNotFoundError, netrc.NetrcParseError):
        entry = None
    if entry:
        rc_user, _, rc_pw = entry
        if rc_user and rc_pw:
            return rc_user, rc_pw

    raise RuntimeError(
        "HSC SSP credentials not found. Provide via --hsc-user/--hsc-password, "
        f"the HSC_SSP_USER/HSC_SSP_PASSWORD env vars, or a ~/.netrc entry for "
        f"machine {NETRC_MACHINE!r}."
    )


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

def _post(path, payload, *, parse_json=True, timeout=60):
    """POST ``payload`` (dict) as JSON to ``{API_BASE}/{path}``.

    Returns the parsed JSON response, or the raw bytes if ``parse_json``
    is False (used by the download endpoint).
    """
    url = f"{API_BASE}/{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HSC SSP {path} returned HTTP {e.code}: {detail.strip()}"
        ) from None
    if not parse_json:
        return data
    return json.loads(data.decode("utf-8"))


def _credential(user, password):
    return {"account_name": user, "password": password}


# ---------------------------------------------------------------------------
# Job API
# ---------------------------------------------------------------------------

def submit_job(sql, *, user, password,
               release_version=DEFAULT_RELEASE_VERSION,
               out_format="csv"):
    """Submit an SQL job and return its job id."""
    payload = {
        "clientVersion": CLIENT_VERSION,
        "credential": _credential(user, password),
        "catalog_job": {
            "sql": sql,
            "out_format": out_format,
            # False so the CSV body starts with the column header row.
            # When True, the server prepends a ``#``-prefixed metainfo
            # block that swallows the header line under any naive comment
            # filter — making it ambiguous where the data starts.
            "include_metainfo_to_body": False,
            "release_version": release_version,
        },
        "nomail": True,
        # Let the job submit even if the server-side SQL preview would
        # have errored — the actual error then comes through the status
        # endpoint as a structured ``error`` field instead of as an
        # opaque HTML 500 from the syntax-check handler.
        "skip_syntax_check": True,
    }
    resp = _post("submit", payload)
    job = resp.get("job") or resp
    job_id = job.get("id") or resp.get("id")
    if not job_id:
        raise RuntimeError(f"HSC SSP submit: no job id in response: {resp!r}")
    return job_id


def job_status(job_id, *, user, password):
    """Return the raw status dict for a job."""
    payload = {
        "clientVersion": CLIENT_VERSION,
        "credential": _credential(user, password),
        "id": job_id,
    }
    return _post("status", payload)


def cancel_job(job_id, *, user, password):
    """Best-effort cancel; ignores errors (cleanup path)."""
    try:
        _post("cancel", {
            "clientVersion": CLIENT_VERSION,
            "credential": _credential(user, password),
            "id": job_id,
        })
    except Exception as e:
        log(f"hsc cancel job {job_id} failed (ignored): {e}")


def download_result(job_id, *, user, password):
    """Download CSV bytes for a completed job."""
    payload = {
        "clientVersion": CLIENT_VERSION,
        "credential": _credential(user, password),
        "id": job_id,
    }
    return _post("download", payload, parse_json=False, timeout=300)


def wait_for_job(job_id, *, user, password,
                 poll_interval_s=2.0, max_interval_s=10.0,
                 max_wait_s=600):
    """Block until a job finishes; return the final status dict.

    Polls with exponential backoff up to ``max_interval_s``. Raises
    ``TimeoutError`` after ``max_wait_s`` and ``RuntimeError`` if the
    job ends in an error state.
    """
    interval = poll_interval_s
    start = time.monotonic()
    last_status = None
    while True:
        status = job_status(job_id, user=user, password=password)
        state = (status.get("status") or status.get("job", {}).get("status") or
                 "").lower()
        if state != last_status:
            log(f"hsc job {job_id}: {state or '(unknown)'}")
            last_status = state
        if state == "done":
            return status
        if state in ("error", "aborted"):
            msg = (status.get("error") or status.get("job", {}).get("error") or
                   status)
            raise RuntimeError(f"HSC SSP job {job_id} {state}: {msg!r}")
        if time.monotonic() - start > max_wait_s:
            raise TimeoutError(
                f"HSC SSP job {job_id} did not finish within {max_wait_s}s "
                f"(last status: {state!r})"
            )
        time.sleep(interval)
        interval = min(interval * 1.5, max_interval_s)


@contextmanager
def _job_lifecycle(sql, *, user, password,
                   release_version=DEFAULT_RELEASE_VERSION):
    """Submit, yield job id, ensure cancel on KeyboardInterrupt."""
    job_id = submit_job(sql, user=user, password=password,
                        release_version=release_version)
    log(f"hsc job submitted: id={job_id}")
    try:
        yield job_id
    except KeyboardInterrupt:
        log(f"hsc job {job_id}: cancelling on interrupt")
        cancel_job(job_id, user=user, password=password)
        raise


def run_sql(sql, *, user, password,
            release_version=DEFAULT_RELEASE_VERSION,
            max_wait_s=600):
    """Submit, wait, download; return the result as an astropy Table.

    The body is plain CSV (``include_metainfo_to_body=False``); we stage
    it on disk because astropy's fast C tokenizer refuses in-memory
    ``StringIO`` objects.
    """
    with _job_lifecycle(sql, user=user, password=password,
                        release_version=release_version) as job_id:
        wait_for_job(job_id, user=user, password=password,
                     max_wait_s=max_wait_s)
        csv_bytes = download_result(job_id, user=user, password=password)

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(csv_bytes)
        tmp_path = tmp.name
    try:
        return ascii.read(tmp_path, format="csv")
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# DUD / Wide coverage decision (no DB roundtrip)
# ---------------------------------------------------------------------------

def _cone_overlaps_bbox(center, radius_deg, bbox):
    """Quick on-sky check: cone (RA, Dec, r) vs (ra_lo, ra_hi, dec_lo, dec_hi).

    Pad the bbox by ``radius_deg`` (RA padding by ``1/cos(dec)``) and
    test whether the cone center falls inside. This is conservative —
    a cone whose center is outside but whose edge clips the bbox is
    still treated as overlapping — which is what we want for routing
    decisions.
    """
    ra, dec = center
    ra_lo, ra_hi, dec_lo, dec_hi = bbox
    cos_dec = max(0.05, abs(math.cos(math.radians(dec))))
    ra_pad = radius_deg / cos_dec
    # RA wraps at 360; normalize the cone RA into the bbox's continuous frame
    ra_candidates = (ra, ra + 360.0, ra - 360.0)
    if not any(ra_lo - ra_pad <= r <= ra_hi + ra_pad for r in ra_candidates):
        return False
    return dec_lo - radius_deg <= dec <= dec_hi + radius_deg


def dud_fields_overlapping(center, radius_deg):
    """Return the names of DUD fields whose footprint the cone touches."""
    return [name for name, spec in DUD_FIELDS.items()
            if _cone_overlaps_bbox(center, radius_deg, spec["bbox"])]


def cone_inside_dud_field(center, radius_deg, field_name):
    """True if the cone's *inflated* footprint sits inside one DUD bbox.

    Used to decide whether the Wide layer can be skipped in
    ``release='auto'`` — only safe when the cone fits cleanly inside a
    single DUD field's bbox, so no part of it can clip a Wide-only
    tract.
    """
    ra, dec = center
    cos_dec = max(0.05, abs(math.cos(math.radians(dec))))
    ra_pad = radius_deg / cos_dec
    ra_lo, ra_hi, dec_lo, dec_hi = DUD_FIELDS[field_name]["bbox"]
    return (ra_lo <= ra - ra_pad and ra + ra_pad <= ra_hi
            and dec_lo <= dec - radius_deg
            and dec + radius_deg <= dec_hi)


def dud_tract_envelope(field_name):
    """Inclusive (min_tract, max_tract) covering a DUD field's tracts.

    The DUD layer is partitioned per-field with non-contiguous tract
    ranges, but each field's tracts are clustered tightly enough that a
    single ``tractSearch(object_id, min, max)`` is an effective
    pre-filter on its own.
    """
    ranges = DUD_FIELDS[field_name]["tracts"]
    return min(lo for lo, _ in ranges), max(hi for _, hi in ranges)
