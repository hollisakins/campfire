"""
Automatic stuck closed shutter detection for NIRSpec observations.

Supports both PRISM and grating data, with independent thresholds for each.
Grating detection is disabled by default (opt-in via detect_stuck_shutters_grating).

Analyzes rectified 2D spectral images (s2d files) produced by stage2a to
identify MSA shutters that are stuck closed. A stuck shutter shows near-zero
signal in the cross-dispersion profile, consistent across all nods/dithers.

Detection results are written to the observation's stuck_closed_shutters TOML
file, and diagnostic QA plots are generated for visual verification.
"""

import os

import numpy as np
import toml
from astropy.io import fits

from campfire_pipeline.common.io import log

# JWST NIRSpec MSA slit geometry constants (from assign_wcs/nirspec.py)
_MSA_MARGIN = 1.05   # 0.55 (half open area) + 0.50 (padding beyond outer shutters)
_MSA_PITCH = 1.15    # slit_frame units per shutter pitch


def _contiguous_slitlet_length(shutsta):
    """Count shutters in the contiguous slitlet region around 'x'.

    Expands outward from the source shutter, including '1' (open) and
    gaps of up to 2 consecutive '0's (stuck/closed shutters within the
    slitlet), but stopping at runs of 3+ consecutive '0's (gaps to
    detached background shutters).
    """
    x_pos = shutsta.index('x')

    # Expand left
    left = x_pos
    while left > 0:
        if shutsta[left - 1] != '0':
            left -= 1
        elif left > 1 and shutsta[left - 2] != '0':
            left -= 1
        elif left > 2 and shutsta[left - 3] != '0':
            left -= 1
        else:
            break

    # Expand right
    right = x_pos
    while right < len(shutsta) - 1:
        if shutsta[right + 1] != '0':
            right += 1
        elif right < len(shutsta) - 2 and shutsta[right + 2] != '0':
            right += 1
        elif right < len(shutsta) - 3 and shutsta[right + 3] != '0':
            right += 1
        else:
            break

    return right - left + 1


def _build_tasks(group_files, threshold):
    """Build task tuples for a group of files sharing a threshold.

    Parameters
    ----------
    group_files : Table
        Subset of the file table (e.g. PRISM-only or grating-only).
    threshold : float
        The low_frac_threshold to embed in each task.

    Returns
    -------
    list of tuple
        Each tuple is (root, source_id, s2d_paths, n_shutters, threshold).
    """
    tasks = []
    for root in np.unique(group_files['root']):
        root_files = group_files[group_files['root'] == root]

        for source_id in np.unique(root_files['source_id']):
            source_files = root_files[root_files['source_id'] == source_id]

            s2d_paths = []
            for f in source_files:
                s2d = f['path'].replace('_cal.fits', '_s2d.fits')
                if os.path.exists(s2d):
                    s2d_paths.append(s2d)

            if not s2d_paths:
                continue

            n_shutters = _get_n_shutters(source_files)

            if n_shutters < 2:
                continue

            tasks.append((root, int(source_id), s2d_paths, n_shutters,
                          threshold))

    return tasks


def detect_stuck_shutters(obs, files, stage_config, n_processes=1):
    """Detect stuck closed shutters from s2d files.

    Processes PRISM files by default and optionally grating files when
    ``detect_stuck_shutters_grating`` is enabled, using independent
    thresholds for each.

    Parameters
    ----------
    obs : Observation
        The observation object.
    files : Table
        Grouped file table from discover_files('cal') + group_files(),
        with columns: path, source_id, root, grating, nod_type, detector, etc.
    stage_config : dict
        Stage2 config. Relevant keys:
        - stuck_shutter_low_frac_threshold (PRISM, default 0.5)
        - detect_stuck_shutters_grating (default False)
        - stuck_shutter_low_frac_threshold_grating (default 0.7)
    n_processes : int
        Number of parallel workers for analysis (default 1 = serial).

    Returns
    -------
    dict
        Mapping {root: {source_id: [shutter_numbers]}} of newly detected
        stuck shutters. Shutter numbers are 1-indexed ordinals.
    """
    from campfire_pipeline.common.parallel import dispatch

    prism_threshold = stage_config.get('stuck_shutter_low_frac_threshold', 0.5)
    grating_enabled = stage_config.get('detect_stuck_shutters_grating', False)
    grating_threshold = stage_config.get(
        'stuck_shutter_low_frac_threshold_grating', 0.7)

    # Partition files into PRISM and grating groups
    prism_mask = np.array([g.upper() == 'PRISM' for g in files['grating']])
    grating_mask = ~prism_mask

    # Build tasks per group with per-group thresholds
    all_tasks = []

    if np.any(prism_mask):
        prism_tasks = _build_tasks(files[prism_mask], prism_threshold)
        log(f'Analyzing {len(prism_tasks)} PRISM sources for stuck shutters '
            f'(threshold={prism_threshold:.2f}, n_processes={n_processes})')
        all_tasks.extend(prism_tasks)

    if grating_enabled and np.any(grating_mask):
        grating_tasks = _build_tasks(files[grating_mask],
                                     grating_threshold)
        log(f'Analyzing {len(grating_tasks)} grating sources for stuck '
            f'shutters (threshold={grating_threshold:.2f}, '
            f'n_processes={n_processes})')
        all_tasks.extend(grating_tasks)
    elif not grating_enabled and np.any(grating_mask):
        log('Grating files present but detect_stuck_shutters_grating is '
            'disabled, skipping')

    if not all_tasks:
        log('No files eligible for stuck shutter detection')
        return {}

    # Dispatch analysis in parallel (threshold is embedded in each task tuple)
    results = dispatch(
        _analyze_source_task, all_tasks,
        n_processes=n_processes,
        use_starmap=True,
    )

    # Collect detections
    detections = {}
    for (root, source_id, _, n_shutters, _), stuck in zip(all_tasks, results):
        if stuck:
            if root not in detections:
                detections[root] = {}
            detections[root][source_id] = stuck

            if len(stuck) == n_shutters:
                log(f'WARNING: All shutters stuck for source {source_id} '
                    f'in {root} -- source will be skipped during extraction')
            else:
                log(f'Detected stuck shutter(s) {stuck} for source '
                    f'{source_id} in {root}')

    n_sources = sum(len(v) for v in detections.values())
    n_shutters_total = sum(
        len(s) for v in detections.values() for s in v.values()
    )
    if detections:
        log(f'Stuck shutter detection complete: {n_shutters_total} stuck '
            f'shutter(s) across {n_sources} source(s)')
    else:
        log('Stuck shutter detection complete: no stuck shutters found')

    return detections


def _compute_shutter_regions(n_shutters, n_rows):
    """Compute pixel row boundaries for each shutter region in an s2d image.

    Uses the JWST NIRSpec MSA slit geometry: each shutter occupies 1.15
    slit_frame units, with an additional margin beyond each outer shutter
    (0.55 half open area + 0.50 padding = 1.05 total).  The 0.50 padding
    zone is excluded from the returned regions because it may contain
    flux from adjacent slitlets.

    Parameters
    ----------
    n_shutters : int
        Number of shutters in the slitlet.
    n_rows : int
        Number of spatial pixels in the s2d image.

    Returns
    -------
    list of (int, int)
        ``(row_start, row_end)`` for each region, ordered from bottom
        of the s2d (region 0 = highest shutter_column = shutter N) to
        top (region N-1 = lowest shutter_column = shutter 1).
    """
    total = (n_shutters - 1) * _MSA_PITCH + 2 * _MSA_MARGIN

    # Exclude the 0.50 padding beyond the outer shutters
    _PADDING = 0.50
    outer_margin_pix = int(np.round(_PADDING / total * (n_rows - 1)))

    boundaries = [outer_margin_pix]
    for k in range(n_shutters - 1):
        # Bar midpoint between shutter k and k+1, measured from slit edge
        frac = (_MSA_MARGIN + (k + 0.5) * _MSA_PITCH) / total
        boundary_pixel = int(np.round(frac * (n_rows - 1)))
        boundaries.append(boundary_pixel)
    boundaries.append(n_rows - outer_margin_pix)

    # Skip the bar pixel at each inter-shutter boundary: the resampled
    # bar is sub-pixel, so the boundary pixel blends flux from both
    # neighbours.  Excluding it avoids contamination at region edges.
    # Also skip 1 additional row at the bottom (low-index side) of each
    # shutter region to avoid cross-talk from the bar below.
    regions = []
    for i in range(n_shutters):
        start = boundaries[i] + (1 if i > 0 else 0) + 1
        end = boundaries[i + 1]
        regions.append((start, end))

    return regions


def _analyze_source_task(root, source_id, s2d_paths, n_shutters,
                         low_frac_threshold):
    """Pickle-friendly wrapper for parallel dispatch."""
    return _analyze_source_shutters(s2d_paths, n_shutters, low_frac_threshold)


def _analyze_source_shutters(s2d_paths, n_shutters, low_frac_threshold):
    """Analyze s2d files for a single source to identify stuck shutters.

    For each s2d file, divides the spatial axis into shutter regions
    using the MSA slit geometry (accounting for the margin beyond outer
    shutters) and computes the fraction of pixels with signal below 2x
    the read noise (``low_frac``, adapted from msaexp
    ``mask_stuck_closed``).  A stuck closed shutter transmits no light,
    so nearly all its pixels sit at the read-noise floor.

    A shutter is flagged as stuck if ``low_frac > low_frac_threshold``
    across ALL nods/dithers.  Using the per-pixel read-noise variance
    (VAR_RNOISE extension) as an absolute physical reference makes the
    metric immune to bright-source contamination in neighbouring
    shutters.

    The spatial axis in s2d images is inverted relative to
    shutter_column ordering: bottom rows correspond to the highest
    shutter_column (shutter N) and top rows to the lowest (shutter 1).
    The returned ordinals account for this inversion.

    Parameters
    ----------
    s2d_paths : list of str
        Paths to s2d FITS files for this source (one per nod/dither/detector).
    n_shutters : int
        Number of shutters in the slitlet.
    low_frac_threshold : float
        Minimum ``low_frac`` (fraction of pixels below 2x read noise) for a
        shutter to be considered stuck.  Default 0.5.

    Returns
    -------
    list of int
        1-indexed shutter numbers flagged as stuck, or empty list.
    """
    # Collect per-shutter low_frac values across all s2d files
    shutter_low_fracs = [[] for _ in range(n_shutters)]

    for s2d_path in s2d_paths:
        data = fits.getdata(s2d_path, ext=1)

        try:
            var_rnoise = fits.getdata(s2d_path, extname='VAR_RNOISE')
        except KeyError:
            continue

        good_mask = np.isfinite(data) & np.isfinite(var_rnoise) & (var_rnoise > 0)

        n_rows = data.shape[0]
        if n_rows < n_shutters:
            continue

        regions = _compute_shutter_regions(n_shutters, n_rows)

        for k, (row_start, row_end) in enumerate(regions):
            region_data = data[row_start:row_end, :]
            region_var = var_rnoise[row_start:row_end, :]
            region_good = good_mask[row_start:row_end, :]

            d = region_data[region_good]
            rn_sigma = np.sqrt(region_var[region_good])

            if len(d) < 5:
                shutter_low_fracs[k].append(1.0)
                continue

            low_frac = np.sum(d < 2 * rn_sigma) / len(d)
            shutter_low_fracs[k].append(low_frac)

    # Flag shutters where ALL nods show a high fraction of noise-level pixels
    # Spatial axis is inverted: region 0 (bottom) = shutter N, region N-1 (top) = shutter 1
    stuck = []
    for k in range(n_shutters):
        if not shutter_low_fracs[k]:
            continue
        # Use the MINIMUM low_frac across nods — must exceed threshold in every nod
        min_low_frac = min(shutter_low_fracs[k])
        if min_low_frac > low_frac_threshold:
            shutter_ordinal = n_shutters - k
            stuck.append(shutter_ordinal)

    return sorted(stuck)


def _get_n_shutters(source_files):
    """Determine the number of shutters for a source from the file table.

    Uses the SHUTSTA shutter_state string (length = number of shutters),
    falling back to NOD_TYPE parsing if shutter_state is unavailable.

    Parameters
    ----------
    source_files : Table
        File table rows for a single (root, source_id) group.
        Expected columns: shutter_state, nod_type.

    Returns
    -------
    int
        Number of shutters in the slitlet.
    """
    # Primary: parse SHUTSTA shutter_state string.
    # Compact strings (e.g. '1x11') give len() directly.  Full-column
    # strings (e.g. '1000...0001x1') encode closed shutters as '0'; we
    # find the contiguous region around 'x', allowing single-0 gaps
    # (stuck shutters) but breaking on runs of 00+ (detached shutters).
    if 'shutter_state' in source_files.colnames:
        for shutsta in source_files['shutter_state']:
            shutsta = str(shutsta)
            if not shutsta or 'x' not in shutsta:
                continue
            if '0' not in shutsta:
                return len(shutsta)
            return _contiguous_slitlet_length(shutsta)

    # Fallback: parse N-SHUTTER-SLITLET nod types
    for nt in np.unique(source_files['nod_type']):
        nt_str = str(nt)
        if 'SHUTTER-SLITLET' in nt_str:
            try:
                return int(nt_str.split('-')[0])
            except (ValueError, IndexError):
                pass

    return 3


def merge_stuck_shutters(existing_toml, detected):
    """Merge detected stuck shutters with existing manual entries.

    Manual entries always take priority. Detected shutters are only added
    for (root, source_id) pairs not already present in the TOML.

    Parameters
    ----------
    existing_toml : dict
        Current TOML data: {root: {source_id_str: [shutters]}}.
    detected : dict
        Newly detected: {root: {source_id: [shutters]}}.

    Returns
    -------
    merged : dict
        Merged TOML data.
    updated : set
        Set of (root, source_id) pairs that were added.
    """
    import copy
    merged = copy.deepcopy(existing_toml)
    updated = set()

    for root, sources in detected.items():
        if root not in merged:
            merged[root] = {}
        for source_id, shutters in sources.items():
            sid_str = str(source_id)
            if sid_str not in merged[root]:
                merged[root][sid_str] = shutters
                updated.add((root, source_id))
            else:
                log(f'Skipping auto-detected stuck shutters for '
                    f'{root}/{source_id}: manual entry already exists')

    return merged, updated


def write_stuck_shutters_toml(data, filepath, obs_name, auto_detected=None):
    """Write the stuck shutters TOML file.

    Parameters
    ----------
    data : dict
        Full TOML data: {root: {source_id_str: [shutters]}}.
    filepath : str
        Path to write.
    obs_name : str
        Observation name for the header comment.
    auto_detected : set, optional
        Set of (root, source_id) pairs that were auto-detected.
    """
    if auto_detected is None:
        auto_detected = set()

    lines = []
    lines.append(f'# vetted stuck closed shutter list for {obs_name}')
    lines.append('# format is a table for each "root" file name, which')
    lines.append('# consists of obs/visit/config, e.g. "jw06368001001_03101"')
    lines.append('# the list of stuck closed shutters for a source ID should')
    lines.append('# be given as a key-value pair in the table; for example:')
    lines.append('# [jw06368001001_03101]')
    lines.append('#     12345 = [1,2,3]')
    lines.append('')

    for root in sorted(data.keys()):
        lines.append(f'[{root}]')
        for sid_str in sorted(data[root].keys(), key=int):
            shutters = data[root][sid_str]
            shutter_list = '[' + ', '.join(str(s) for s in shutters) + ']'
            sid_int = int(sid_str)
            if (root, sid_int) in auto_detected:
                lines.append(f'    {sid_str} = {shutter_list}  # auto-detected')
            else:
                lines.append(f'    {sid_str} = {shutter_list}')
        lines.append('')

    with open(filepath, 'w') as f:
        f.write('\n'.join(lines))

    log(f'Wrote stuck shutter TOML to {filepath}')
