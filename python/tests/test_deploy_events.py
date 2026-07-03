"""Unit tests for the Phase 3 deploy ledger (admin audit 2026-07-03, Theme B).

Pure/local: the normalized deploy_events metadata envelope, the field→p_field
forwarding through log_deploy_event, best-effort provenance header reads, and
the new delete events emitted by remove/prune. Full SQL (the field column,
backfill, log_deploy_event arity) is exercised on the Supabase preview branch.
"""
from unittest.mock import MagicMock

import numpy as np
import pytest
from astropy.io import fits

from campfire.deploy import nircam as nc
from campfire.deploy import deploy as dep
from campfire.deploy.supabase import deploy_event_metadata, log_deploy_event


# --- deploy_event_metadata (the envelope) -----------------------------------

def test_envelope_shape_and_partial_derivation():
    m = deploy_event_metadata(
        "nircam", field="cosmos", filters=["f444w"],
        planned=10, succeeded=8, failed=2, skipped=1, draft=True)
    assert m["instrument"] == "nircam"
    assert m["scope"] == {"field": "cosmos", "filters": ["f444w"]}
    assert m["counts"] == {"planned": 10, "succeeded": 8, "failed": 2, "skipped": 1}
    assert m["flags"] == {"draft": True, "partial": True}


def test_envelope_partial_false_and_absent_counts_omitted():
    m = deploy_event_metadata("nirspec", observation="obs1", succeeded=5)
    assert m["scope"] == {"observation": "obs1"}
    assert m["counts"] == {"succeeded": 5}          # planned/failed/etc absent, not 0
    assert m["flags"]["partial"] is False


def test_envelope_supabase_only_and_extra_keys():
    m = deploy_event_metadata(
        "nirspec", observation="o", supabase_only=True, spectra=3, r2_objects_deleted=0)
    assert m["flags"]["supabase_only"] is True
    assert m["spectra"] == 3 and m["r2_objects_deleted"] == 0   # extra folds through


# --- log_deploy_event forwards field → p_field ------------------------------

def test_log_deploy_event_forwards_field():
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = "evt-id"
    log_deploy_event(client, action="upload", field="cosmos", observation=None)
    args, _ = client.rpc.call_args
    assert args[0] == "log_deploy_event"
    assert args[1]["p_field"] == "cosmos"
    assert args[1]["p_action"] == "upload"


def test_log_deploy_event_best_effort_swallows_errors():
    client = MagicMock()
    client.rpc.side_effect = RuntimeError("boom")
    # Audit is best-effort; a failure must never raise into the deploy path.
    assert log_deploy_event(client, action="delete") is None


# --- provenance header reads (best-effort) ----------------------------------

def test_read_field_provenance_none_when_no_mosaic_or_exposure(monkeypatch):
    monkeypatch.setattr(nc, "discover_mosaics", lambda *a, **k: [])
    monkeypatch.setattr(nc, "discover_exposures", lambda *a, **k: {})
    assert nc._read_field_provenance({}, "cosmos", ["f444w"]) == (None, None, None)


def test_read_field_provenance_falls_back_to_exposure(tmp_path, monkeypatch):
    # No mosaic yet (mid-reduction --draft): read the stamped exposure header.
    epath = tmp_path / "jw123_nrcalong.fits"
    hdu = fits.PrimaryHDU(data=np.zeros((2, 2), dtype="float32"))
    hdu.header["CMPFRVER"] = "2.0.0"
    hdu.header["CAL_VER"] = "1.14.0"
    fits.HDUList([hdu]).writeto(epath)
    monkeypatch.setattr(nc, "discover_mosaics", lambda *a, **k: [])
    monkeypatch.setattr(
        nc, "discover_exposures",
        lambda *a, **k: {("f444w", "jw123_nrcalong"): {"path": str(epath)}})
    assert nc._read_field_provenance({}, "cosmos", ["f444w"]) == ("2.0.0", "1.14.0", None)


def test_read_field_provenance_reads_mosaic_cards(tmp_path, monkeypatch):
    mpath = tmp_path / "mosaic_cosmos_f444w_i2d.fits"
    hdu = fits.PrimaryHDU()
    hdu.header["CMPFRVER"] = "1.2.3"
    hdu.header["CAL_VER"] = "1.14.0"
    hdu.header["CRDS_CTX"] = "jwst_1234.pmap"
    fits.HDUList([hdu]).writeto(mpath)
    monkeypatch.setattr(
        nc, "discover_mosaics",
        lambda *a, **k: [{"extension": "i2d", "path": str(mpath)}])
    assert nc._read_field_provenance({}, "cosmos", ["f444w"]) == (
        "1.2.3", "1.14.0", "jwst_1234.pmap")


def test_read_exposure_provenance_none_when_empty():
    assert dep._read_exposure_provenance([]) == (None, None, None)


def test_read_exposure_provenance_reads_first(tmp_path):
    p = tmp_path / "exp.fits"
    hdu = fits.PrimaryHDU(data=np.zeros((2, 2), dtype="float32"))
    hdu.header["CMPFRVER"] = "9.9.9"
    fits.HDUList([hdu]).writeto(p)
    cf, jw, crds = dep._read_exposure_provenance([str(p)])
    assert cf == "9.9.9" and jw is None and crds is None


# --- silent-mutation delete events ------------------------------------------

def test_remove_observation_emits_delete_event(monkeypatch):
    from campfire.deploy import remove as rm

    sb = MagicMock()
    monkeypatch.setattr(rm, "get_supabase_client", lambda cfg: sb)
    monkeypatch.setattr(rm, "get_user_id_from_token", lambda cfg: "user-1")
    monkeypatch.setattr(rm, "_fetch_observation",
                        lambda sb, o: {"field": "cosmos", "latest_deployment_id": None})
    monkeypatch.setattr(rm, "_fetch_targets", lambda sb, o: [{"target_id": "t1", "id": 1}])
    monkeypatch.setattr(rm, "_count_spectra", lambda sb, ids: 3)
    monkeypatch.setattr(rm, "_count_by_obs", lambda sb, t, o: 0)
    monkeypatch.setattr(rm, "_count_comments", lambda sb, ids: 0)
    monkeypatch.setattr(rm, "_is_inspected", lambda t: False)
    monkeypatch.setattr(rm, "_delete_db_rows", lambda *a, **k: None)
    monkeypatch.setattr(rm, "refresh_filter_options", lambda sb: None)
    monkeypatch.setattr(rm, "refresh_programs_overview", lambda sb: None)
    events = []
    monkeypatch.setattr(rm, "log_deploy_event",
                        lambda sb, **kw: events.append(kw))

    rm.remove_observation("obs1", {}, supabase_only=True, auto_approve=True,
                          skip_rebuild=True)

    assert len(events) == 1
    ev = events[0]
    assert ev["action"] == "delete"
    assert ev["observation"] == "obs1"
    assert ev["metadata"]["scope"]["observation"] == "obs1"
    assert ev["metadata"]["spectra"] == 3
    assert ev["metadata"]["flags"]["supabase_only"] is True
