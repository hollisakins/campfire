#!/usr/bin/env python3
"""Extract the −Var(M(θ)) score curve from diag_striping PDFs and report
per-exposure stripe-depth metrics.

Use to design a skip condition: if the score curve is flat across the
tested θ range, the diagonal subtraction has nothing real to fit and is
liable to make things worse (over-modelling the source-plus-sky-residual
floor as a "stripe").

Workflow
--------
Run extraction across a directory of diagnostic PDFs:

    python scripts/diag_striping_score_audit.py \\
        --pdf-dir ~/Downloads/f356w_diag \\
        --csv /tmp/diag_audit.csv

Then iterate on a skip condition and stage candidate-skip PDFs into a
review directory:

    python scripts/diag_striping_score_audit.py \\
        --csv /tmp/diag_audit.csv \\
        --rel-range-max 0.01 \\
        --review-dir /tmp/skip_review --pdf-dir ~/Downloads/f356w_diag

The review dir is wiped and re-populated with symlinks each invocation,
so it's safe to re-run with different thresholds.

Metric definitions
------------------
Let ``s(θ) = -Var(M(θ))`` (so smaller / more negative = better fit).
Both s_min and s_max are negative in practice.

    abs_range  = |s_max - s_min|
    rel_range  = |s_max - s_min| / |s_min|
    edge_depth = (mean(s at far-from-optimum θ) - s_min) / |s_min|

``rel_range`` is the simplest — it asks "how big a fraction of the
optimum's depth is the angle-dependent spread?" If it's tiny (e.g.
< 1 %), nothing is θ-dependent → no real diagonal stripe to subtract.
``edge_depth`` is a sanity check that the minimum is a *peaked* feature
rather than a monotonic trend (which would suggest the search hit a
boundary).
"""
import argparse
import csv
import os
import re
import sys
from pathlib import Path

import pdfplumber


VIRIDIS_PURPLE = (0.267004, 0.004874, 0.329415)


def _is_purple(color):
    return (
        isinstance(color, tuple)
        and len(color) == 3
        and all(abs(color[i] - VIRIDIS_PURPLE[i]) < 0.01 for i in range(3))
    )


def _score_curve(page):
    """Return the (long, purple) Line2D as a list of (x_pdf, y_pdf) tuples."""
    for c in page.curves:
        pts = c.get('pts') or []
        if len(pts) >= 30 and _is_purple(c.get('stroking_color')):
            return list(pts)
    return None


def _axis_ticks(page):
    """Recover (x_tick_label_value, x_tick_pdf_x) and
    (y_tick_label_value, y_tick_pdf_y) lists from the top-panel ticks.

    The score panel is the topmost panel; its tick labels are the
    text closest to the data area. The y-tick labels are decimal
    numbers along the left edge; the x-tick labels are short integers
    just below the panel data area.

    The strategy is geometric, not semantic: cluster chars by
    horizontal stripe and pick the most likely tick rows.
    """
    chars = [c for c in page.chars if c['top'] < 250]

    # y-axis: decimal labels (e.g. "5.40", "-5.40"). Group chars on the
    # same row (same `top`, within ±1 pt) into label strings.
    from collections import defaultdict
    rows = defaultdict(list)
    for c in chars:
        rows[round(c['top'])].append(c)
    labels = []
    for top, row in rows.items():
        row.sort(key=lambda c: c['x0'])
        # Split into adjacent groups (gap > 2 pt → new label)
        groups = [[row[0]]]
        for c in row[1:]:
            if c['x0'] - groups[-1][-1]['x1'] > 2.0:
                groups.append([c])
            else:
                groups[-1].append(c)
        for g in groups:
            txt = ''.join(c['text'] for c in g).strip()
            x_center = 0.5 * (g[0]['x0'] + g[-1]['x1'])
            y_center = 0.5 * (g[0]['top'] + g[0]['bottom'])
            labels.append((top, x_center, y_center, txt))

    # Y-tick candidates: labels that match \d+\.\d+ (signed or not) at low x
    y_label_re = re.compile(r'^-?\d+\.\d+$')
    y_ticks = []
    for top, xc, yc, txt in labels:
        if y_label_re.match(txt) and xc < 80:
            try:
                y_ticks.append((float(txt), yc))
            except ValueError:
                continue

    # X-tick candidates: integer labels along a single horizontal row,
    # below the score data area (top > score_curve y_max).
    score = _score_curve(page)
    if score is None:
        return y_ticks, []
    score_ymax = max(p[1] for p in score)
    x_label_re = re.compile(r'^-?\d+$')
    # Find the row of integer labels just below score_ymax
    int_labels = [
        (top, xc, yc, txt) for top, xc, yc, txt in labels
        if x_label_re.match(txt) and yc > score_ymax
    ]
    if not int_labels:
        return y_ticks, []
    # Use the row with min top (closest to the axis)
    target_top = min(top for top, _, _, _ in int_labels)
    x_ticks = []
    for top, xc, yc, txt in int_labels:
        if abs(top - target_top) <= 1:
            try:
                x_ticks.append((float(txt), xc))
            except ValueError:
                continue
    x_ticks.sort(key=lambda p: p[1])
    return y_ticks, x_ticks


def _linear_transform(ticks):
    """From [(data, pdf), ...] return (a, b) such that data = a*pdf + b.

    Uses least-squares — robust to a missing tick or label-position
    rendering jitter.
    """
    if len(ticks) < 2:
        return None
    import numpy as np
    ticks = sorted(ticks, key=lambda t: t[1])
    pdf = np.array([t[1] for t in ticks])
    data = np.array([t[0] for t in ticks])
    A = np.vstack([pdf, np.ones_like(pdf)]).T
    (a, b), *_ = np.linalg.lstsq(A, data, rcond=None)
    return float(a), float(b)


def _extract_scale_offset(page):
    """Return the ``×10^k`` offset on the y-axis ('1e−5' in our plots)
    as a float multiplier.
    """
    # The offset text reads like "1e5", "1e6", "1e-5", etc. — pdfplumber
    # often drops the minus sign on a unicode-minus glyph. Detect any
    # short '1eN' or '1e-N' at the top-left of the y-axis label area.
    for c in page.chars:
        if c['top'] >= 50:
            continue
    rows = {}
    for c in page.chars:
        if c['top'] < 30 and c['x0'] < 100:
            rows.setdefault(round(c['top']), []).append(c)
    for top, row in rows.items():
        row.sort(key=lambda c: c['x0'])
        txt = ''.join(c['text'] for c in row).strip()
        m = re.match(r'^1e(-?)(\d+)$', txt)
        if m:
            sign = -1 if m.group(1) == '-' else 1
            exp = int(m.group(2))
            # All our diag_striping plots use the offset "1e-5" but
            # pdfplumber may strip the minus; trust 1e-N for N>=4.
            if sign == 1 and exp >= 4:
                sign = -1
            return 10.0 ** (sign * exp)
    return 1.0


def extract_score_data(pdf_path):
    """Return dict with keys ``thetas`` and ``scores`` (1D numpy arrays)
    plus axis metadata, or ``None`` if extraction failed.
    """
    import numpy as np

    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return None
        page = pdf.pages[0]
        score = _score_curve(page)
        if score is None:
            return None
        y_ticks, x_ticks = _axis_ticks(page)
        x_aff = _linear_transform(x_ticks)
        y_aff = _linear_transform(y_ticks)
        if x_aff is None or y_aff is None:
            return None
        y_scale = _extract_scale_offset(page)

        thetas = np.array([x_aff[0] * p[0] + x_aff[1] for p in score])
        # y axis is -Var(M(θ)), which is always negative. The PDF tick
        # labels include the unicode minus, but pdfplumber drops it on
        # extraction, so the regression treats them as positive
        # magnitudes — flip the sign here so the resulting scores agree
        # with the pipeline log convention (s_min ≈ -5.6e-5, etc.).
        score_vals = np.array([y_aff[0] * p[1] + y_aff[1] for p in score])
        score_vals = -score_vals * y_scale

    order = np.argsort(thetas)
    return {
        'thetas': thetas[order],
        'scores': score_vals[order],
        'y_scale': y_scale,
        'y_ticks': y_ticks,
        'x_ticks': x_ticks,
    }


def stripe_metrics(thetas, scores):
    """Per-exposure summary stats.

    Returns a dict with:
        s_min, s_max          (extracted -Var range, sign convention as-is)
        abs_range             |s_max - s_min|; absolute angle-dependent
                              spread of -Var(M). Clean (no-stripe)
                              exposures collapse near zero — the dominant
                              skip signal in practice (~50× spread across
                              the F356W audit set).
        rel_range             abs_range / |s_min|; nearly constant
                              (≈ 3–7 %) across exposures because |s_min|
                              scales with stripe amplitude. Reported but
                              not useful as a skip discriminator.
        edge_depth            (edge_mean - s_min) / |s_min|; how far the
                              optimum sits below the mean of the score at
                              the θ-range endpoints. Cleaner shape signal
                              than rel_range — flat curves give edge_depth
                              ≈ 0 even when |s_min| is small.
        opt_theta             argmin(scores)
        boundary_dist         min(opt_theta - theta_min, theta_max - opt_theta);
                              when the optimum sits inside the fine_window
                              of an endpoint the search hit a wall —
                              indicates no real interior minimum.
        n_points              number of sampled angles
    """
    import numpy as np
    s = np.asarray(scores)
    t = np.asarray(thetas)
    s_min = float(np.min(s))
    s_max = float(np.max(s))
    abs_range = abs(s_max - s_min)
    denom = abs(s_min) if s_min != 0 else 1.0
    rel_range = abs_range / denom
    # Edge mean: average of first and last 10% of the (θ-sorted) curve.
    n = len(s)
    k = max(int(0.1 * n), 1)
    edge_mean = float(0.5 * (np.mean(s[:k]) + np.mean(s[-k:])))
    edge_depth = abs(edge_mean - s_min) / denom
    opt_theta = float(t[int(np.argmin(s))])
    theta_min = float(t.min())
    theta_max = float(t.max())
    boundary_dist = float(min(opt_theta - theta_min, theta_max - opt_theta))
    return {
        's_min': s_min,
        's_max': s_max,
        'abs_range': abs_range,
        'rel_range': rel_range,
        'edge_depth': edge_depth,
        'opt_theta': opt_theta,
        'boundary_dist': boundary_dist,
        'n_points': n,
    }


def cmd_extract(args):
    pdf_dir = Path(args.pdf_dir).expanduser()
    pdfs = sorted(pdf_dir.glob('*_diag_striping*.pdf'))
    if not pdfs:
        sys.exit(f"no diag_striping PDFs in {pdf_dir}")
    rows = []
    failed = []
    for path in pdfs:
        rootname = path.stem.replace('_diag_striping', '').replace('_baseline', '').replace('_stripemask', '')
        try:
            data = extract_score_data(str(path))
        except Exception as e:
            failed.append((path.name, repr(e)))
            continue
        if data is None:
            failed.append((path.name, 'no score curve'))
            continue
        m = stripe_metrics(data['thetas'], data['scores'])
        rows.append({
            'pdf': path.name,
            'rootname': rootname,
            **m,
            'theta_min': float(data['thetas'].min()),
            'theta_max': float(data['thetas'].max()),
        })
    print(f"Extracted {len(rows)} / {len(pdfs)} PDFs"
          + (f" ({len(failed)} failed)" if failed else ""))
    if failed and args.verbose:
        for name, err in failed[:10]:
            print(f"  FAIL {name}: {err}")
        if len(failed) > 10:
            print(f"  ... + {len(failed) - 10} more")

    fieldnames = ['pdf', 'rootname', 'n_points', 'opt_theta',
                  'theta_min', 'theta_max', 'boundary_dist',
                  's_min', 's_max', 'abs_range', 'rel_range', 'edge_depth']
    with open(args.csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in fieldnames})
    print(f"wrote {args.csv}")


def cmd_review(args):
    if not args.csv or not args.review_dir or not args.pdf_dir:
        sys.exit("--csv, --review-dir, and --pdf-dir all required for review")
    pdf_dir = Path(args.pdf_dir).expanduser()
    review_dir = Path(args.review_dir).expanduser()

    # Wipe the review dir (only symlinks/files — never recurse into dirs).
    if review_dir.exists():
        for child in review_dir.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
    review_dir.mkdir(parents=True, exist_ok=True)

    with open(args.csv) as f:
        rows = list(csv.DictReader(f))

    def passes(row):
        return all([
            args.rel_range_max is None
            or float(row['rel_range']) <= args.rel_range_max,
            args.abs_range_max is None
            or float(row['abs_range']) <= args.abs_range_max,
            args.edge_depth_max is None
            or float(row['edge_depth']) <= args.edge_depth_max,
            args.boundary_dist_max is None
            or float(row['boundary_dist']) <= args.boundary_dist_max,
        ])

    skip_rows = [r for r in rows if passes(r)]
    # Optional exclusion: pretend we already reviewed every PDF whose
    # basename appears in any ``--exclude-dir``. This is the iterative-
    # review workflow: bump the threshold, re-stage, see only the new
    # additions. The previous review's dir is the natural exclusion set.
    excluded_names = set()
    for d in (args.exclude_dir or []):
        p = Path(d).expanduser()
        if p.is_dir():
            excluded_names.update(child.name for child in p.iterdir())
    if excluded_names:
        before = len(skip_rows)
        skip_rows = [r for r in skip_rows if r['pdf'] not in excluded_names]
        print(f"Excluded {before - len(skip_rows)} already-reviewed PDFs "
              f"(from {len(excluded_names)} entries across --exclude-dir).")
    print(f"Skip condition matches {len(skip_rows)} / {len(rows)} exposures "
          f"(after exclusion).")
    print(f"Thresholds: rel_range≤{args.rel_range_max}, "
          f"abs_range≤{args.abs_range_max}, "
          f"edge_depth≤{args.edge_depth_max}, "
          f"boundary_dist≤{args.boundary_dist_max}")
    print()
    print(f"{'rootname':<46} {'abs_range':>11} {'edge_depth':>11} "
          f"{'bdy':>6} {'opt_θ':>7}")
    for row in sorted(skip_rows, key=lambda r: float(r['abs_range'])):
        print(f"  {row['rootname']:<44} "
              f"{float(row['abs_range']):11.3e} "
              f"{float(row['edge_depth']):11.4f} "
              f"{float(row['boundary_dist']):6.2f} "
              f"{float(row['opt_theta']):7.2f}")
        # Symlink PDF into review dir
        src = pdf_dir / row['pdf']
        if src.exists():
            link = review_dir / row['pdf']
            try:
                link.symlink_to(src.resolve())
            except FileExistsError:
                pass
    print()
    print(f"PDFs symlinked into {review_dir} for browsing.")


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--pdf-dir', default='~/Downloads/f356w_diag')
    p.add_argument('--csv', default='/tmp/diag_audit.csv')
    p.add_argument('--review-dir', default=None,
                   help='If provided, symlink would-skip PDFs here for '
                        'browsing. Triggers review mode (no re-extraction).')
    p.add_argument('--rel-range-max', type=float, default=None,
                   help='Skip threshold on (s_max - s_min) / |s_min|.')
    p.add_argument('--abs-range-max', type=float, default=None,
                   help='Skip threshold on |s_max - s_min| (raw).')
    p.add_argument('--edge-depth-max', type=float, default=None,
                   help='Skip threshold on (edge_mean - s_min) / |s_min|.')
    p.add_argument('--boundary-dist-max', type=float, default=None,
                   help='Skip threshold on distance of opt_theta from the '
                        'nearest θ search-range boundary (in degrees). Use '
                        'something like 0.3 to flag exposures whose argmin '
                        'walked to the edge — no real interior minimum.')
    p.add_argument('--exclude-dir', action='append', default=None,
                   help='Directory of already-reviewed PDFs to exclude from '
                        'the new staging. Repeatable. The iterative-review '
                        'workflow: bump thresholds, point --exclude-dir at '
                        'the previous --review-dir, see only the new adds.')
    p.add_argument('--verbose', action='store_true')
    return p.parse_args()


def main():
    args = parse_args()
    if args.review_dir is not None:
        cmd_review(args)
    else:
        cmd_extract(args)


if __name__ == '__main__':
    main()
