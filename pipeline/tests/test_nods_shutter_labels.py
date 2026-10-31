"""Shutter labels of the nods plots: physical column, the fit's amplitude and call, and
the stuck list's origin."""
from campfire_pipeline.nirspec import plots as P
from campfire_pipeline.nirspec.stuck_list import Entry

PHYS = (2, 195, [64, 65, 66])          # quadrant, row, the slitlet's columns (ordinal k -> column k-1)


def test_label_shows_column_amplitude_and_fit_call():
    calls = {(2, 195, 64): dict(stuck=True, a=-0.02, n=6, note=''),
             (2, 195, 65): dict(stuck=False, a=1.09, n=6, note='')}
    assert P._shutter_label(1, PHYS, {}, calls) == ('c64 -0.02 *fit', 'red', 'bold')
    assert P._shutter_label(2, PHYS, {}, calls) == ('c65 1.09', '0.5', 'normal')
    assert P._shutter_label(3, PHYS, {}, calls) == ('c66', '0.5', 'normal')


def test_label_shows_list_origin_and_veto():
    entries = {(2, 195, 64): Entry(origin='manual', stuck=True), (2, 195, 65): Entry(origin='web', stuck=False)}
    calls = {(2, 195, 65): dict(stuck=True, a=0.1, n=6, note='')}
    assert P._shutter_label(1, PHYS, entries, {}) == ('c64 *[m]', 'red', 'bold')
    # a web veto of a fit call: shown as open, not red
    assert P._shutter_label(2, PHYS, entries, calls) == ('c65 0.10 open[w]', '0.5', 'normal')


def test_label_marks_review_calls_and_falls_back_to_ordinals():
    calls = {(2, 195, 66): dict(stuck=False, a=0.65, n=6, note='review: partly open or unmodelled light')}
    assert P._shutter_label(3, PHYS, {}, calls) == ('c66 0.65', 'darkorange', 'normal')
    assert P._shutter_label(2, None, {}, {}) == ('2', '0.5', 'normal')
