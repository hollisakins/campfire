#!/usr/bin/env python
"""A/B astrometry comparison for NIRCam mosaics: ``jhat`` vs ``align``.

Given a JHAT-aligned mosaic and an ``align``-aligned mosaic for the same
field/filter/tile, this extracts a comparable source catalog from each and
cross-matches them (a) to each other and (b) to an absolute reference catalog
(for COSMOS that is ``cosmos2025_v1_refcat.ecsv`` — the very ECSV both methods
tie to). It reports ΔRA/ΔDec/separation in milliarcseconds (mean/median/MAD)
and writes 2D-histogram QA figures, so you can read off *which method achieves
the tighter tie to the reference* and *how much the two methods disagree*.

This is a thin orchestrator over existing pipeline building blocks:
``refcat.extract.extract_from_mosaic`` (mosaic → RA/DEC/mag catalog via SEP),
``refcat.io.read_refcat`` (loads + column-canonicalizes the reference), and
``refcat.compare.compare_catalogs`` / ``plot_comparison`` (the mas-residual
comparator + figure).

Example
-------
    python scripts/nircam_ab_astrometry.py \\
        --ref  $CAMPFIRE_ROOT/reference/nircam/cosmos/astrom_cats/cosmos2025_v1_refcat.ecsv \\
        --out  $CAMPFIRE_ROOT/products/nircam/ab_astrometry_A4 \\
        --case f200w .../cosmos/f200w/mosaic_nircam_f200w_cosmos_30mas_A4_i2d.fits \\
                     .../cosmos_align/f200w/mosaic_nircam_f200w_cosmos_align_30mas_A4_i2d.fits \\
        --case f356w .../cosmos/f356w/mosaic_nircam_f356w_cosmos_30mas_A4_i2d.fits \\
                     .../cosmos_align/f356w/mosaic_nircam_f356w_cosmos_align_30mas_A4_i2d.fits

Produces, in ``--out``: ``<filter>_{jhat_vs_align,jhat_vs_ref,align_vs_ref}.png``
and a machine-readable ``ab_astrometry_summary.json``.
"""

import argparse
import json
import os

import astropy.units as u

# Which pairings to compute, and their display labels (a, b).
_PAIRINGS = {
    'jhat_vs_align': ('jhat', 'align'),
    'jhat_vs_ref': ('jhat', 'ref'),
    'align_vs_ref': ('align', 'ref'),
}


def run_ab(cat_jhat, cat_align, cat_ref, *, match_radius=0.5 * u.arcsec):
    """Three-way astrometric comparison of the two arms and the reference.

    ``cat_jhat`` / ``cat_align`` / ``cat_ref`` are tables with ``RA``/``DEC``
    (deg). Returns ``{pairing: compare_catalogs(...) dict}`` for
    ``jhat_vs_align``, ``jhat_vs_ref``, ``align_vs_ref`` — pure and in-memory,
    so it is unit-testable without any mosaics.
    """
    from campfire_pipeline.nircam.refcat.compare import compare_catalogs
    return {
        'jhat_vs_align': compare_catalogs(cat_jhat, cat_align,
                                          match_radius=match_radius),
        'jhat_vs_ref': compare_catalogs(cat_jhat, cat_ref,
                                        match_radius=match_radius),
        'align_vs_ref': compare_catalogs(cat_align, cat_ref,
                                         match_radius=match_radius),
    }


def summarize(result):
    """Strip the big per-pair arrays, keeping counts + stats for JSON output."""
    out = {}
    for pairing, r in result.items():
        out[pairing] = {
            'n_matched': r['n_matched'],
            'n_a': r['n_a'],
            'n_b': r['n_b'],
            'match_radius_arcsec': r['match_radius_arcsec'],
            'dra_stats': r['dra_stats'],
            'ddec_stats': r['ddec_stats'],
            'sep_stats': r['sep_stats'],
        }
    return out


def _fmt(stats, key):
    return f"{stats[key]:7.1f}" if stats else "     --"


def print_case(filt, result):
    """Print the headline A/B table for one filter to stdout."""
    print(f"\n=== {filt} ===  (residuals in mas; abs = vs reference catalog)")
    print(f"  {'arm':<6} {'N':>5}  {'med|sep|':>8}  "
          f"{'med dRA':>8}  {'med dDec':>8}  {'MAD dRA':>8}  {'MAD dDec':>8}")
    for arm, pairing in (('jhat', 'jhat_vs_ref'), ('align', 'align_vs_ref')):
        r = result[pairing]
        print(f"  {arm:<6} {r['n_matched']:>5}  "
              f"{_fmt(r['sep_stats'], 'median')}  "
              f"{_fmt(r['dra_stats'], 'median')}  "
              f"{_fmt(r['ddec_stats'], 'median')}  "
              f"{_fmt(r['dra_stats'], 'mad')}  "
              f"{_fmt(r['ddec_stats'], 'mad')}")
    ja = result['jhat_vs_align']
    js = ja['sep_stats']
    print(f"  jhat<->align: N={ja['n_matched']}  "
          f"med|sep|={_fmt(js, 'median')} mas  "
          f"med(dRA)={_fmt(ja['dra_stats'], 'median')}  "
          f"med(dDec)={_fmt(ja['ddec_stats'], 'median')}")


def _extract(mosaic_path, **kw):
    from campfire_pipeline.nircam.refcat.extract import extract_from_mosaic
    table, _info = extract_from_mosaic(mosaic_path, **kw)
    return table


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="A/B astrometry: jhat vs align mosaics, cross-matched to "
                    "each other and to an absolute reference catalog.")
    ap.add_argument('--ref', required=True,
                    help="Absolute reference catalog ECSV (e.g. "
                         "cosmos2025_v1_refcat.ecsv). Loaded via read_refcat, "
                         "so aliased RA/Dec column names are accepted.")
    ap.add_argument('--out', required=True,
                    help="Output directory for QA figures + summary JSON.")
    ap.add_argument('--case', nargs=3, action='append', required=True,
                    metavar=('FILTER', 'JHAT_MOSAIC', 'ALIGN_MOSAIC'),
                    help="A (filter, jhat mosaic, align mosaic) triple. "
                         "Repeatable, one per filter.")
    ap.add_argument('--match-radius', type=float, default=0.5,
                    help="Cross-match radius in arcsec (default 0.5).")
    ap.add_argument('--snr-min', type=float, default=10.0,
                    help="Lower SNR cut for source extraction (default 10).")
    ap.add_argument('--mag-min', type=float, default=None,
                    help="Optional bright AB-mag cut for extracted sources.")
    ap.add_argument('--mag-max', type=float, default=None,
                    help="Optional faint AB-mag cut for extracted sources.")
    args = ap.parse_args(argv)

    from campfire_pipeline.nircam.refcat.compare import plot_comparison
    from campfire_pipeline.nircam.refcat.io import read_refcat

    os.makedirs(args.out, exist_ok=True)
    cat_ref = read_refcat(args.ref)
    match_radius = args.match_radius * u.arcsec
    mag_range = None
    if args.mag_min is not None or args.mag_max is not None:
        mag_range = (args.mag_min if args.mag_min is not None else -99.0,
                     args.mag_max if args.mag_max is not None else 99.0)
    extract_kw = dict(snr_min=args.snr_min, mag_range=mag_range)

    combined = {}
    for filt, jhat_mosaic, align_mosaic in args.case:
        cat_jhat = _extract(jhat_mosaic, **extract_kw)
        cat_align = _extract(align_mosaic, **extract_kw)
        result = run_ab(cat_jhat, cat_align, cat_ref, match_radius=match_radius)

        for pairing, (name_a, name_b) in _PAIRINGS.items():
            plot_comparison(
                result[pairing],
                name_a=f"{name_a} {filt}", name_b=name_b,
                save_path=os.path.join(args.out, f"{filt}_{pairing}.png"))

        combined[filt] = summarize(result)
        print_case(filt, result)

    summary_path = os.path.join(args.out, 'ab_astrometry_summary.json')
    with open(summary_path, 'w') as fh:
        json.dump(combined, fh, indent=2)
    print(f"\nWrote {summary_path} and QA figures to {args.out}")


if __name__ == '__main__':
    main()
