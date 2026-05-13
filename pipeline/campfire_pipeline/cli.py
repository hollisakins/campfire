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

import matplotlib
matplotlib.use('Agg')

import click

from campfire_pipeline.config import load_config, setup_environment, resolve_paths


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name='campfire-pipeline')
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
@click.option('--program', type=int, required=True,
              help='JWST program ID.')
@click.option('--instrument', type=click.Choice(['nirspec', 'nircam'],
              case_sensitive=False), default='nirspec',
              help='Instrument (default: nirspec).')
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
def download(program, instrument, obs_ids, filters, targets, radius, radius_units,
             exp_type, download_dir, dry_run, token, processes):
    """Download raw JWST data from MAST.

    NIRSpec layout: $CAMPFIRE_ROOT/raw/{PID}/{filename}
    NIRCam layout:  $CAMPFIRE_ROOT/raw/nircam/{PID}/{filter}/{filename}
                    plus a manifest.ecsv per PID directory.

    Auxiliary metafiles (e.g. NIRSpec MSA metadata) are fetched first so
    reduction can begin while uncal files are still downloading.
    """
    from campfire_pipeline.common.query import download_jwst_data

    instrument_upper = instrument.upper()

    if exp_type is None:
        exp_type = INSTRUMENT_DEFAULTS.get(instrument_upper, 'NRS_MSASPEC')

    if download_dir is None:
        from campfire_pipeline.config import _get_campfire_root
        download_dir = os.path.join(_get_campfire_root(), 'raw')

    token = token or os.environ.get('MAST_API_TOKEN')

    if filters and instrument_upper != 'NIRCAM':
        click.echo("Warning: --filters is ignored for NIRSpec.")
        filters = ()

    if processes < 1:
        raise click.BadParameter('--processes must be >= 1')

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
        )
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted. Re-run to resume (existing files will be skipped).")
        sys.exit(130)


if __name__ == '__main__':
    main()
