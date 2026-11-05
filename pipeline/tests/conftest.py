import pytest


@pytest.fixture(autouse=True)
def _optics_standin_unless_asked(request, monkeypatch, tmp_path):
    """The diffraction model (``nirspec.optics``) builds tables of about a minute each on first
    use. Tests run a closed-form stand-in with its interface (``optics_standin.Analytic``) unless
    they ask for the shipped model (the ``optics_model`` fixture), and any table built goes under
    the test's tmp_path."""
    from campfire_pipeline.nirspec import optics
    monkeypatch.setattr(optics, 'default_cache_dir', lambda: str(tmp_path / 'optics'))
    if 'optics_model' not in request.fixturenames:
        from optics_standin import STANDIN
        monkeypatch.setattr(optics, 'optics_for', lambda detector: STANDIN)


@pytest.fixture
def optics_model():
    """Marks a test that runs the shipped diffraction model (``optics.optics_for``)."""
    return True
