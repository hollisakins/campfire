"""Stage 1's slit mask for the 1/f fit: the wavelength range its assign_wcs takes per
disperser (``stage1.mask_wavelength_range``)."""
import pytest

from campfire_pipeline.nirspec.stage1 import mask_wavelength_range

DEFAULT = {'G140M': [0.70, 2.5], 'G235M': [1.66, 4.0], 'G395M': [2.87, 5.5], 'G140H': [0.70, 2.5]}
CRDS = (9.7e-7, 1.89e-6)


@pytest.mark.parametrize('grating, filt', [('G140M', 'F100LP'), ('G235M', 'F170LP'), ('G140H', 'F100LP'),
                                           ('G235H', 'F170LP')])
def test_traces_past_the_calibrated_range_are_masked_to_5p3_um_without_the_extension(grating, filt):
    """No extension setting anywhere: the red edge is 5.3 um, the blue edge the configured
    one (the CRDS one when nothing is configured)."""
    blue, red = mask_wavelength_range(grating, filt, DEFAULT, CRDS)
    assert red == pytest.approx(5.3e-6)
    assert blue == pytest.approx(DEFAULT[grating][0] * 1e-6 if grating in DEFAULT else CRDS[0])


def test_other_dispersers_keep_the_configured_or_crds_range():
    assert mask_wavelength_range('G395M', 'F290LP', DEFAULT, CRDS) == pytest.approx([2.87e-6, 5.5e-6])
    assert mask_wavelength_range('G140M', 'F070LP', DEFAULT, CRDS) == pytest.approx([0.70e-6, 2.5e-6])
    assert mask_wavelength_range('PRISM', 'CLEAR', DEFAULT, CRDS) is None
    # a configured red edge beyond 5.3 um is kept
    assert mask_wavelength_range('G235M', 'F170LP', {'G235M': [1.66, 5.5]}, CRDS) == pytest.approx([1.66e-6, 5.5e-6])
