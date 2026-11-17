"""The rule for which exposures a visit is compared with in the outlier pass
(imaging/outlier_visits.py), one function for NIRCam and the HST instruments.
"""

from campfire_pipeline.imaging.outlier_visits import overlap_inputs


def _box(x0, x1, y0):
    """An S_REGION string for a unit-high box from x0 to x1, starting at y0.

    The boxes are staggered in y on purpose. NIRCam's footprint test
    (``_polygon_overlap``) calls two footprints overlapping when a corner of
    one lies inside the other; boxes that share an edge line have no corner
    strictly inside, and neither do two squares on one center rolled 45
    degrees apart.
    """
    return (f'POLYGON ICRS {x0} {y0} {x1} {y0} {x1} {y0 + 1.0} {x0} {y0 + 1.0}')


# five exposures along a line: two of the visit, one of another visit of the
# same program that overlaps the second, one of another program that overlaps
# the first, and one far away
FILES = ['jw01111001001_a.fits', 'jw01111001001_b.fits', 'jw01111002001_c.fits',
         'jw02222001001_d.fits', 'jw01111003001_e.fits']
SPANS = {'jw01111001001_a.fits': (0.0, 2.0), 'jw01111001001_b.fits': (1.5, 3.5),
         'jw01111002001_c.fits': (3.0, 5.0), 'jw02222001001_d.fits': (-1.5, 0.5),
         'jw01111003001_e.fits': (20.0, 22.0)}


def _overlaps(a, b):
    return SPANS[a][0] < SPANS[b][1] and SPANS[b][0] < SPANS[a][1]


def _program(path):
    return path[2:7]


def test_the_same_program_by_default_and_every_program_on_request():
    visit = FILES[:2]
    assert overlap_inputs(visit, FILES, _overlaps, _program, False) == [FILES[2]]
    # met in the order of the visit's exposures: d overlaps a, c overlaps b
    assert overlap_inputs(visit, FILES, _overlaps, _program, True) == [
        FILES[3], FILES[2]]
    # an exposure that overlaps two of the visit's is taken once
    wide = dict(SPANS)
    wide['jw01111002001_c.fits'] = (1.0, 5.0)
    got = overlap_inputs(
        visit, FILES,
        lambda a, b: wide[a][0] < wide[b][1] and wide[b][0] < wide[a][1],
        _program, False)
    assert got == [FILES[2]]
    # nothing overlaps the far one
    assert overlap_inputs([FILES[4]], FILES, _overlaps, _program, True) == []


def test_nircam_and_hst_go_through_the_one_rule():
    """Each instrument's selection is this function with its own footprint
    test and its own way of reading a program."""
    from shapely.geometry import box

    from campfire_pipeline.hst import orchestrate
    from campfire_pipeline.nircam.steps import outlier as nircam_outlier

    sregions = [_box(*SPANS[f], 0.1 * k) for k, f in enumerate(FILES)]
    visit = FILES[:2]
    # NIRCam returns the visit's files followed by the others
    assert nircam_outlier._compute_overlap_inputs(
        visit, FILES, sregions, False) == visit + [FILES[2]]
    assert nircam_outlier._compute_overlap_inputs(
        visit, FILES, sregions, True) == visit + [FILES[3], FILES[2]]

    hst = ['ites01a0q_flt.fits', 'ites01a1q_flt.fits', 'ites02a0q_flt.fits',
           'ioth01a0q_flt.fits', 'ites03a0q_flt.fits']
    polygons = {h: box(SPANS[f][0], 0.1 * k, SPANS[f][1], 1.0 + 0.1 * k)
                for k, (h, f) in enumerate(zip(hst, FILES))}
    assert orchestrate.overlap_inputs(hst[:2], hst, polygons, False) == [hst[2]]
    assert orchestrate.overlap_inputs(hst[:2], hst, polygons, True) == [
        hst[3], hst[2]]
