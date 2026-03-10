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
import warnings

import numpy as np
import toml
import matplotlib.pyplot as plt
from astropy.io import fits
from astropy.visualization import ImageNormalize, ZScaleInterval

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


def plot_stuck_shutter_diagnostics(files, source_id, root, workspace_dir,
                                   n_shutters, stuck_shutters, stage_config):
    """Generate a diagnostic QA plot for stuck shutter detection.

    Layout mirrors the nods.pdf plots with shutter boundaries and
    stuck shutter highlights overlaid.

    Parameters
    ----------
    files : Table
        File table for this source (from discover_files + group_files),
        filtered to a single root.
    source_id : int
    root : str
    workspace_dir : str
        Output directory (plots go into {workspace_dir}/stuck_shutters/).
    n_shutters : int
    stuck_shutters : list of int
        1-indexed shutter numbers flagged as stuck.
    stage_config : dict
        For retrieving threshold values to display.
    """
    from campfire_pipeline.nirspec.observation import Observation

    # Collect s2d files organized by nod
    root_files = files[(files['root'] == root) & (files['source_id'] == source_id)]
    detectors = sorted(np.unique(root_files['detector']))
    has_both = 'nrs1' in detectors and 'nrs2' in detectors
    exp_groups = sorted(np.unique(root_files['exp_group']))
    multi_eg = len(exp_groups) > 1

    plot_rows = []
    for eg_idx, eg in enumerate(exp_groups):
        eg_files = root_files[root_files['exp_group'] == eg]
        for nod in sorted(np.unique(eg_files['nod'])):
            nod_files = eg_files[eg_files['nod'] == nod]
            nrs1_s2d = nrs2_s2d = None
            for f in nod_files:
                s2d = f['path'].replace('_cal.fits', '_s2d.fits')
                if os.path.exists(s2d):
                    if f['detector'] == 'nrs1':
                        nrs1_s2d = s2d
                    else:
                        nrs2_s2d = s2d
            label = f"d{eg_idx+1}:{nod}" if multi_eg else nod
            plot_rows.append((label, nrs1_s2d, nrs2_s2d))

    n_nods = len(plot_rows)
    if n_nods == 0:
        return

    # Shared ZScale normalization
    data_all = []
    for _, n1, n2 in plot_rows:
        for s2d_file in [n1, n2]:
            if s2d_file:
                d = fits.getdata(s2d_file, ext=1)
                try:
                    dq = fits.getdata(s2d_file, extname='DQ')
                    good = np.isfinite(d) & (dq == 0)
                except KeyError:
                    good = np.isfinite(d)
                data_all.append(d[good])
    if not data_all:
        return
    data_concat = np.concatenate(data_all)
    med = np.median(data_concat)
    mad = np.median(np.abs(data_concat - med))
    sigma = mad * 1.4826
    data_concat = data_concat[np.abs(data_concat - med) < 10 * sigma]
    norm = ImageNormalize(data_concat, interval=ZScaleInterval())

    # Determine grating to pick the appropriate threshold for display
    grating = root_files['grating'][0].upper() if len(root_files) > 0 else 'PRISM'
    if grating == 'PRISM':
        low_frac_thresh = stage_config.get('stuck_shutter_low_frac_threshold', 0.5)
    else:
        low_frac_thresh = stage_config.get(
            'stuck_shutter_low_frac_threshold_grating', 0.7)
    stuck_str = ', '.join(str(s) for s in stuck_shutters)
    title = (f'{root} | {source_id} | {grating} | Stuck shutters: [{stuck_str}]\n'
             f'low_frac > {low_frac_thresh}')

    # Create output directory
    out_dir = os.path.join(workspace_dir, 'stuck_shutters')
    os.makedirs(out_dir, exist_ok=True)

    def _add_shutter_overlays(ax_img, ax_prof, n_rows, n_shutters,
                              stuck_shutters, data, var_rnoise=None):
        """Add shutter boundary lines and stuck-shutter highlights.

        Spatial axis is inverted: region 0 (bottom) = shutter N,
        region N-1 (top) = shutter 1.
        """
        regions = _compute_shutter_regions(n_shutters, n_rows)

        # Shutter boundaries (between adjacent regions)
        for k in range(1, n_shutters):
            boundary = regions[k][0] - 0.5
            ax_img.axhline(boundary, color='gray', linestyle='--',
                           linewidth=0.5, alpha=0.7)
            ax_prof.axhline(boundary, color='gray', linestyle='--',
                            linewidth=0.5, alpha=0.7)

        # Highlight stuck shutters (convert ordinal to spatial region)
        for s in stuck_shutters:
            k = n_shutters - s
            row_start, row_end = regions[k]
            y_lo = row_start - 0.5
            y_hi = row_end - 0.5
            ax_img.axhspan(y_lo, y_hi, color='red', alpha=0.15)
            ax_prof.axhspan(y_lo, y_hi, color='red', alpha=0.15)
            y_mid = (y_lo + y_hi) / 2
            ax_prof.text(0.95, y_mid, f'STUCK (s{s})',
                         transform=ax_prof.get_yaxis_transform(),
                         ha='right', va='center', fontsize=5, color='red',
                         fontweight='bold')

        # Per-shutter low_frac metrics (fraction of pixels below 2x read noise)
        if var_rnoise is not None:
            good_mask = (np.isfinite(data) & np.isfinite(var_rnoise)
                         & (var_rnoise > 0))
            for k, (row_start, row_end) in enumerate(regions):
                region_data = data[row_start:row_end, :]
                region_var = var_rnoise[row_start:row_end, :]
                region_good = good_mask[row_start:row_end, :]
                d = region_data[region_good]
                rn_sigma = np.sqrt(region_var[region_good])
                if len(d) > 0:
                    low_frac = np.sum(d < 2 * rn_sigma) / len(d)
                else:
                    low_frac = 1.0
                shutter_ordinal = n_shutters - k

                y_mid = (row_start + row_end) / 2 - 0.5
                ax_img.text(0.02, y_mid,
                            f's{shutter_ordinal} lf={low_frac:.2f}',
                            transform=ax_img.get_yaxis_transform(),
                            fontsize=4, color='white', va='center',
                            fontweight='bold',
                            bbox=dict(boxstyle='round,pad=0.2',
                                      facecolor='black', alpha=0.5))

    if has_both:
        # Determine width ratios
        nrs1_shape = nrs2_shape = None
        for _, n1, n2 in plot_rows:
            if n1 and nrs1_shape is None:
                nrs1_shape = np.shape(fits.getdata(n1, ext=1))
            if n2 and nrs2_shape is None:
                nrs2_shape = np.shape(fits.getdata(n2, ext=1))
            if nrs1_shape and nrs2_shape:
                break

        nrs1_ratio = nrs1_shape[1] / (nrs1_shape[1] + nrs2_shape[1]) * 6
        nrs2_ratio = 6 - nrs1_ratio

        fig, ax = plt.subplots(n_nods, 4,
            figsize=(7*1.5, n_nods*1.5),
            width_ratios=[nrs1_ratio, 0.5, nrs2_ratio, 0.5],
            constrained_layout=True)
        if n_nods == 1:
            ax = ax.reshape(1, -1)

        fig.suptitle(title, fontname='monospace', fontsize=7)

        for i, (label, nrs1_s2d, nrs2_s2d) in enumerate(plot_rows):
            if nrs1_s2d:
                nrs1 = fits.getdata(nrs1_s2d, ext=1)
                try:
                    nrs1_vrn = fits.getdata(nrs1_s2d, extname='VAR_RNOISE')
                except KeyError:
                    nrs1_vrn = None
                ax[i,0].imshow(nrs1, norm=norm, origin='lower', aspect='auto',
                               interpolation='nearest')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    prof = np.nanmedian(nrs1, axis=1)
                ax[i,1].step(prof, np.arange(nrs1.shape[0])-0.5, where='pre',
                             linewidth=1, color='k')
                _add_shutter_overlays(ax[i,0], ax[i,1], nrs1.shape[0],
                                      n_shutters, stuck_shutters, nrs1,
                                      var_rnoise=nrs1_vrn)

            if nrs2_s2d:
                nrs2 = fits.getdata(nrs2_s2d, ext=1)
                try:
                    nrs2_vrn = fits.getdata(nrs2_s2d, extname='VAR_RNOISE')
                except KeyError:
                    nrs2_vrn = None
                ax[i,2].imshow(nrs2, norm=norm, origin='lower', aspect='auto',
                               interpolation='nearest')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    prof = np.nanmedian(nrs2, axis=1)
                ax[i,3].step(prof, np.arange(nrs2.shape[0])-0.5, where='pre',
                             linewidth=1, color='k')
                _add_shutter_overlays(ax[i,2], ax[i,3], nrs2.shape[0],
                                      n_shutters, stuck_shutters, nrs2,
                                      var_rnoise=nrs2_vrn)

            ax[i,1].tick_params(labelleft=False)
            ax[i,2].tick_params(labelleft=False)
            ax[i,3].tick_params(labelleft=False)
            ax[i,1].set_ylim(*ax[i,0].get_ylim())
            ax[i,2].set_ylim(*ax[i,0].get_ylim())
            ax[i,3].set_ylim(*ax[i,0].get_ylim())
            if i == 0:
                ax[i,0].set_title('nrs1', fontname='monospace')
                ax[i,2].set_title('nrs2', fontname='monospace')
            ax[i,3].set_ylabel(label, fontname='monospace')
            ax[i,3].yaxis.set_label_position("right")

        for i in range(n_nods-1):
            for j in range(4):
                ax[i,j].tick_params(labelbottom=False)

        for col in [1, 3]:
            xmins = [ax[i,col].get_xlim()[0] for i in range(n_nods)]
            xmaxs = [ax[i,col].get_xlim()[1] for i in range(n_nods)]
            for i in range(n_nods):
                ax[i,col].set_xlim(min(xmins), max(xmaxs))

    else:
        det = detectors[0]

        fig, ax = plt.subplots(n_nods, 2,
            figsize=(7*1.5, n_nods*1.5),
            width_ratios=[6, 1],
            constrained_layout=True)
        if n_nods == 1:
            ax = ax.reshape(1, -1)

        fig.suptitle(title, fontname='monospace', fontsize=7)

        for i, (label, nrs1_s2d, nrs2_s2d) in enumerate(plot_rows):
            s2d_file = nrs1_s2d if det == 'nrs1' else nrs2_s2d
            if s2d_file:
                data = fits.getdata(s2d_file, ext=1)
                try:
                    vrn = fits.getdata(s2d_file, extname='VAR_RNOISE')
                except KeyError:
                    vrn = None
                ax[i,0].imshow(data, norm=norm, origin='lower', aspect='auto',
                               interpolation='nearest')
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    prof = np.nanmedian(data, axis=1)
                ax[i,1].step(prof, np.arange(data.shape[0])-0.5, where='pre',
                             linewidth=1, color='k')
                _add_shutter_overlays(ax[i,0], ax[i,1], data.shape[0],
                                      n_shutters, stuck_shutters, data,
                                      var_rnoise=vrn)

            ax[i,1].tick_params(labelleft=False)
            ax[i,1].set_ylim(*ax[i,0].get_ylim())
            ax[i,1].set_ylabel(label, fontname='monospace')
            ax[i,1].yaxis.set_label_position("right")

        for i in range(n_nods-1):
            ax[i,0].tick_params(labelbottom=False)
            ax[i,1].tick_params(labelbottom=False)

        xmins = [ax[i,1].get_xlim()[0] for i in range(n_nods)]
        xmaxs = [ax[i,1].get_xlim()[1] for i in range(n_nods)]
        for i in range(n_nods):
            ax[i,1].set_xlim(min(xmins), max(xmaxs))

    out_path = os.path.join(out_dir, f'{root}_{source_id}_stuck_diagnostic.pdf')
    plt.savefig(out_path, dpi=300)
    plt.close()
    log(f'Saved stuck shutter diagnostic plot: {out_path}')
