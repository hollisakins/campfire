"""P4: the CFEXPGRP exp_group card round-trips and survives an append_extras re-save.

Guards the design §4.2/§8 contract that the pipeline-stamped exp_group is what
deploy reads. Uses astropy directly (no jwst), guarded by importorskip so it runs
in CI and is skipped where campfire_pipeline isn't installed.
"""
import pytest

pytest.importorskip("campfire_pipeline")
import numpy as np  # noqa: E402
from astropy.io import fits  # noqa: E402

from campfire_pipeline.nirspec import canonical as C  # noqa: E402


def _minimal_canonical(path):
    fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(np.zeros((4, 4), dtype="float32"), name="SCI"),
    ]).writeto(path, overwrite=True)


def test_exp_group_card_shape():
    card = C.exp_group_card(np.int64(7))
    assert card == {C.EXP_GROUP_KEYWORD: (7, C.EXP_GROUP_COMMENT)}
    assert isinstance(card[C.EXP_GROUP_KEYWORD][0], int)  # numpy int coerced


def test_exp_group_round_trips_and_survives_append_extras(tmp_path):
    p = str(tmp_path / "jw07076020001_04101_00001_nrs1_117757.fits")
    _minimal_canonical(p)

    C.append_extras(p, header_updates=C.exp_group_card(5))
    assert C.read_exp_group(p) == 5

    # a subsequent append_extras (e.g. stage2b re-stamp / an S2D append) preserves it
    C.append_extras(p, header_updates={"OTHERKEY": (1, "unrelated")})
    assert C.read_exp_group(p) == 5


def test_read_exp_group_absent_is_none(tmp_path):
    p = str(tmp_path / "no_stamp.fits")
    _minimal_canonical(p)
    assert C.read_exp_group(p) is None
