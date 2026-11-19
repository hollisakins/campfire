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


def _stars_from_gaia(table, gaia_catalog, epoch_hint, *, size_column="rhalf",
                     match_radius=0.3, command="refcat extract"):
    """Apply ``motion.use_gaia_for_stars`` to a catalog; the Gaia table is
    read from *gaia_catalog* or queried over the catalog's extent."""
    import numpy as np
    from astropy.coordinates import SkyCoord

    from campfire_pipeline.nircam.refcat.motion import use_gaia_for_stars

    if gaia_catalog:
        gaia = read_refcat(gaia_catalog)
    else:
        sky = SkyCoord(np.asarray(table["RA"], float), np.asarray(table["DEC"], float),
                       unit="deg")
        # Mean direction on the sphere, not a median of RA: a catalog that
        # straddles RA 0 would otherwise get a center on the far side of the
        # sky and an all-sky query.
        from astropy.coordinates import UnitSphericalRepresentation
        center = SkyCoord(
            sky.cartesian.mean().represent_as(UnitSphericalRepresentation))
        radius = float(center.separation(sky).max().deg) + 0.01
        log(f"{command}: querying Gaia within {radius * 60:.1f}' of "
            f"({center.ra.deg:.5f}, {center.dec.deg:.5f})")
        gaia = query_external("gaia", (center.ra.deg, center.dec.deg), radius)
    out, info = use_gaia_for_stars(table, gaia, epoch_hint=epoch_hint,
                                   size_column=size_column,
                                   match_radius=match_radius)
    log(f"{command}: {info['n_gaia_stars']} Gaia stars now carry Gaia's "
        f"position and proper motion; dropped {info['n_dropped_no_motion']} Gaia "
        f"stars without a proper motion and {info['n_dropped_compact']} compact "
        f"sources not in Gaia; {info['n_kept']} of {info['n_catalog']} kept.")
    if info.get("frame_offset_mas"):
        dra, ddec = info["frame_offset_mas"]
        log(f"{command}: at the hinted epoch the catalog's own positions of "
            f"those stars sit ({dra:+.1f}, {ddec:+.1f}) mas in RA, Dec from "
            f"Gaia's. The stars now follow Gaia and the galaxies keep the "
            f"catalog's frame, so that is how far the two parts disagree.")
    if info["size_limit"] is None:
        if size_column not in table.colnames:
            log(f"{command}: the catalog has no '{size_column}' column, so no "
                f"source was dropped for being compact. Point sources that are "
                f"not in Gaia remain at the catalog's own epoch.")
        else:
            log(f"{command}: too few Gaia stars to say what a point source "
                f"looks like here; no source was dropped for being compact.")
    return out, info


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
    """Manage astrometric reference catalogs (query / extract / add-gaia /
    merge / compare)."""
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
                   "g/r/i/z for ls_dr10 (default i), g/r/i/z/y for hsc_ssp "
                   "(default i, cmodel mags).")
@click.option("--mag-max", default=None, type=float,
              help="Upper magnitude cut applied server-side.")
@click.option("--no-point-sources", is_flag=True,
              help="ls_dr10 / hsc_ssp: include extended sources too.")
@click.option("--stars", type=click.Choice(["gaia", "keep"]), default=None,
              help="ls_dr10 only. 'gaia' (default): Gaia stars carry their "
                   "proper motion (the align step moves them to each "
                   "exposure's date), and point sources bright enough to be "
                   "recognized as stars that have no proper motion are "
                   "dropped. 'keep': sources as listed, which puts Gaia "
                   "stars at their 2016 positions.")
@click.option("--row-limit", default=-1, type=int,
              help="gaia only: cap on returned rows (-1 = unlimited).")
@click.option("--hsc-release", "hsc_release",
              type=click.Choice(["auto", "wide", "dud"]), default="auto",
              show_default=True,
              help="hsc_ssp only: which PDR3 schema to query. 'auto' "
                   "splits the cone between pdr3_dud_rev and pdr3_wide "
                   "based on which tracts it overlaps.")
@click.option("--hsc-user", default=None,
              help="hsc_ssp only: HSC SSP account name. Falls back to "
                   "$HSC_SSP_USER, then ~/.netrc.")
@click.option("--hsc-password", default=None,
              help="hsc_ssp only: HSC SSP password. Falls back to "
                   "$HSC_SSP_PASSWORD, then ~/.netrc.")
@click.option("--out", "out_path", default=None,
              help="Output path; default = "
                   "<field.refcat_dir>/<field>_<backend>_<band>_refcat.ecsv.")
@click.option("--notes", default=None,
              help="Free-form note stamped into the catalog meta.")
@click.option("--overwrite", is_flag=True)
def query(config, field_name, backend, center, radius_deg, mag_band, mag_max,
          no_point_sources, stars, row_limit, hsc_release, hsc_user, hsc_password,
          out_path, notes, overwrite):
    """Query an external catalog over the field."""
    _, field_obj = _refcat_setup(config, field_name)
    center_radec = _parse_center(center, field_obj)

    if mag_band is None:
        mag_band = "G" if backend == "gaia" else "i"
    if stars is not None and backend != "ls_dr10":
        raise click.ClickException(
            f"--stars applies to the ls_dr10 backend only (got backend {backend!r})."
        )

    kwargs = {"mag_band": mag_band, "mag_max": mag_max}
    if backend == "gaia":
        kwargs["row_limit"] = row_limit
    elif backend == "ls_dr10":
        kwargs["point_sources"] = not no_point_sources
        kwargs["stars"] = stars or "gaia"
    elif backend == "hsc_ssp":
        kwargs["point_sources"] = not no_point_sources
        kwargs["release"] = hsc_release
        kwargs["user"] = hsc_user
        kwargs["password"] = hsc_password

    log(f"refcat query: backend={backend}, center={center_radec}, "
        f"radius={radius_deg} deg, band={mag_band}")
    table = query_external(backend, center_radec, radius_deg, **kwargs)
    log(f"refcat query: {len(table)} sources")

    if len(table) == 0:
        raise click.ClickException(
            f"Query returned 0 rows. Check the cone center/radius and "
            f"backend cuts ({backend!r})."
        )

    safe_kwargs = {k: v for k, v in kwargs.items()
                   if k not in ("user", "password")}
    params = {
        "backend": backend,
        "center": list(center_radec),
        "radius_deg": radius_deg,
        **safe_kwargs,
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
@click.option("--stars", type=click.Choice(["gaia", "keep"]), default="gaia",
              show_default=True,
              help="gaia: stars take their Gaia position and proper motion, "
                   "and compact sources without a proper motion are dropped, "
                   "so the catalog holds at any epoch. keep: the catalog as "
                   "extracted.")
@click.option("--gaia-catalog", "gaia_catalog", default=None,
              help="A Gaia catalog already on disk (from 'refcat query "
                   "--backend gaia'), instead of a new query.")
@click.option("--epoch-hint", "epoch_hint", default=None, type=float,
              help="Rough date of the mosaic's data as a year (e.g. 2025.8). "
                   "Only helps pair fast stars with Gaia; not written to "
                   "the catalog.")
@click.option("--notes", default=None,
              help="Free-form note stamped into the catalog meta.")
@click.option("--overwrite", is_flag=True)
def extract(config, field_name, mosaic_path, filter_name, tile, scale,
            err_path, snr_thresh, minarea, snr_min, mag_range, stars,
            gaia_catalog, epoch_hint, out_path, notes, overwrite):
    """Build a refcat by extracting sources from a mosaic.

    Two ways to point at the mosaic:

      \b
      --mosaic <path>                          (explicit, anywhere on disk)
      --filter F277W --tile A1 [--scale 30mas] (resolves under
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
                "<name> [--scale 30mas]."
            )
        mosaic_path = resolve_mosaic_path(
            field_obj, filter_name=filter_name, tile=tile, scale=scale,
        )

    mag_range_t = _parse_mag_range(mag_range)
    log(f"refcat extract: mosaic={mosaic_path}")

    table, info = extract_from_mosaic(
        mosaic_path,
        err_path=err_path,
        snr_thresh=snr_thresh, minarea=minarea,
        snr_min=snr_min, mag_range=mag_range_t,
    )
    if stars == "gaia":
        table, info["stars"] = _stars_from_gaia(table, gaia_catalog, epoch_hint)

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
# add-gaia
# ---------------------------------------------------------------------------

def _read_any_catalog(path):
    """A catalog in the reference-catalog layout from any table file astropy
    reads (ECSV, FITS, ...), with the usual column aliases applied."""
    from astropy.table import Table

    from .io import REFCAT_COLUMNS, _canonicalize_columns

    if str(path).endswith(".ecsv"):
        table = read_refcat(path)
    else:
        table = Table.read(path)
        _canonicalize_columns(table)
    missing = [c for c in REFCAT_COLUMNS if c not in table.colnames]
    if missing:
        raise click.ClickException(
            f"{path} is not in the reference-catalog layout: no column(s) "
            f"{missing}. A reference catalog has RA and DEC (degrees, ICRS), "
            f"mag and mag_err; found {table.colnames[:12]}"
            f"{' ...' if len(table.colnames) > 12 else ''}.")
    return table


@refcat.command(name="add-gaia")
@click.option("--field", "field_name", required=True,
              help="Field name from fields.toml (the catalog's home and the "
                   "name recorded in it).")
@click.argument("catalog", type=click.Path(exists=True))
@click.option("--out", "out_path", default=None,
              help="Output path; default = <field's catalog directory>/"
                   "<input name>_gaia.ecsv.")
@click.option("--size-column", default="rhalf", show_default=True,
              help="Column that measures source size, in any unit. Sources "
                   "not in Gaia that are as small as the Gaia stars in this "
                   "column are dropped (they are stars with an unknown "
                   "motion). Without the column nothing is dropped on size.")
@click.option("--match-radius", default=0.3, type=float, show_default=True,
              help="Match radius to Gaia in arcsec.")
@click.option("--gaia-catalog", "gaia_catalog", default=None,
              help="A Gaia catalog already on disk (from 'refcat query "
                   "--backend gaia'), instead of a new query.")
@click.option("--epoch-hint", "epoch_hint", default=None, type=float,
              help="Rough date of the catalog's positions as a year (e.g. "
                   "2023.3). Gaia is moved there before matching, so fast "
                   "stars still pair up; not written to the catalog.")
@click.option("--notes", default=None,
              help="Free-form note stamped into the catalog meta.")
@click.option("--overwrite", is_flag=True)
def add_gaia(field_name, catalog, out_path, size_column, match_radius,
             gaia_catalog, epoch_hint, notes, overwrite):
    """Give an existing catalog Gaia's stars, so it holds at any epoch.

    What `refcat extract` does to a catalog it has just made, for a catalog
    that already exists (a survey catalog, an older reference catalog). CATALOG
    is in the reference-catalog layout: RA, DEC, mag, mag_err. In the result

    \b
      - a source matched to a Gaia star takes Gaia's position, epoch and
        proper motion; the align steps move it to each exposure's date;
      - a source matched to a Gaia star without a proper motion is dropped;
      - a source not in Gaia but as small as the Gaia stars (--size-column)
        is dropped;
      - every other source stays where it is.

    A catalog without proper motions holds stars only at the date of its own
    imaging: on COSMOS ACS data from 2004, the stars of a 2023 catalog sat 70
    mas away and pulled the alignment by 6 mas.

    \b
    Example:
        cfpipe nircam refcat add-gaia --field cosmos cosmos2025_cut.ecsv \\
            --size-column radius_sersic --epoch-hint 2023.3
    """
    import toml
    from campfire_layout import Scope, dir_for
    from campfire_pipeline.config import resolve_fields_file

    # Only the field's name is needed (the catalog's home), so the field is
    # looked up in fields.toml without loading its exposures.
    try:
        fields_file = resolve_fields_file(None)
        with open(fields_file, 'r') as f:
            known = list(toml.load(f))
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))
    if field_name not in known:
        raise click.ClickException(
            f"Field '{field_name}' not found in {fields_file}. Available: {known}")
    table = _read_any_catalog(catalog)
    log(f"refcat add-gaia: {catalog}: {len(table)} sources")
    table, info = _stars_from_gaia(table, gaia_catalog, epoch_hint,
                                   size_column=size_column,
                                   match_radius=match_radius,
                                   command="refcat add-gaia")
    info["input"] = os.path.abspath(catalog)
    table.meta.update(make_meta(field_name, source="add-gaia",
                                params=info, notes=notes))
    if out_path is None:
        stem = os.path.splitext(os.path.basename(catalog))[0]
        refcat_dir = dir_for("nircam_astrom_cat", Scope(field=field_name))
        out = os.path.join(str(refcat_dir), f"{stem}_gaia.ecsv")
    else:
        out = os.path.abspath(out_path)
    write_refcat(table, out, overwrite=overwrite)
    log(f"refcat add-gaia: wrote {out}")


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
