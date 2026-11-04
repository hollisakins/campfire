import pytest


@pytest.fixture(autouse=True)
def _psf_tables_unless_optics(request, monkeypatch, tmp_path):
    """The diffraction model (``nirspec.optics``, FIT_VERSION 6 for PRISM) builds tables of about a
    minute each on first use. Tests run the PSF and shutter tables unless they ask for the model
    (the ``optics_model`` fixture), and any table built goes under the test's tmp_path."""
    from campfire_pipeline.nirspec import optics
    monkeypatch.setattr(optics, 'default_cache_dir', lambda: str(tmp_path / 'optics'))
    if 'optics_model' not in request.fixturenames:
        monkeypatch.setattr(optics, 'optics_for', lambda grating, detector: None)


@pytest.fixture
def optics_model():
    """Marks a test that runs the diffraction model (``optics.optics_for`` as shipped)."""
    return True
