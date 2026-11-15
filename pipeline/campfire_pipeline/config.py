"""
Configuration loading and environment setup.

Resolves paths in order:
1. Explicit paths in config
2. $CAMPFIRE_ROOT/{raw,products,cache} (defaults to ~/campfire if unset)
"""

import os
from pathlib import Path

import toml

from campfire_layout import cache_path, campfire_root as _layout_campfire_root, roots


# ---------------------------------------------------------------------------
# CAMPFIRE_ROOT resolution
# ---------------------------------------------------------------------------

def _get_campfire_root():
    """Return $CAMPFIRE_ROOT, defaulting to ~/campfire if unset.

    Delegates to the shared layout contract (``campfire_layout``) so the pipeline,
    the deploy/download client, and the contract all resolve one root identically.
    """
    return str(_layout_campfire_root())


# ---------------------------------------------------------------------------
# Deep merge utility
# ---------------------------------------------------------------------------

def deep_merge(base, override):
    """Recursively merge *override* into *base*.

    Dict values are merged recursively; all other types are replaced.
    Neither input is mutated — returns a new dict.
    """
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


# ---------------------------------------------------------------------------
# Package defaults
# ---------------------------------------------------------------------------

def _load_package_defaults():
    """Load the default config shipped as package data."""
    default_path = Path(__file__).parent / 'data' / 'config_default.toml'
    with open(default_path, 'r') as f:
        return toml.load(f)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path=None):
    """Load and return the merged pipeline configuration.

    1. Load package defaults (always).
    2. If *config_path* is given explicitly, it must exist (error if not).
       If *config_path* is None, search:
         a. $CAMPFIRE_ROOT/config/config.toml
         b. ./config.toml
    3. If a user config is found, deep-merge it over defaults.
    4. Return the merged dict.

    The config file is optional — defaults alone are sufficient to run.
    """
    defaults = _load_package_defaults()

    # Determine user config path
    user_path = None
    if config_path is not None:
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        user_path = config_path
    else:
        campfire_root = _get_campfire_root()
        candidate = os.path.join(campfire_root, 'config', 'config.toml')
        if os.path.isfile(candidate):
            user_path = candidate
        if user_path is None and os.path.isfile('config.toml'):
            user_path = 'config.toml'

    if user_path is not None:
        with open(user_path, 'r') as f:
            user_config = toml.load(f)
        return deep_merge(defaults, user_config)

    return defaults


# ---------------------------------------------------------------------------
# Environment and path resolution
# ---------------------------------------------------------------------------

_BLAS_THREAD_VARS = (
    'OPENBLAS_NUM_THREADS',
    'MKL_NUM_THREADS',
    'OMP_NUM_THREADS',
    'NUMEXPR_NUM_THREADS',
    'VECLIB_MAXIMUM_THREADS',
    'BLIS_NUM_THREADS',
)


# Which observatory an instrument block of the config belongs to. Each has its
# own CRDS server and its own context series; they cannot be mixed.
_OBSERVATORY = {'nircam': 'jwst', 'nirspec': 'jwst',
                'acswfc': 'hst', 'wfc3ir': 'hst'}
# The [environment] keys holding each observatory's default context and server.
_CRDS_KEYS = {'jwst': ('CRDS_CONTEXT', 'CRDS_SERVER_URL'),
              'hst': ('HST_CRDS_CONTEXT', 'HST_CRDS_SERVER_URL')}


def check_crds_context(context, observatory, where):
    """Refuse a CRDS context of the wrong observatory, loudly.

    A JWST context is ``jwst_<n>.pmap`` (or a ``jwst-...`` tag), an HST one
    ``hst_<n>.pmap``. Handing one to the other observatory's calibration would
    at best fail deep inside CRDS and at worst select nothing sensible, so it
    stops here with the place the value came from (``where``). Returns the
    context.
    """
    text = str(context or '').strip()
    other = 'hst' if observatory == 'jwst' else 'jwst'
    if not text:
        raise ValueError(f'{where}: no CRDS context set')
    low = text.lower()
    if low.startswith((f'{other}_', f'{other}-')):
        raise ValueError(
            f'{where} = {text!r} is a {other.upper()} CRDS context, but this '
            f'setting is for {observatory.upper()} data. JWST contexts go in '
            f'[environment].CRDS_CONTEXT (or [nircam] / [nirspec].crds_context), '
            f'HST contexts in [environment].HST_CRDS_CONTEXT (or [acswfc] / '
            f'[wfc3ir].crds_context).')
    if not low.startswith((f'{observatory}_', f'{observatory}-')):
        raise ValueError(
            f'{where} = {text!r} is not a {observatory.upper()} CRDS context '
            f'(expected something like "{observatory}_1234.pmap")')
    return text


def check_crds_server(url, observatory, where):
    """Refuse the other observatory's CRDS server. Returns the url."""
    other = 'hst' if observatory == 'jwst' else 'jwst'
    if f'{other}-crds' in str(url or '').lower():
        raise ValueError(
            f'{where} = {url!r} is the {other.upper()} CRDS server, but this '
            f'setting is for {observatory.upper()} data')
    return url


def crds_context_for(config, instrument):
    """The CRDS context one instrument's reductions use.

    ``[<instrument>].crds_context`` when set, otherwise the default of the
    instrument's observatory in ``[environment]``: ``CRDS_CONTEXT`` for
    ``nircam`` / ``nirspec`` (the line the release rule names),
    ``HST_CRDS_CONTEXT`` for ``acswfc`` / ``wfc3ir``. An HST instrument never
    reads the JWST default, nor the reverse, and a context of the wrong
    observatory raises ``ValueError`` (:func:`check_crds_context`). Returns
    ``None`` for a JWST instrument when nothing is set anywhere (CRDS then
    uses its own default); an HST instrument with nothing set is an error,
    since its calibration takes the context explicitly.
    """
    observatory = _OBSERVATORY[instrument]
    block = config.get(instrument, {})
    own = block.get('crds_context') if isinstance(block, dict) else None
    if own:
        return check_crds_context(own, observatory, f'[{instrument}].crds_context')
    key = _CRDS_KEYS[observatory][0]
    default = config.get('environment', {}).get(key)
    if default or observatory == 'hst':
        return check_crds_context(default, observatory, f'[environment].{key}')
    return None


def crds_server_for(config, instrument):
    """The CRDS server of an instrument's observatory, from ``[environment]``
    (``CRDS_SERVER_URL`` or ``HST_CRDS_SERVER_URL``); the other observatory's
    server there raises ``ValueError``. ``None`` when unset."""
    observatory = _OBSERVATORY[instrument]
    key = _CRDS_KEYS[observatory][1]
    url = config.get('environment', {}).get(key)
    return check_crds_server(url, observatory, f'[environment].{key}') if url else None


def setup_environment(config, instrument=None):
    """Set environment variables from config file.

    ``instrument`` is the instrument the command reduces. A JWST instrument
    (``'nircam'``, ``'nirspec'``) may pin a CRDS context of its own,
    ``[<instrument>].crds_context``, which then replaces
    ``[environment].CRDS_CONTEXT`` for this process; a command runs one
    instrument, so the pins never meet. For an HST instrument (``'acswfc'``,
    ``'wfc3ir'``) its context is resolved here only to fail early when it is
    missing or wrong: HST settings (the ``HST_*`` keys of ``[environment]``
    and the pins) are never exported, the HST calibrate step hands them to its
    subprocesses. Every CRDS setting is checked on the way: a context or
    server of the other observatory stops the command
    (:func:`check_crds_context`).

    For CRDS_PATH, priority is:
    1. Existing $CRDS_PATH in the user's environment
    2. [environment].CRDS_PATH in config
    3. $CAMPFIRE_ROOT/cache/crds (CAMPFIRE_ROOT defaults to ~/campfire)

    Also pins BLAS/OpenMP thread counts to 1 (unless the user already set
    them) before any worker pool forks. Pipeline stages parallelize via
    processes; letting numpy/scipy/astropy spawn one BLAS thread per core
    inside each worker leads to N_processes * N_cores threads, which on
    high-core HPC nodes (e.g. candide, 64 cores) exhausts RLIMIT_NPROC and
    surfaces as cascading "OpenBLAS blas_thread_init: pthread_create
    failed" errors plus spurious KeyboardInterrupt tracebacks from workers
    that lost a thread-spawn race inside an astropy.modeling call. Setting
    the env vars here propagates to fork-pool children automatically.
    """
    if instrument is not None and instrument not in _OBSERVATORY:
        raise ValueError(
            f'setup_environment: unknown instrument {instrument!r}; expected '
            f'one of {", ".join(_OBSERVATORY)}')

    # BLAS thread caps must be set in the parent before pool forks; OpenBLAS
    # re-reads OPENBLAS_NUM_THREADS in each child on first numpy use.
    for var in _BLAS_THREAD_VARS:
        os.environ.setdefault(var, '1')

    if 'environment' in config:
        env = config['environment']

        # Only set CRDS_PATH fallback if not in config and not already in env
        if 'CRDS_PATH' not in env and 'CRDS_PATH' not in os.environ:
            env['CRDS_PATH'] = str(cache_path('crds'))

        for observatory, (context_key, server_key) in _CRDS_KEYS.items():
            if env.get(context_key):
                check_crds_context(env[context_key], observatory,
                                   f'[environment].{context_key}')
            if env.get(server_key):
                check_crds_server(env[server_key], observatory,
                                  f'[environment].{server_key}')

        for key, value in env.items():
            if key == 'CRDS_PATH' and 'CRDS_PATH' in os.environ:
                continue
            if key.startswith('HST_'):
                continue                  # HST settings stay out of the process
            os.environ[key] = str(value)

    if instrument is not None:
        context = crds_context_for(config, instrument)
        if context and _OBSERVATORY[instrument] == 'jwst':
            os.environ['CRDS_CONTEXT'] = context


def resolve_paths(config=None):
    """Resolve and create the pipeline directory roots.

    Returns dict with keys: ``data_dir``, ``products_dir``, ``reference_dir`` —
    all derived from a single ``$CAMPFIRE_ROOT`` (default ``~/campfire``):
    ``{root}/raw``, ``{root}/products``, ``{root}/reference``.

    There is intentionally no per-path override: relocate the whole tree by
    setting ``$CAMPFIRE_ROOT``, or symlink an individual subdir if it must live
    on a different filesystem. (``config`` is accepted and ignored for
    backward compatibility with existing call sites.)
    """
    r = roots()
    result = {
        'data_dir': str(r.raw),
        'products_dir': str(r.products),
        'reference_dir': str(r.reference),
    }
    for d in result.values():
        os.makedirs(d, exist_ok=True)
    return result


# ---------------------------------------------------------------------------
# Observation / field file resolution
# ---------------------------------------------------------------------------

def resolve_observations_file(explicit_path=None):
    """Find the observations.toml file.

    Search order:
    1. explicit_path (if provided and exists)
    2. $CAMPFIRE_ROOT/config/observations.toml
    3. ./observations.toml (backwards compat)

    Returns
    -------
    str
        Path to the observations file.

    Raises
    ------
    FileNotFoundError
        If no observations file is found.
    """
    tried = []

    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path
    if explicit_path:
        tried.append(explicit_path)

    campfire_root = _get_campfire_root()
    candidate = os.path.join(campfire_root, 'config', 'observations.toml')
    if os.path.isfile(candidate):
        return candidate
    tried.append(candidate)

    if os.path.isfile('observations.toml'):
        return 'observations.toml'
    tried.append('observations.toml')

    raise FileNotFoundError(
        f"observations.toml not found. Searched: {tried}"
    )


def resolve_fields_file(explicit_path=None):
    """Find the fields.toml file for NIRCam field definitions.

    Search order:
    1. explicit_path (if provided and exists)
    2. $CAMPFIRE_ROOT/config/fields.toml
    3. ./fields.toml (backwards compat)

    Returns
    -------
    str
        Path to the fields file.

    Raises
    ------
    FileNotFoundError
        If no fields file is found.
    """
    tried = []

    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path
    if explicit_path:
        tried.append(explicit_path)

    campfire_root = _get_campfire_root()
    candidate = os.path.join(campfire_root, 'config', 'fields.toml')
    if os.path.isfile(candidate):
        return candidate
    tried.append(candidate)

    if os.path.isfile('fields.toml'):
        return 'fields.toml'
    tried.append('fields.toml')

    raise FileNotFoundError(
        f"fields.toml not found. Searched: {tried}"
    )


def resolve_programs_file(explicit_path=None):
    """Find the programs.toml file mapping program slugs to metadata.

    Search order:
    1. explicit_path (if provided and exists)
    2. $CAMPFIRE_ROOT/config/programs.toml
    3. ./programs.toml (backwards compat)

    Returns
    -------
    str

    Raises
    ------
    FileNotFoundError
        If no programs file is found. Callers that treat the cross-check as
        best-effort should catch this.
    """
    tried = []

    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path
    if explicit_path:
        tried.append(explicit_path)

    campfire_root = _get_campfire_root()
    candidate = os.path.join(campfire_root, 'config', 'programs.toml')
    if os.path.isfile(candidate):
        return candidate
    tried.append(candidate)

    if os.path.isfile('programs.toml'):
        return 'programs.toml'
    tried.append('programs.toml')

    raise FileNotFoundError(
        f"programs.toml not found. Searched: {tried}"
    )


# ---------------------------------------------------------------------------
# Stage config getters
# ---------------------------------------------------------------------------

def get_stage_config(stage_name, config, obs):
    """Build effective config for a NIRSpec pipeline stage.

    Merges two layers (highest priority wins):
        1. Observation-specific overrides  (observations.toml  [obs.stageN])
        2. Config defaults + user overrides (already merged in load_config)

    When ``[nirspec.stage2].extend_g140m_g235m`` is enabled for this
    observation, stage1's background-subtraction wavelength caps for the
    extended gratings are auto-widened (see
    ``_widen_stage1_ranges_for_extension``) so stage1 does not subtract the
    extended-order flux as background before stage2 can recover it.
    """
    merged = dict(config.get('nirspec', {}).get(stage_name, {}))
    merged.update(obs.stage_overrides.get(stage_name, {}))

    if stage_name == 'stage1':
        stage2 = dict(config.get('nirspec', {}).get('stage2', {}))
        stage2.update(obs.stage_overrides.get('stage2', {}))
        if stage2.get('extend_g140m_g235m'):
            merged = _widen_stage1_ranges_for_extension(merged)

    return merged


def _widen_stage1_ranges_for_extension(stage1_config):
    """Widen stage1's background-subtraction wavelength caps for extended gratings.

    Extended-wavelength reductions (``[nirspec.stage2].extend_g140m_g235m``) push
    the extracted range of G140M/F100LP and G235M/F170LP out to
    ``EXTENDED_MAX_WAVELENGTH_UM``. Stage1's ``override_wavelength_range`` defines
    the science footprint protected from the background estimate; if its red caps
    stay at the nominal values, the extended-order flux is treated as background and
    subtracted in place before stage2 runs.

    Returns a copy of *stage1_config* with the red edge of the extended gratings
    raised to the extension limit (the blue edge is preserved). The nested
    ``override_wavelength_range`` dict is deep-copied before mutation so the loaded
    config is never modified.
    """
    import copy
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec.constants import (
        EXTENDED_MAX_WAVELENGTH_UM, EXTENDED_GRATING_FILTERS,
    )

    gratings_to_widen = {grating for grating, _filt in EXTENDED_GRATING_FILTERS}

    merged = dict(stage1_config)
    ranges = copy.deepcopy(merged.get('override_wavelength_range', {}))
    for grating in gratings_to_widen:
        if grating in ranges:
            lo, hi = ranges[grating]
            if hi < EXTENDED_MAX_WAVELENGTH_UM:
                ranges[grating] = [lo, EXTENDED_MAX_WAVELENGTH_UM]
                log(f"extend_g140m_g235m: widened stage1 {grating} background mask "
                    f"red edge {hi} -> {EXTENDED_MAX_WAVELENGTH_UM} um")
        else:
            log(f"extend_g140m_g235m: no stage1 override_wavelength_range entry for "
                f"{grating}; extended-order flux may be subtracted as background")
    merged['override_wavelength_range'] = ranges
    return merged


def get_nircam_step_config(step_name, config, field):
    """Build effective config for a single NIRCam pipeline step.

    Reads from the flat ``[nircam.<step>]`` layout in
    ``config_default.toml`` and the matching flat ``[<field>.<step>]``
    layout in ``fields.toml``.

    Merges (highest priority wins):
        1. Field-specific step overrides  (fields.toml [<field>.<step>])
        2. Config defaults + user overrides (already merged in load_config)
    """
    base = config.get('nircam', {}).get(step_name, {})
    return deep_merge(base, field.step_overrides.get(step_name, {}))


_HST_DEFAULTS = None


def get_hst_step_config(step_name, detector, config=None, overrides=None):
    """Effective settings of one step of an HST instrument.

    Each HST detector is an instrument with a block of its own,
    ``[<instrument>.<step>]`` with ``<instrument>`` the product-name token
    (``acswfc``, ``wfc3ir``), on the same footing as ``[nircam.<step>]``: the
    detectors differ in almost every threshold, and a block that reads for one
    cannot be misread for the other. ``detector`` is that token. Merged,
    highest priority last:

        1. ``config_default.toml`` and the user config (already merged by
           :func:`load_config`). With ``config=None`` the shipped defaults
           alone, so a step called outside the command line runs with exactly
           what ``cfpipe config`` prints.
        2. ``overrides``: the field's ``[<field>.<instrument>.<step>]`` table.

    The shipped file is the only place a default lives; the step modules carry
    none. A name that is not an HST instrument raises ``KeyError``.
    """
    global _HST_DEFAULTS
    hst_instruments = sorted(k for k, v in _OBSERVATORY.items() if v == 'hst')
    if detector not in hst_instruments:
        raise KeyError(
            f'{detector!r} is not an HST instrument; expected one of '
            f'{", ".join(hst_instruments)}')
    if config is None:
        if _HST_DEFAULTS is None:
            shipped = _load_package_defaults()
            _HST_DEFAULTS = {k: shipped.get(k, {}) for k in hst_instruments}
        config = _HST_DEFAULTS
    block = config.get(detector, {})
    step = block.get(step_name, {}) if isinstance(block, dict) else {}
    return deep_merge(step if isinstance(step, dict) else {}, overrides or {})


# ---------------------------------------------------------------------------
# Template grid paths
# ---------------------------------------------------------------------------

def resolve_template_grid_paths(config):
    """Resolve template grid pickle file paths.

    If a path in template_grids.*.file is relative, resolve it relative
    to $CAMPFIRE_ROOT/cache/templates/. Absolute paths are used as-is.
    """
    template_grids = config.get('nirspec', {}).get('template_grids', {})

    for name, grid_config in template_grids.items():
        filepath = grid_config.get('file', '')
        if filepath and not os.path.isabs(filepath):
            grid_config['file'] = str(cache_path('templates', filepath))

    return template_grids


# ---------------------------------------------------------------------------
# Package data helpers
# ---------------------------------------------------------------------------

def get_r_curve_path(grating):
    """Get the path to an r-curve FITS file shipped as package data.

    Parameters
    ----------
    grating : str
        Grating name (e.g. 'prism', 'g395m'). Case-insensitive.

    Returns
    -------
    str
        Absolute path to the r-curve FITS file.
    """
    data_dir = Path(__file__).parent / 'data'
    filename = f'jwst_nirspec_{grating.lower()}_disp.fits'
    path = data_dir / filename
    if not path.exists():
        available = sorted(p.name for p in data_dir.glob('jwst_nirspec_*_disp.fits'))
        raise FileNotFoundError(
            f"R-curve file not found: {path}\nAvailable: {available}"
        )
    return str(path)


def get_extended_photom_path(fixed_slit=False):
    """Get the path to the committed extended-wavelength photom reference file.

    This is the SPURS-derived calibrated photom for the extended-wavelength
    feature (``[nirspec.stage2].extend_g140m_g235m``), shipped as package data.
    Fixed-slit sources require a different flux calibration than MSA sources, so
    they use a dedicated photom reference (v0014) when *fixed_slit* is True.

    Parameters
    ----------
    fixed_slit : bool, optional
        If True, return the fixed-slit photom reference
        (``extended_jwst_nirspec_photom_0014.fits``) instead of the MSA one
        (``extended_jwst_nirspec_photom_0015.fits``).

    Returns
    -------
    str
        Absolute path to the appropriate extended photom reference file.
    """
    data_dir = Path(__file__).parent / 'data'
    fname = ('extended_jwst_nirspec_photom_0014.fits' if fixed_slit
             else 'extended_jwst_nirspec_photom_0015.fits')
    path = data_dir / fname
    if not path.exists():
        raise FileNotFoundError(
            f"Extended photom reference not found: {path}\n"
            "It ships with the pipeline (PR #163); ensure the package data is installed."
        )
    return str(path)
