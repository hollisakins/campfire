"""Parity test: Image2Pipeline.call(path) vs Image2Pipeline.call(model).

Open question #1 of docs/design-nircam-exposure-major.md: the exposure-major
chain wants to hand Image2Pipeline an in-memory ImageModel instead of a path.
This verifies, on the pinned jwst version, that the two invocation modes:
  1. select the same CRDS reference files (meta.ref_file.*),
  2. produce bitwise-identical output arrays,
  3. produce the same photometry keywords and WCS,
  4. (model mode) do not mutate the caller's input model in place.
"""
import os
import tempfile
import shutil
import sys
import numpy as np

SRC = ('/Users/hba423/simmons/campfire-data/products/nircam/rj0911/f090w/'
       'jw06882025001_02101_00001_nrca1.fits')
WORKDIR = os.path.join(tempfile.gettempdir(), 'image2_parity')

os.makedirs(WORKDIR, exist_ok=True)
local = os.path.join(WORKDIR, os.path.basename(SRC))
if not os.path.exists(local):
    shutil.copy2(SRC, local)

# Production environment: same config resolution as cfpipe (CRDS_PATH,
# CRDS_CONTEXT, thread caps).
from campfire_pipeline.config import load_config, setup_environment
config = load_config()
setup_environment(config)
print(f"CRDS_CONTEXT={os.environ.get('CRDS_CONTEXT')} "
      f"CRDS_PATH={os.environ.get('CRDS_PATH')}")

import jwst
print(f"jwst {jwst.__version__}")
from jwst.pipeline import calwebb_image2
from stdatamodels.jwst.datamodels import ImageModel

# Exactly the kwargs image2_step builds (no custom flat).
KW = {
    'output_dir': WORKDIR,
    'save_results': False,
    'steps': {
        'bkg_subtract': {'skip': True},
        'assign_wcs': {
            'skip': False, 'save_results': False,
            'sip_approx': True, 'sip_degree': None, 'sip_inv_degree': None,
            'sip_max_inv_pix_error': 0.25, 'sip_max_pix_error': 0.25,
            'sip_npoints': 32, 'slit_y_high': 0.55, 'slit_y_low': -0.55,
        },
        'flat_field': {'skip': False},
        'photom': {'skip': False},
        'resample': {'skip': True},
    },
}


def unwrap(result):
    if isinstance(result, list):
        assert len(result) == 1, f"expected 1 result, got {len(result)}"
        return result[0]
    return result


print("\n=== Run A: Image2Pipeline.call(path) ===", flush=True)
res_a = unwrap(calwebb_image2.Image2Pipeline.call(local, **KW))

print("\n=== Run B: Image2Pipeline.call(model) ===", flush=True)
model_in = ImageModel(local)
# Snapshot input arrays to detect in-place mutation by the pipeline.
snap_data = model_in.data.copy()
snap_dq = model_in.dq.copy()
res_b = unwrap(calwebb_image2.Image2Pipeline.call(model_in, **KW))

print("\n" + "=" * 70)
print("PARITY REPORT")
print("=" * 70)
failures = []

# --- 1. CRDS reference selection ---
ref_a = res_a.meta.ref_file.instance
ref_b = res_b.meta.ref_file.instance
keys = sorted(set(ref_a) | set(ref_b))
print("\n[1] CRDS reference files selected:")
for k in keys:
    va, vb = ref_a.get(k), ref_b.get(k)
    same = va == vb
    if not same:
        failures.append(f"ref_file.{k}: {va} != {vb}")
    name = (va or {}).get('name', va) if isinstance(va, dict) else va
    print(f"  {'OK ' if same else 'DIFF'} {k}: {name}")

# --- 2. Output arrays, bitwise ---
print("\n[2] Output arrays (bitwise, NaN-aware):")
for attr in ('data', 'err', 'dq', 'var_poisson', 'var_rnoise', 'var_flat',
             'area'):
    a = getattr(res_a, attr, None)
    b = getattr(res_b, attr, None)
    if a is None and b is None:
        continue
    eq = (a is not None and b is not None and a.shape == b.shape
          and np.array_equal(a, b, equal_nan=np.issubdtype(a.dtype,
                                                           np.floating)))
    if not eq:
        n_diff = (np.sum(~np.isclose(a, b, equal_nan=True))
                  if a is not None and b is not None and a.shape == b.shape
                  else 'shape/None mismatch')
        failures.append(f"array {attr}: {n_diff} differing px")
    print(f"  {'OK ' if eq else 'DIFF'} {attr}")

# --- 3. Photometry keywords + units ---
print("\n[3] Photometry / units:")
for key in ('photometry.conversion_megajanskys',
            'photometry.conversion_microjanskys',
            'photometry.pixelarea_steradians', 'bunit_data', 'bunit_err'):
    parts = key.split('.')
    ga = res_a.meta
    gb = res_b.meta
    for p in parts:
        ga = getattr(ga, p, None) if ga is not None else None
        gb = getattr(gb, p, None) if gb is not None else None
    same = ga == gb
    if not same:
        failures.append(f"meta.{key}: {ga} != {gb}")
    print(f"  {'OK ' if same else 'DIFF'} meta.{key} = {ga}")

# --- 4. WCS evaluation at sample pixels ---
print("\n[4] WCS evaluation:")
pts = [(0, 0), (1023.5, 1023.5), (2047, 2047), (100.25, 1900.75)]
wcs_ok = True
for (x, y) in pts:
    ra_a, dec_a = res_a.meta.wcs(x, y)
    ra_b, dec_b = res_b.meta.wcs(x, y)
    same = (ra_a == ra_b) and (dec_a == dec_b)
    wcs_ok &= same
    print(f"  {'OK ' if same else 'DIFF'} ({x},{y}) -> "
          f"({ra_a:.10f},{dec_a:.10f}) vs ({ra_b:.10f},{dec_b:.10f})")
if not wcs_ok:
    failures.append("WCS evaluation differs")

# --- 5. Filename metadata (cosmetic but affects saved FILENAME card) ---
print("\n[5] meta.filename:")
print(f"  A (path input):  {res_a.meta.filename}")
print(f"  B (model input): {res_b.meta.filename}")
if res_a.meta.filename != res_b.meta.filename:
    print("  NOTE: differs (cosmetic; chain must set meta.filename "
          "explicitly like persistence_step already does)")

# --- 6. Input-model mutation check ---
print("\n[6] Caller's model mutated in place by run B?")
mut_data = not np.array_equal(model_in.data, snap_data, equal_nan=True)
mut_dq = not np.array_equal(model_in.dq, snap_dq)
print(f"  data mutated: {mut_data}; dq mutated: {mut_dq}; "
      f"result is same object: {res_b is model_in}")
if mut_data or mut_dq or res_b is model_in:
    print("  NOTE: pipeline operates on/returns the caller's model — chain "
          "must not reuse the input model after the call (it doesn't).")

print("\n" + "=" * 70)
if failures:
    print(f"PARITY: FAIL — {len(failures)} difference(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PARITY: PASS — model input is equivalent to path input "
      "(refs, arrays, photometry, WCS)")
