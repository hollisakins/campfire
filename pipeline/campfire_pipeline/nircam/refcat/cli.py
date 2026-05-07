"""
Click subcommands for ``cfpipe nircam refcat ...``.

Wired into the top-level NIRCam CLI in ``nircam/cli.py``.
"""

import json
import os

import astropy.units as u
import click

from campfire_pipeline.common.io import log

from .compare import compare_catalogs, plot_comparison
from .extract import extract_from_mosaic, resolve_mosaic_path
from .io import make_meta, read_refcat, write_refcat
from .merge import label_from_path, merge_refcats
from .query import SUPPORTED_BACKENDS, query as query_external


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _refcat_setup(config_path, field_name):
    """``_setup`` analogue local to refcat — load config + Field."""
    # Imported here rather than at module top to avoid a circular import
    # (the parent ``nircam/cli.py`` registers this module's ``main``).
    from campfire_pipeline.nircam.cli import _setup
    return _setup(config_path, field_name)


def _parse_center(center_str, field_obj):
    """``--center RA,DEC`` parser with field-tangent fallback."""
    if center_str is None:
        return tuple(field_obj.tangent_point)
    parts = [p.strip() for p in center_str.split(",")]
    if len(parts) != 2:
        raise click.BadParameter(
            "--center must be 'RA,DEC' in decimal degrees"
        )
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as e:
        raise click.BadParameter(f"--center parse error: {e}")


def _parse_mag_range(spec):
    """``--mag-range MIN,MAX`` parser → ``(float, float)`` or ``None``."""
    if spec is None:
        return None
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 2:
        raise click.BadParameter("--mag-range must be 'MIN,MAX'")
    try:
        return (float(parts[0]), float(parts[1]))
    except ValueError as e:
        raise click.BadParameter(f"--mag-range parse error: {e}")


def _default_query_outname(field_name, backend, mag_band):
    return f"{field_name}_{backend}_{mag_band.lower()}_refcat.ecsv"


def _resolve_output(out_arg, default_name, field_obj):
    """Output is either an explicit path or ``field.refcat_dir/<default>``."""
    if out_arg:
        return os.path.abspath(out_arg)
    return os.path.join(field_obj.refcat_dir, default_name)


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------

@click.group()
def refcat():
    """Manage astrometric reference catalogs (query / extract / merge / compare)."""
    pass


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

@refcat.command()
@click.option("--config", default=None, help="Path to configuration file.")
@click.option("--field", "field_name", required=True,
              help="Field name from fields.toml.")
@click.option("--backend", required=True,
              type=click.Choice(SUPPORTED_BACKENDS),
              help="External catalog source.")
@click.option("--center", default=None,
              help="Cone center as 'RA,DEC' (deg). Defaults to "
                   "the field's tangent_point.")
@click.option("--radius", "radius_deg", default=0.1, type=float,
              help="Cone radius in degrees (default 0.1).")
@click.option("--mag-band", default=None,
              help="Magnitude band: G/BP/RP for gaia (default G), "
                   "g/r/i/z for ls_dr10 (default i).")
@click.option("--mag-max", default=None, type=float,
              help="Upper magnitude cut applied server-side.")
@click.option("--no-point-sources", is_flag=True,
              help="ls_dr10 only: include extended sources too.")
@click.option("--row-limit", default=-1, type=int,
              help="gaia only: cap on returned rows (-1 = unlimited).")
@click.option("--out", "out_path", default=None,
              help="Output path; default = "
                   "<field.refcat_dir>/<field>_<backend>_<band>_refcat.ecsv.")
@click.option("--notes", default=None,
              help="Free-form note stamped into the catalog meta.")
@click.option("--overwrite", is_flag=True)
def query(config, field_name, backend, center, radius_deg, mag_band, mag_max,
          no_point_sources, row_limit, out_path, notes, overwrite):
    """Query an external catalog over the field."""
    _, field_obj = _refcat_setup(config, field_name)
    center_radec = _parse_center(center, field_obj)

    if mag_band is None:
        mag_band = "G" if backend == "gaia" else "i"

    kwargs = {"mag_band": mag_band, "mag_max": mag_max}
    if backend == "gaia":
        kwargs["row_limit"] = row_limit
    elif backend == "ls_dr10":
        kwargs["point_sources"] = not no_point_sources

    log(f"refcat query: backend={backend}, center={center_radec}, "
        f"radius={radius_deg} deg, band={mag_band}")
    table = query_external(backend, center_radec, radius_deg, **kwargs)
    log(f"refcat query: {len(table)} sources")

    if len(table) == 0:
        raise click.ClickException(
            f"Query returned 0 rows. Check the cone center/radius and "
            f"backend cuts ({backend!r})."
        )

    params = {
        "backend": backend,
        "center": list(center_radec),
        "radius_deg": radius_deg,
        **kwargs,
    }
    table.meta.update(make_meta(field_obj.name, source="query",
                                params=params, notes=notes))

    out = _resolve_output(out_path,
                          _default_query_outname(field_obj.name, backend,
                                                 mag_band),
                          field_obj)
    write_refcat(table, out, overwrite=overwrite)
    log(f"refcat query: wrote {out}")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

@refcat.command()
@click.option("--config", default=None, help="Path to configuration file.")
@click.option("--field", "field_name", required=True,
              help="Field name from fields.toml.")
@click.option("--mosaic", "mosaic_path", default=None,
              help="Explicit mosaic FITS path. Mutually exclusive with "
                   "--filter/--tile.")
@click.option("--filter", "filter_name", default=None,
              help="Filter to resolve via field.filter_dir.")
@click.option("--tile", default=None,
              help="Tile name (must exist in fields.toml).")
@click.option("--scale", default="30mas",
              help="Pixel-scale tag (default 30mas).")
@click.option("--version", default="latest",
              help="Mosaic version tag (default 'latest').")
@click.option("--err", "err_path", default=None,
              help="Override error-map path; auto-detected otherwise.")
@click.option("--snr-thresh", default=3.0, type=float,
              help="SEP per-pixel SNR detection threshold (default 3.0).")
@click.option("--minarea", default=15, type=int,
              help="SEP minimum area in pixels (default 15).")
@click.option("--snr-min", default=10.0, type=float,
              help="Lower cut on integrated source SNR (default 10).")
@click.option("--mag-range", default=None,
              help="Bracket of acceptable AB mags as 'MIN,MAX'.")
@click.option("--out", "out_path", default=None,
              help="Output path; default = "
                   "<field.refcat_dir>/<field>_<filter>_extract_refcat.ecsv.")
@click.option("--notes", default=None,
              help="Free-form note stamped into the catalog meta.")
@click.option("--overwrite", is_flag=True)
def extract(config, field_name, mosaic_path, filter_name, tile, scale, version,
            err_path, snr_thresh, minarea, snr_min, mag_range, out_path,
            notes, overwrite):
    """Build a refcat by extracting sources from a mosaic.

    Two ways to point at the mosaic:

      \b
      --mosaic <path>                          (explicit, anywhere on disk)
      --filter F277W --tile A1 [--scale 30mas] [--version latest]
                                               (resolves under
                                                field.filter_dir(<filter>)/)
    """
    _, field_obj = _refcat_setup(config, field_name)

    if mosaic_path and (filter_name or tile):
        raise click.UsageError(
            "--mosaic is mutually exclusive with --filter/--tile."
        )
    if mosaic_path is None:
        if not (filter_name and tile):
            raise click.UsageError(
                "Provide either --mosaic <path> or --filter <name> --tile "
                "<name> [--scale 30mas] [--version latest]."
            )
        mosaic_path = resolve_mosaic_path(
            field_obj, filter_name=filter_name, tile=tile,
            scale=scale, version=version,
        )

    mag_range_t = _parse_mag_range(mag_range)
    log(f"refcat extract: mosaic={mosaic_path}")

    table, info = extract_from_mosaic(
        mosaic_path,
        err_path=err_path,
        snr_thresh=snr_thresh, minarea=minarea,
        snr_min=snr_min, mag_range=mag_range_t,
    )

    table.meta.update(make_meta(
        field_obj.name, source="extract",
        params=info,
        notes=notes,
    ))

    if out_path is None:
        if filter_name:
            default = (f"{field_obj.name}_{filter_name.lower()}_extract"
                       f"_refcat.ecsv")
        else:
            base = os.path.splitext(os.path.basename(mosaic_path))[0]
            default = f"{base}_refcat.ecsv"
        out = os.path.join(field_obj.refcat_dir, default)
    else:
        out = os.path.abspath(out_path)
    write_refcat(table, out, overwrite=overwrite)
    log(f"refcat extract: wrote {out}")


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

@refcat.command()
@click.option("--config", default=None, help="Path to configuration file.")
@click.option("--field", "field_name", required=True,
              help="Field name from fields.toml.")
@click.argument("catalogs", nargs=-1, type=click.Path(exists=True))
@click.option("--label", "labels", multiple=True,
              help="Label per input (matched by order). Defaults to the "
                   "input filename.")
@click.option("--match-radius", default=3.0, type=float,
              help="Sky-match dedup tolerance in arcsec (default 3.0).")
@click.option("--out", "out_path", required=True,
              help="Output path for the merged catalog.")
@click.option("--notes", default=None,
              help="Free-form note stamped into the merged catalog meta.")
@click.option("--overwrite", is_flag=True)
def merge(config, field_name, catalogs, labels, match_radius, out_path, notes,
          overwrite):
    """Merge two or more refcats with positional dedup. First wins.

    \b
    Example:
        cfpipe nircam refcat merge --field uds \\
            uds_f277w_extract_refcat.ecsv \\
            hsc_ssp_pdr3_2026.ecsv \\
            --out uds_f277w_plus_hsc_refcat.ecsv
    """
    _, field_obj = _refcat_setup(config, field_name)
    if len(catalogs) < 2:
        raise click.UsageError("merge needs >=2 input catalogs")
    if labels and len(labels) != len(catalogs):
        raise click.UsageError(
            "If --label is given, pass exactly one per input catalog."
        )

    tables = [read_refcat(p) for p in catalogs]
    label_list = list(labels) if labels else [label_from_path(p) for p in catalogs]

    merged, info = merge_refcats(
        tables, labels=label_list,
        match_radius=match_radius * u.arcsec,
    )

    merged.meta.update(make_meta(
        field_obj.name, source="merge",
        params={"inputs": [os.path.abspath(p) for p in catalogs],
                "labels": label_list,
                "match_radius_arcsec": match_radius,
                "summary": info},
        notes=notes,
    ))
    write_refcat(merged, os.path.abspath(out_path), overwrite=overwrite)
    log(f"refcat merge: wrote {out_path} ({len(merged)} rows)")


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

@refcat.command()
@click.option("--config", default=None, help="Path to configuration file.")
@click.option("--field", "field_name", required=True,
              help="Field name from fields.toml.")
@click.argument("cat_a", type=click.Path(exists=True))
@click.argument("cat_b", type=click.Path(exists=True))
@click.option("--match-radius", default=0.5, type=float,
              help="Match radius in arcsec (default 0.5).")
@click.option("--save-plot", "save_plot", default=None,
              help="Path for the dRA/dDec 2D-histogram PNG/PDF. "
                   "Default: <field.refcat_dir>/diagnostics/"
                   "compare_<a>_vs_<b>.png.")
@click.option("--no-plot", is_flag=True,
              help="Skip the plot; just print summary stats.")
@click.option("--name-a", default=None,
              help="Display name for catalog A (default: filename).")
@click.option("--name-b", default=None,
              help="Display name for catalog B (default: filename).")
def compare(config, field_name, cat_a, cat_b, match_radius, save_plot,
            no_plot, name_a, name_b):
    """Compare two catalogs and report ΔRA/ΔDec residuals."""
    _, field_obj = _refcat_setup(config, field_name)
    a = read_refcat(cat_a)
    b = read_refcat(cat_b)
    name_a = name_a or label_from_path(cat_a)
    name_b = name_b or label_from_path(cat_b)

    result = compare_catalogs(a, b, match_radius=match_radius * u.arcsec)

    log(f"refcat compare: {name_a} ({result['n_a']}) vs "
        f"{name_b} ({result['n_b']})")
    log(f"  matched within {match_radius} arcsec: {result['n_matched']}")
    if result["n_matched"] == 0:
        return
    for axis_label, key in (("dRA", "dra_stats"), ("dDec", "ddec_stats"),
                            ("sep", "sep_stats")):
        s = result[key]
        log(f"  {axis_label:>4s}  mean={s['mean']:+7.2f} mas  "
            f"med={s['median']:+7.2f} mas  MAD={s['mad']:6.2f} mas")

    if no_plot:
        return
    if save_plot is None:
        diag_dir = os.path.join(field_obj.refcat_dir, "diagnostics")
        save_plot = os.path.join(
            diag_dir, f"compare_{name_a}_vs_{name_b}.png",
        )
    plot_comparison(result, name_a=name_a, name_b=name_b,
                    save_path=save_plot)
    log(f"refcat compare: wrote plot {save_plot}")
    _write_compare_sidecar(result, save_plot)


def _write_compare_sidecar(result, plot_path):
    """Drop a JSON sidecar with the summary stats (no per-pair arrays)."""
    sidecar = os.path.splitext(plot_path)[0] + ".json"
    payload = {k: v for k, v in result.items()
               if k not in ("dra_mas", "ddec_mas", "sep_mas")}
    with open(sidecar, "w") as f:
        json.dump(payload, f, indent=2)
