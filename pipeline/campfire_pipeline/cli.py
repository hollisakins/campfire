"""
cfpipe — Unified CLI for the CAMPFIRE data reduction pipeline.

Usage:
    cfpipe nirspec stage1  --obs ember_uds_p4 -p 4
    cfpipe nircam  stage1  --field cosmos --filters f444w -p 4
    cfpipe info
    cfpipe config > my_config.toml
    cfpipe download --program 6585 --instrument nirspec
"""

from campfire_pipeline import _thread_caps  # noqa: F401  (must precede numpy/matplotlib)

import os
import sys
from pathlib import Path

import click

from campfire_pipeline.config import load_config, setup_environment, resolve_paths
from campfire_pipeline.common.version import get_reduction_version


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------

def _print_version(ctx, param, value):
    """Eager ``--version`` callback so the git interrogation is lazy.

    ``click.version_option(version=get_reduction_version())`` evaluated the
    version at module import — i.e. on *every* cfpipe invocation — and
    ``get_reduction_version()`` runs four git subprocesses including a
    ``git status`` walk of the pipeline subtree, which on a cluster NFS
    checkout is pure startup latency for commands that never print it.
    Reduction runs still resolve the version when they stamp CMPFRVER.
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(f'cfpipe, version {get_reduction_version()}')
    ctx.exit()


@click.group()
@click.option('--version', is_flag=True, expose_value=False, is_eager=True,
              callback=_print_version, help='Show the version and exit.')
def main():
    """CAMPFIRE data reduction pipeline."""
    pass


# ---------------------------------------------------------------------------
# Instrument subgroups
# ---------------------------------------------------------------------------

from campfire_pipeline.nirspec.cli import main as nirspec_cli
from campfire_pipeline.nircam.cli import main as nircam_cli

main.add_command(nirspec_cli, 'nirspec')
main.add_command(nircam_cli, 'nircam')


# ---------------------------------------------------------------------------
# config command
# ---------------------------------------------------------------------------

@main.command()
def config():
    """Print the default configuration to stdout."""
    default_path = Path(__file__).parent / 'data' / 'config_default.toml'
    click.echo(default_path.read_text())


# ---------------------------------------------------------------------------
# info command
# ---------------------------------------------------------------------------

@main.command()
@click.option('--config', 'config_path', default=None,
              help='Path to configuration file.')
def info(config_path):
    """Show pipeline environment and resolved paths."""
    import campfire_pipeline

    pkg_dir = Path(campfire_pipeline.__file__).parent

    click.echo("CAMPFIRE Pipeline")
    click.echo(f"  Python:         {sys.version.split()[0]}")

    try:
        import jwst
        click.echo(f"  jwst:           {jwst.__version__}")
    except ImportError:
        click.echo("  jwst:           not installed")

    click.echo(f"  Package:        {pkg_dir}")

    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    click.echo()
    click.echo(f"  CAMPFIRE_ROOT:  {campfire_root or '~/campfire (default)'}")

    cfg = load_config(config_path)
    setup_environment(cfg)

    from campfire_pipeline.common.version import get_reduction_version
    version = get_reduction_version(cfg)
    click.echo(f"  Pipeline ver:   {version}")

    # CRDS settings
    crds_server = os.environ.get('CRDS_SERVER_URL', '(not set)')
    crds_context = os.environ.get('CRDS_CONTEXT', '(not set)')
    crds_path = os.environ.get('CRDS_PATH', '(not set)')
    click.echo()
    click.echo("  CRDS:")
    click.echo(f"    Server:       {crds_server}")
    click.echo(f"    Context:      {crds_context}")
    click.echo(f"    Cache:        {crds_path}")

    # Resolved paths
    try:
        paths = resolve_paths(cfg)
        click.echo()
        click.echo("  Paths:")
        click.echo(f"    data_dir:     {paths['data_dir']}")
        click.echo(f"    products_dir: {paths['products_dir']}")
    except RuntimeError as e:
        click.echo()
        click.echo(f"  Paths:          {e}")


# ---------------------------------------------------------------------------
# download command
# ---------------------------------------------------------------------------

INSTRUMENT_DEFAULTS = {
    'NIRSPEC': 'NRS_MSASPEC',
    'NIRCAM': 'NRC_IMAGE',
}


@main.command()
@click.option('--program', type=int, default=None,
              help='Program (proposal) number. Required for JWST; one way to '
                   'select HST exposures.')
@click.option('--telescope', type=click.Choice(['jwst', 'hst'],
              case_sensitive=False), default=None,
              help='Telescope. "hst" with no --instrument means ACS/WFC and '
                   'WFC3/IR.')
@click.option('--instrument', type=click.Choice(
              ['nirspec', 'nircam', 'acswfc', 'wfc3ir'], case_sensitive=False),
              default=None,
              help='Instrument (default: nirspec, or both HST detectors with '
                   '--telescope hst).')
@click.option('--field', 'field_name', default=None,
              help='HST: exposures whose footprint touches a tile of this '
                   'field (as `cfpipe query --field`).')
@click.option('--tiles', default=None,
              help='With --field: comma-separated tile names (default: all).')
@click.option('--from', 'from_path', default=None,
              help='HST: download the exposures listed in the JSON that '
                   '`cfpipe query --format json` wrote ("-" reads it from '
                   'standard input).')
@click.option('--obs-id', 'obs_ids', type=int, multiple=True,
              help='JWST observation number(s); repeat for multiple (e.g. --obs-id 1 --obs-id 2).')
@click.option('--filters', 'filters', multiple=True,
              help='NIRCam: restrict to these filters (repeat for multiple). Ignored for NIRSpec.')
@click.option('--target', 'targets', multiple=True,
              help='Cone-search center: object name (e.g. "M1") or "RA Dec" in '
                   'decimal degrees (e.g. "150.1163 2.2070"). Repeat for multiple.')
@click.option('--radius', type=float, default=None,
              help='Cone-search radius (default: 3 arcmin server-side; max: 30 arcmin). '
                   'Requires --target.')
@click.option('--radius-units', type=click.Choice(['arcmin', 'arcsec'],
              case_sensitive=False), default='arcmin', show_default=True,
              help='Units for --radius.')
@click.option('--exp-type', default=None,
              help='Exposure type (default: auto from instrument).')
@click.option('--download-dir', default=None,
              help='Download directory (default: $CAMPFIRE_ROOT/raw, or ~/campfire/raw if unset).')
@click.option('--dry-run', is_flag=True,
              help='List files without downloading.')
@click.option('--token', default=None,
              help='MAST API token for proprietary data. Falls back to $MAST_API_TOKEN env var.')
@click.option('--processes', '-p', type=int, default=4, show_default=True,
              help='Number of parallel download streams.')
@click.option('--backend', type=click.Choice(['tap', 'search'],
              case_sensitive=False), default='tap', show_default=True,
              help='How files are found: "tap" queries the MAST CAOM catalog '
                   'in one request; "search" uses the JWST Search API '
                   '(/search + /list_products), which can take very long '
                   'under load. --target always uses "search".')
def download(program, telescope, instrument, field_name, tiles, from_path,
             obs_ids, filters, targets, radius, radius_units,
             exp_type, download_dir, dry_run, token, processes, backend):
    """Download raw data from MAST.

    NIRSpec layout: $CAMPFIRE_ROOT/raw/nirspec/{PID}/{filename}
    NIRCam layout:  $CAMPFIRE_ROOT/raw/nircam/{PID}/{filter}/{filename}
                    plus a manifest.ecsv per PID directory.
    HST layout:     $CAMPFIRE_ROOT/raw/{acswfc,wfc3ir}/{PID}/{filter}/{rootname}_raw.fits
                    plus a manifest.ecsv per instrument and PID.

    JWST downloads are by --program. Auxiliary metafiles (e.g. NIRSpec MSA
    metadata) are fetched first so reduction can begin while uncal files are
    still downloading.

    HST downloads take the selection of `cfpipe query` (--program, --field,
    --target with --radius, --filters), or the exposures query listed:

    \b
      cfpipe download --telescope hst --field pg004 --dry-run
      cfpipe query --instrument wfc3ir --program 14096 --format json \\
          | cfpipe download --from -
    """
    import requests
    from campfire_pipeline.common.query import (
        MastQueryError, MastTransientError, download_jwst_data,
    )

    if processes < 1:
        raise click.BadParameter('--processes must be >= 1')
    token = token or os.environ.get('MAST_API_TOKEN')
    hst = ((telescope or '').lower() == 'hst'
           or (instrument or '').lower() in ('acswfc', 'wfc3ir')
           or from_path is not None)
    if hst:
        _download_hst(program, telescope, instrument, field_name, tiles,
                      from_path, filters, targets, radius, radius_units,
                      download_dir, dry_run, token, processes)
        return
    if program is None:
        raise click.UsageError('--program is required for JWST downloads')
    if field_name or tiles:
        raise click.UsageError(
            '--field selects HST exposures; JWST downloads are by --program '
            '(`cfpipe query --telescope jwst --field ...` lists the programs)')
    instrument = instrument or 'nirspec'

    instrument_upper = instrument.upper()

    if exp_type is None:
        exp_type = INSTRUMENT_DEFAULTS.get(instrument_upper, 'NRS_MSASPEC')

    if download_dir is None:
        from campfire_pipeline.config import _get_campfire_root
        download_dir = os.path.join(_get_campfire_root(), 'raw')

    if filters and instrument_upper != 'NIRCAM':
        click.echo("Warning: --filters is ignored for NIRSpec.")
        filters = ()

    radius_units_full = {'arcmin': 'arcminutes', 'arcsec': 'arcseconds'}[
        radius_units.lower()
    ]
    if radius is not None and not targets:
        raise click.BadParameter('--radius requires --target.')
    if radius is not None:
        if radius <= 0:
            raise click.BadParameter('--radius must be positive.')
        radius_arcmin = radius if radius_units_full == 'arcminutes' else radius / 60.0
        if radius_arcmin > 30:
            raise click.BadParameter(
                f'--radius {radius} {radius_units_full} exceeds the MAST '
                f'server-side cap of 30 arcminutes.'
            )

    try:
        download_jwst_data(
            program_id=program,
            instrument=instrument_upper,
            exp_type=exp_type,
            download_dir=download_dir,
            dry_run=dry_run,
            obs_ids=list(obs_ids) if obs_ids else None,
            filters=list(filters) if filters else None,
            targets=list(targets) if targets else None,
            radius=radius,
            radius_units=radius_units_full if targets else None,
            token=token,
            workers=processes,
            backend=backend.lower(),
        )
    except requests.HTTPError as e:
        msg = f"MAST API error: {e}"
        if targets:
            msg += (
                "\n\nHint: --target must be a resolvable object name (e.g. "
                '"M1") or a "RA Dec" pair in decimal degrees separated by a '
                'space (e.g. --target "215.0 52.9"). A comma-separated pair '
                "is rejected by MAST's resolver."
            )
        raise click.ClickException(msg)
    except MastTransientError as e:
        # Transient MAST /list_products failures (batches that never stopped
        # timing out, or an empty 200 for filesets that exist) carry their own
        # re-run hint — show it cleanly, not as a traceback. Deliberately not
        # a bare `except RuntimeError`: every other RuntimeError raised
        # anywhere under download_jwst_data is a real bug and keeps its
        # traceback.
        raise click.ClickException(str(e))
    except MastQueryError as e:
        raise click.ClickException(str(e))
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted. Re-run to resume (existing files will be skipped).")
        sys.exit(130)


def _download_hst(program, telescope, instrument, field_name, tiles, from_path,
                  filters, targets, radius, radius_units, download_dir, dry_run,
                  token, processes):
    """The HST branch of ``cfpipe download``: select as ``cfpipe query`` does
    (or read its JSON), then fetch the RAW files."""
    from pathlib import Path

    from campfire_pipeline.common import mast_query
    from campfire_pipeline.common.query import MastQueryError
    from campfire_pipeline.config import _get_campfire_root

    root = (Path(download_dir).parent if download_dir
            else Path(_get_campfire_root()))
    try:
        if from_path is not None:
            if program is not None or field_name or targets or filters:
                raise click.UsageError(
                    '--from takes the exposures as listed; select with '
                    '`cfpipe query`, not here')
            text = (click.get_text_stream('stdin').read() if from_path == '-'
                    else Path(from_path).read_text())
            exposures = mast_query.from_json(text)
        else:
            chosen = mast_query.resolve_instruments(
                telescope or 'hst', [instrument] if instrument else None)
            if field_name and targets:
                raise click.UsageError('give --field or --target, not both')
            if len(targets) > 1:
                raise click.UsageError('one --target at a time for HST')
            regions = None
            if field_name:
                from campfire_pipeline.nircam.field import Field
                regions = mast_query.field_polygons(
                    Field.load(field_name),
                    [t.strip() for t in tiles.split(',')] if tiles else None)
            elif targets:
                arcmin = 3.0 if radius is None else (
                    radius if radius_units.lower() == 'arcmin' else radius / 60.0)
                ra, dec = _parse_target(targets[0])
                regions = [mast_query.cone_polygon(ra, dec, arcmin / 60.0)]
            if program is None and regions is None:
                raise click.UsageError(
                    'give --program, --field, --target or --from')
            exposures = mast_query.query_exposures(
                chosen, programs=[program] if program is not None else None,
                regions=regions, filters=list(filters) or None, log=click.echo)
        counts = mast_query.download_hst(
            exposures, root, token=token, workers=processes, dry_run=dry_run,
            log=click.echo)
    except (FileNotFoundError, ValueError, MastQueryError) as e:
        raise click.ClickException(str(e))
    except requests_errors() as e:
        raise click.ClickException(f'MAST could not be reached: {e}')
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted. Re-run to resume (existing files will be skipped).")
        sys.exit(130)
    if counts['errors']:
        raise click.ClickException(
            f"{counts['errors']} file(s) failed to download; re-run to retry "
            f"(files already on disk are skipped)")


def _parse_target(text):
    """``"RA Dec"`` (decimal degrees, space or comma) or an object name, to
    ``(ra, dec)`` in degrees."""
    parts = text.replace(',', ' ').split()
    if len(parts) == 2:
        try:
            ra, dec = float(parts[0]), float(parts[1])
        except ValueError:
            pass
        else:
            if not (0.0 <= ra < 360.0 and -90.0 <= dec <= 90.0):
                raise click.BadParameter(
                    f'--target "{text}": RA must be in [0, 360) and Dec in '
                    f'[-90, 90] (decimal degrees).')
            return ra, dec
    from astropy.coordinates import SkyCoord
    try:
        c = SkyCoord.from_name(text)
    except Exception as e:                      # resolver or network failure
        raise click.BadParameter(
            f'--target "{text}" is not an "RA Dec" pair in decimal degrees '
            f'and could not be resolved as an object name ({e}).')
    return float(c.ra.deg), float(c.dec.deg)


@main.command()
@click.option('--telescope', type=click.Choice(['jwst', 'hst'],
              case_sensitive=False), default=None,
              help='Telescope. With no --instrument, every supported '
                   'instrument of it.')
@click.option('--instrument', 'instruments', multiple=True,
              type=click.Choice(['nircam', 'nirspec', 'acswfc', 'wfc3ir'],
                                case_sensitive=False),
              help='Instrument (repeat for several). Implies the telescope.')
@click.option('--program', 'programs', type=int, multiple=True,
              help='Proposal number (repeat for several).')
@click.option('--field', 'field_name', default=None,
              help='Field in fields.toml: exposures whose footprint touches '
                   'one of its tiles.')
@click.option('--tiles', default=None,
              help='With --field: comma-separated tile names (default: all).')
@click.option('--target', default=None,
              help='Position: "RA Dec" in decimal degrees, or an object name.')
@click.option('--radius', type=float, default=3.0, show_default=True,
              help='With --target: radius in arcminutes.')
@click.option('--filters', 'filters', multiple=True,
              help='Keep these filters only (repeat for several).')
@click.option('--include-calibration', is_flag=True,
              help='Also list calibration exposures (darks, flats, ...).')
@click.option('--format', 'fmt', default='summary', show_default=True,
              type=click.Choice(['summary', 'exposures', 'json'],
                                case_sensitive=False),
              help='summary: per program and filter, with the file globs for '
                   'fields.toml. exposures: one line per file set. json: '
                   'every field, machine-readable.')
def query(telescope, instruments, programs, field_name, tiles, target, radius,
          filters, include_calibration, fmt):
    """List what MAST holds, for JWST or HST, without downloading anything.

    Select by program, by a field's tiles, by position, or a combination
    (program AND region). Examples:

    \b
      cfpipe query --telescope hst --field pg004
      cfpipe query --instrument acswfc --target "289.27 -33.52" --radius 4
      cfpipe query --instrument nircam --program 6882 --format exposures

    The summary ends with the file globs that select each program, to paste
    into fields.toml: which exposures belong to a field is decided there, not
    by this query.
    """
    from campfire_pipeline.common import mast_query
    from campfire_pipeline.common.query import MastQueryError

    try:
        chosen = mast_query.resolve_instruments(telescope, instruments)
    except ValueError as e:
        raise click.UsageError(str(e))
    if field_name and target:
        raise click.UsageError('give --field or --target, not both')
    if tiles and not field_name:
        raise click.UsageError('--tiles needs --field')

    regions = None
    if field_name:
        from campfire_pipeline.nircam.field import Field
        try:
            field_obj = Field.load(field_name)
            regions = mast_query.field_polygons(
                field_obj, [t.strip() for t in tiles.split(',')] if tiles else None)
        except (FileNotFoundError, ValueError) as e:
            raise click.ClickException(str(e))
    elif target:
        if radius <= 0:
            raise click.BadParameter('--radius must be positive.')
        ra, dec = _parse_target(target)
        regions = [mast_query.cone_polygon(ra, dec, radius / 60.0)]
    if not programs and regions is None:
        raise click.UsageError('give --program, --field or --target')

    quiet = fmt.lower() == 'json'
    try:
        exposures = mast_query.query_exposures(
            chosen, programs=list(programs) or None, regions=regions,
            filters=list(filters) or None,
            science_only=not include_calibration,
            log=(lambda msg: click.echo(msg, err=True)) if quiet else click.echo)
    except MastQueryError as e:
        raise click.ClickException(str(e))
    except requests_errors() as e:
        raise click.ClickException(f'MAST could not be reached: {e}')

    if fmt.lower() == 'json':
        click.echo(mast_query.to_json(exposures))
    elif fmt.lower() == 'exposures':
        click.echo(mast_query.format_exposures(exposures))
    else:
        click.echo()
        click.echo(mast_query.format_summary(exposures))


def requests_errors():
    """The network exception classes of ``requests``, imported on demand."""
    import requests
    return (requests.Timeout, requests.ConnectionError, requests.HTTPError)


if __name__ == '__main__':
    main()
