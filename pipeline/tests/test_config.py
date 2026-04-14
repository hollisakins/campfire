"""Tests for config path resolution and environment setup."""

import os
from pathlib import Path
from unittest import mock

from campfire_pipeline.config import (
    _get_campfire_root,
    _resolve_path,
    setup_environment,
)


class TestGetCampfireRoot:
    def test_returns_env_var_when_set(self):
        with mock.patch.dict(os.environ, {"CAMPFIRE_ROOT": "/custom/root"}):
            assert _get_campfire_root() == "/custom/root"

    def test_defaults_to_home_campfire_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert _get_campfire_root() == str(Path.home() / "campfire")


class TestResolvePath:
    def test_config_value_takes_priority(self):
        result = _resolve_path("/explicit/path", "/root", "raw")
        assert result == "/explicit/path"

    def test_campfire_root_used_when_no_config(self):
        result = _resolve_path(None, "/root", "raw")
        assert result == "/root/raw"


class TestSetupEnvironmentCrdsPath:
    def test_respects_existing_crds_path(self):
        config = {"environment": {"CRDS_SERVER_URL": "https://example.com"}}
        with mock.patch.dict(os.environ, {"CRDS_PATH": "/user/crds"}, clear=False):
            setup_environment(config)
            assert os.environ["CRDS_PATH"] == "/user/crds"

    def test_env_crds_path_beats_config(self):
        config = {"environment": {"CRDS_PATH": "/config/crds"}}
        with mock.patch.dict(os.environ, {"CRDS_PATH": "/user/crds"}, clear=False):
            setup_environment(config)
            assert os.environ["CRDS_PATH"] == "/user/crds"

    def test_config_crds_path_overrides_default(self):
        config = {"environment": {"CRDS_PATH": "/config/crds"}}
        with mock.patch.dict(os.environ, {}, clear=True):
            setup_environment(config)
            assert os.environ["CRDS_PATH"] == "/config/crds"

    def test_falls_back_to_campfire_root_cache(self):
        config = {"environment": {}}
        env = {"CAMPFIRE_ROOT": "/my/root"}
        with mock.patch.dict(os.environ, env, clear=True):
            setup_environment(config)
            assert os.environ["CRDS_PATH"] == "/my/root/cache/crds"

    def test_falls_back_to_default_root_cache(self):
        config = {"environment": {}}
        with mock.patch.dict(os.environ, {}, clear=True):
            setup_environment(config)
            expected = os.path.join(str(Path.home() / "campfire"), "cache", "crds")
            assert os.environ["CRDS_PATH"] == expected
