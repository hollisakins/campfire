"""NIRCam reviewer masks and triage status on the config plane (issue #709).

Covers `campfire.deploy.nircam_review`: the entry model, the `.reg` reader, and pull /
push / diff end to end against the in-memory Supabase fake with a tmp
``$CAMPFIRE_ROOT``. ``nircam_review_apply`` (the guarded write) is imitated here by
``_apply_handler``; the real function is guarded by
``supabase/tests/check_nircam_review_apply.sql``.
"""
import copy
import json

import pytest
from click.testing import CliRunner

from campfire.deploy import config_cli
from campfire.deploy import nircam_review as nr
from campfire.deploy.config_cli import _resolve_masks, _resolve_scope, config_group
from tests.fakes import FakePostgrest

pytest.importorskip("regions")

FIELD = "cosmos"
F = "f444w"
A = "jw01727001001_02101_00001_nrcalong"
B = "jw01727001001_02101_00002_nrcalong"
C = "jw01727001001_02101_00003_nrcalong"
TRI = [[10.0, 10.0], [50.0, 10.0], [50.0, 40.0]]
SQUARE = [[100.0, 100.0], [140.0, 100.0], [140.0, 140.0], [100.0, 140.0]]


# ---------------------------------------------------------------------------
# Fixtures: a fake cloud implementing the guarded write, and a tmp tree
# ---------------------------------------------------------------------------

def _apply_handler(client):
    """nircam_review_apply in Python: apply a change only while the column still
    holds `expect`; report the rest under `refused` / `missing`."""
    def handler(params):
        field = params["p_field"]
        applied = unchanged = 0
        refused, missing = [], []
        for c in params["p_changes"]:
            row = next((r for r in client.rows(nr.TABLE) if r["field"] == field
                        and r["filter"] == c["filter"] and r["filename"] == c["filename"]), None)
            ident = {"channel": c["channel"], "filter": c["filter"], "filename": c["filename"]}
            if row is None:
                missing.append(ident)
                continue
            col = "mask_regions" if c["channel"] == "mask" else "review_status"
            cur = row.get(col)
            new = (c.get("set") or {}).get("value")
            if cur == new:
                unchanged += 1
                continue
            if cur != (c.get("expect") or {}).get("value"):
                refused.append({**ident, "current": cur})
                continue
            row[col] = copy.deepcopy(new)
            applied += 1
        return {"applied": applied, "unchanged": unchanged, "refused": refused, "missing": missing}
    return handler


def _exp(i, name, mask=None, status="pending", filt=F, field=FIELD):
    return {"id": i, "field": field, "filter": filt, "filename": name, "detector": "nrcalong",
            "mask_regions": mask, "review_status": status}


def _payload(*polys, label=None):
    return {"version": 1, "polygons": [
        {"id": f"p{i}", "vertices": v, "source": "web", **({"label": label} if label else {})}
        for i, v in enumerate(polys)]}


def _cloud(rows):
    client = FakePostgrest(tables={nr.TABLE: [dict(r) for r in rows]})
    client.rpc_handlers["nircam_review_apply"] = _apply_handler(client)
    return client


def _row(client, name):
    return next(r for r in client.rows(nr.TABLE) if r["filename"] == name)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    return tmp_path


def _reg(root, name, *polys, filt=F, extra=""):
    path = nr.masks_dir(FIELD, root) / filt / f"{name}.reg"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join("polygon(" + ",".join(f"{x},{y}" for x, y in p) + ")" for p in polys)
    path.write_text(f"# Region file format: DS9 version 4.1\nimage\n{body}\n{extra}")
    return path


def _exposures(root, excluded=None, approved=None):
    path = nr.ref_dir(FIELD, root) / nr.EXPOSURES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "excluded": excluded or {}, "approved": approved or {}}))
    return path


def _pull(client, state, root, **kw):
    lines = []
    counts = nr.sync_pull(client, [FIELD], state, root=root, out=lines.append, **kw)
    return counts, "\n".join(lines)


def _push(client, state, root, **kw):
    lines = []
    counts = nr.sync_push(client, [FIELD], state, root=root, out=lines.append, **kw)
    return counts, "\n".join(lines)


def _local(root):
    return nr.read_local(FIELD, root)


# ---------------------------------------------------------------------------
# The entry model and the .reg reader
# ---------------------------------------------------------------------------

def test_mask_entry_normalises_what_a_reg_file_holds():
    entry = nr.mask_entry([{"vertices": [[1.000004, 2], [3, -0.00001], [5, 6]], "label": ""},
                           {"vertices": [[1, 1], [2, 2]]}])        # 2 vertices: dropped
    assert entry == [{"vertices": [[1.0, 2.0], [3.0, 0.0], [5.0, 6.0]], "label": None}]
    assert nr.mask_entry([]) is None
    with pytest.raises(nr.ReviewError):
        nr.mask_entry([{"vertices": [[1, "x"], [2, 2], [3, 3]]}])


def test_written_reg_reads_back_as_the_same_entry(tree):
    entry = nr.mask_entry([{"vertices": TRI, "label": "wisp"},
                           {"vertices": [[1.23456, 7.5], [9, 7.5], [9, 20]]}])
    path = nr.write_mask(FIELD, (F, A), entry, tree)
    assert nr.read_reg(path) == entry


def test_reg_the_portal_cannot_hold_is_an_error(tree):
    with pytest.raises(nr.ReviewError, match="Circle"):
        nr.read_reg(_reg(tree, A, TRI, extra="circle(5,5,3)\n"))
    with pytest.raises(nr.ReviewError, match="exclude"):
        nr.read_reg(_reg(tree, B, extra="-polygon(1,1,5,1,5,5)\n"))


def test_exposures_json_v1_reads_and_rejects_a_double_listing(tree):
    _exposures(tree, excluded={F: [A]})
    assert _local(tree).status == {(F, A): "excluded"}
    _exposures(tree, excluded={F: [A]}, approved={F: [A]})
    with pytest.raises(nr.ReviewError, match="both"):
        _local(tree)


def test_resolve_masks_matrix():
    none = (False, False, False)
    # nothing selected: everything, masks of every field included
    assert _resolve_masks(*none, (), ()) is None
    # --field X: its fields.toml section and its masks
    assert _resolve_scope(*none, (), ("cosmos",)) == ({"fields": ["cosmos"]}, False)
    assert _resolve_masks(*none, (), ("cosmos",)) == ["cosmos"]
    # --field X --masks: only the masks; --field X --fields: only the section
    assert _resolve_scope(*none, (), ("cosmos",), kind_masks=True) == ({}, False)
    assert _resolve_masks(*none, (), ("cosmos",), kind_masks=True) == ["cosmos"]
    assert _resolve_masks(False, False, True, (), ("cosmos",)) is config_cli._NO_FLAGS
    # --masks alone: every field's masks and nothing else
    assert _resolve_scope(*none, (), (), kind_masks=True) == ({}, False)
    assert _resolve_masks(*none, (), (), kind_masks=True) is None
    # another kind alone: no masks
    assert _resolve_masks(*none, ("a",), ()) is config_cli._NO_FLAGS
    assert _resolve_masks(*none, (), (), kind_flags=True) is config_cli._NO_FLAGS


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def test_first_pull_writes_masks_and_full_triage_status(tree):
    client = _cloud([_exp(1, A, mask=_payload(TRI, label="spike")),
                     _exp(2, B, status="excluded"), _exp(3, C, status="approved")])
    state = {}
    counts, _ = _pull(client, state, tree)
    assert counts["applied"] == 3
    local = _local(tree)
    assert local.mask == {(F, A): [{"vertices": TRI, "label": "spike"}]}
    assert local.status == {(F, B): "excluded", (F, C): "approved"}
    doc = json.loads((nr.ref_dir(FIELD, tree) / nr.EXPOSURES_FILE).read_text())
    assert doc["version"] == 2 and doc["excluded"] == {F: [B]} and doc["approved"] == {F: [C]}
    # the second pull has nothing to do
    assert _pull(client, state, tree)[0] == {"applied": 0, "kept": 0, "fields": 1, "errors": 0}


def test_portal_edit_to_a_pulled_mask_reaches_the_reduction(tree):
    # #709: a portal edit to an existing mask used to be reported and never written.
    client = _cloud([_exp(1, A, mask=_payload(TRI))])
    state = {}
    _pull(client, state, tree)
    _row(client, A)["mask_regions"] = _payload(TRI, SQUARE)
    counts, _ = _pull(client, state, tree)
    assert counts["applied"] == 1
    assert len(_local(tree).mask[(F, A)]) == 2


def test_conflict_is_kept_local_unless_theirs(tree):
    client = _cloud([_exp(1, A, mask=_payload(TRI))])
    state = {}
    _pull(client, state, tree)
    _reg(tree, A, SQUARE)                                    # edited here
    _row(client, A)["mask_regions"] = _payload(TRI, SQUARE)  # and in the portal
    counts, text = _pull(client, state, tree)
    assert counts["kept"] == 1 and "changed here AND in the cloud" in text
    assert _local(tree).mask[(F, A)] == [{"vertices": SQUARE, "label": None}]
    _pull(client, state, tree, theirs=True)
    assert len(_local(tree).mask[(F, A)]) == 2


def test_portal_unexclude_asks_and_prune_applies_it(tree):
    # #709: un-excluding in the portal used to never propagate.
    client = _cloud([_exp(1, A, status="excluded")])
    state = {}
    _pull(client, state, tree)
    _row(client, A)["review_status"] = "pending"
    counts, text = _pull(client, state, tree)
    assert counts["kept"] == 1 and "not applied (--prune applies them)" in text
    assert _local(tree).status == {(F, A): "excluded"}
    _pull(client, state, tree, prune=True)
    assert _local(tree).status == {}


def test_portal_mask_removal_moves_the_reg_aside_on_prune(tree):
    client = _cloud([_exp(1, A, mask=_payload(TRI))])
    state = {}
    _pull(client, state, tree)
    _row(client, A)["mask_regions"] = None
    _pull(client, state, tree)
    assert (F, A) in _local(tree).mask
    _pull(client, state, tree, prune=True)
    reg = nr.masks_dir(FIELD, tree) / F / f"{A}.reg"
    assert not reg.exists() and reg.with_name(reg.name + ".prev").exists()


def test_pull_restores_an_entry_the_file_lost(tree):
    client = _cloud([_exp(1, A, status="excluded"), _exp(2, B, mask=_payload(TRI))])
    state = {}
    _pull(client, state, tree)
    _exposures(tree)                                                   # exclusion lost
    (nr.masks_dir(FIELD, tree) / F / f"{B}.reg").unlink()              # mask lost
    counts, text = _pull(client, state, tree)
    assert counts["applied"] == 2 and "restored" in text
    local = _local(tree)
    assert local.status == {(F, A): "excluded"} and (F, B) in local.mask


def test_unreadable_reg_is_reported_and_never_overwritten(tree):
    client = _cloud([_exp(1, A, mask=_payload(SQUARE))])
    path = _reg(tree, A, TRI, extra="circle(5,5,3)\n")
    before = path.read_text()
    state = {}
    _, text = _pull(client, state, tree)
    assert "Circle" in text and "skipped" in text
    assert path.read_text() == before
    counts, _ = _push(client, state, tree, prune=True)
    assert counts["pushed"] == 0
    assert _row(client, A)["mask_regions"] == _payload(SQUARE)


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def test_push_sends_a_ds9_mask_and_an_exclusion(tree):
    # #709: masks drawn locally (DS9) had no way up.
    client = _cloud([_exp(1, A), _exp(2, B)])
    _reg(tree, A, TRI)
    _exposures(tree, excluded={F: [B]})
    state = {}
    counts, _ = _push(client, state, tree)
    assert counts == {"pushed": 2, "pending": 0, "fields": 1, "errors": 0}
    mask = _row(client, A)["mask_regions"]
    assert mask["version"] == 1 and mask["polygons"][0]["vertices"] == TRI
    assert mask["polygons"][0]["source"] == "imported"
    assert mask["polygons"][0]["imported_from"] == f"{A}.reg"
    assert _row(client, B)["review_status"] == "excluded"
    assert _push(client, state, tree)[0]["pushed"] == 0          # in sync now


def test_push_keeps_the_stored_record_of_unchanged_polygons(tree):
    client = _cloud([_exp(1, A, mask=_payload(TRI, label="spike"))])
    state = {}
    _pull(client, state, tree)
    path = nr.masks_dir(FIELD, tree) / F / f"{A}.reg"
    path.write_text(path.read_text() + "polygon(" + ",".join(f"{x},{y}" for x, y in SQUARE) + ")\n")
    _push(client, state, tree)
    polys = _row(client, A)["mask_regions"]["polygons"]
    assert polys[0] == {"id": "p0", "vertices": TRI, "source": "web", "label": "spike"}
    assert polys[1]["vertices"] == SQUARE and polys[1]["source"] == "imported"


def test_local_removal_needs_a_yes_and_conflicts_need_force(tree):
    client = _cloud([_exp(1, A, mask=_payload(TRI)), _exp(2, B, status="excluded")])
    state = {}
    _pull(client, state, tree)
    (nr.masks_dir(FIELD, tree) / F / f"{A}.reg").unlink()
    _exposures(tree)                       # un-excluded here
    counts, text = _push(client, state, tree)
    assert counts["pushed"] == 0 and counts["pending"] == 2 and "not removed" in text
    assert _row(client, A)["mask_regions"] is not None
    counts, _ = _push(client, state, tree, prune=True)
    assert counts["pushed"] == 2
    assert _row(client, A)["mask_regions"] is None
    assert _row(client, B)["review_status"] == "pending"


def test_conflict_is_refused_without_force(tree):
    client = _cloud([_exp(1, A, status="excluded")])
    state = {}
    _pull(client, state, tree)
    _exposures(tree, approved={F: [A]})
    _row(client, A)["review_status"] = "pending"
    counts, text = _push(client, state, tree)
    assert counts["pushed"] == 0 and "conflict(s) not sent" in text
    _push(client, state, tree, force=True)
    assert _row(client, A)["review_status"] == "approved"


def test_portal_edit_between_read_and_write_is_refused(tree):
    client = _cloud([_exp(1, A, mask=_payload(TRI))])
    state = {}
    _pull(client, state, tree)
    _reg(tree, A, SQUARE)
    rows = copy.deepcopy(nr.fetch_cloud_rows(client, [FIELD])[FIELD])   # what the push read
    _row(client, A)["mask_regions"] = _payload(TRI, TRI)               # portal edit meanwhile
    lines = []
    counts = nr.push_field(FIELD, rows, state, client, out=lines.append, root=tree)
    assert counts == {"pushed": 0, "pending": 1}
    assert "changed in the cloud while pushing" in "\n".join(lines)
    assert _row(client, A)["mask_regions"] == _payload(TRI, TRI)


def test_entries_for_undeployed_exposures_are_reported(tree):
    client = _cloud([])
    _reg(tree, A, TRI)
    counts, text = _push(client, {}, tree)
    assert counts["pushed"] == 0 and counts["pending"] == 1
    assert "deploy the field first" in text


def test_a_missing_file_is_no_statement(tree):
    # No masks/<filter>/ directory and no exposures.json: push sends nothing even
    # though the base says this machine once had entries; pull writes them back.
    client = _cloud([_exp(1, A, mask=_payload(TRI), status="excluded")])
    state = {}
    _pull(client, state, tree)
    (nr.masks_dir(FIELD, tree) / F / f"{A}.reg").unlink()
    (nr.masks_dir(FIELD, tree) / F).rmdir()
    (nr.ref_dir(FIELD, tree) / nr.EXPOSURES_FILE).unlink()
    counts, _ = _push(client, state, tree, prune=True)
    assert counts["pushed"] == 0
    assert _row(client, A)["review_status"] == "excluded"
    _pull(client, state, tree)
    assert _local(tree).status == {(F, A): "excluded"} and (F, A) in _local(tree).mask


def test_cloud_reads_page_past_the_row_cap(tree, monkeypatch):
    monkeypatch.setattr(nr, "_PAGE", 3)
    rows = [_exp(i, f"jw_{i:03d}_nrcalong", status="approved") for i in range(1, 9)]
    rows += [_exp(20, "jw_020_nrcalong", mask=_payload(TRI), status="excluded")]
    client = _cloud(rows)
    got = nr.fetch_cloud_rows(client, [FIELD])[FIELD]
    assert sorted(r["id"] for r in got) == [1, 2, 3, 4, 5, 6, 7, 8, 20]


def test_diff_reports_each_direction(tree):
    client = _cloud([_exp(1, A, mask=_payload(TRI)), _exp(2, B), _exp(3, C, status="approved")])
    _reg(tree, B, SQUARE)
    _exposures(tree)
    lines = nr.sync_diff(client, [FIELD], {}, root=tree)
    text = "\n".join(lines)
    assert f"< cloud-only       {FIELD} mask {F}/{A}" in text
    assert f"> local-only       {FIELD} mask {F}/{B}" in text
    assert f"< cloud-only       {FIELD} status {F}/{C}: approved" in text


def test_config_cli_masks_flag_pushes_and_pulls(tree, monkeypatch):
    client = _cloud([_exp(1, A, status="excluded")])
    config = {"supabase": {"url": "http://127.0.0.1:54321", "_auth_mode": "local"}}
    monkeypatch.setattr(config_cli, "_client", lambda *a: (config, client))
    monkeypatch.setattr(config_cli.cs, "load_state", lambda: {})
    monkeypatch.setattr(config_cli.cs, "save_state", lambda state: None)
    res = CliRunner().invoke(config_group, ["pull", "--masks", "--field", FIELD, "--local"])
    assert res.exit_code == 0, res.output
    assert "masks: pulling" in res.output and "fields:" not in res.output
    assert _local(tree).status == {(F, A): "excluded"}
