"""Unit tests for reconcile.classify — focused on correctness of the
split/merge/overlap topology detection that would otherwise silently
corrupt the objects catalog. Also covers file_hash staleness detection
in batch_upsert_spectra."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from campfire.deploy import reconcile
from campfire.deploy.reconcile import (
    ClusterAggregates,
    Insert,
    Orphan,
    Proposals,
    Split,
    Update,
    apply_proposals,
    classify,
)
from campfire.deploy.supabase import batch_upsert_spectra


def _mock_supabase_client(existing_rows: list[dict]) -> MagicMock:
    """Return a Supabase client whose select chain yields ``existing_rows``."""
    client = MagicMock()
    client.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])
    (
        client.table.return_value.select.return_value.in_.return_value
        .execute.return_value
    ) = MagicMock(data=existing_rows)
    return client


def _target(db_id: int, target_id: str, ra: float, dec: float,
            object_id: int | None, program: str = 'prog', obs: str = 'obs'):
    return {
        'id': db_id,
        'target_id': target_id,
        'ra': ra,
        'dec': dec,
        'object_id': object_id,
        'program_slug': program,
        'observation': obs,
    }


def _obj(db_id: int, object_id: str, ra: float, dec: float, *, active: bool = True):
    return {
        'id': db_id,
        'object_id': object_id,
        'field': 'cosmos',
        'ra': ra,
        'dec': dec,
        'is_active': active,
        'redshift_quality': None,
        'last_inspected_at': None,
        'max_snr': None,
        'redshift_inspected': None,
        'redshift_auto': None,
        'last_inspected_by': None,
        'last_data_change_at': None,
        'staleness_reason': None,
        'version': 0,
    }


def test_pure_split_classifies_as_split():
    # A's members {t1,t2,t3} land in two distinct clusters: {t1,t2} and {t3}.
    # No other object contributes. Should be a pure Split, no overlap.
    targets = [
        _target(1, 't1', 150.0, 2.0, 100),
        _target(2, 't2', 150.0, 2.0, 100),
        _target(3, 't3', 150.1, 2.1, 100),
    ]
    groups = [[0, 1], [2]]
    existing = [_obj(100, 'OBJ-A', 150.05, 2.05)]
    members = {100: {'t1', 't2', 't3'}}

    p = classify(
        groups=groups, targets=targets, existing_objects=existing,
        members_by_obj=members, spectra_map={},
        changed_hashes=set(),
    )

    assert len(p.complex_overlaps) == 0
    assert len(p.splits) == 1
    assert p.splits[0].object['id'] == 100
    assert p.orphans == []


def test_pure_merge_classifies_as_merge():
    # Two objects A, B whose members collapse into a single cluster.
    targets = [
        _target(1, 't1', 150.0, 2.0, 100),
        _target(2, 't2', 150.0, 2.0, 101),
    ]
    groups = [[0, 1]]
    existing = [
        _obj(100, 'OBJ-A', 150.0, 2.0),
        _obj(101, 'OBJ-B', 150.0, 2.0),
    ]
    members = {100: {'t1'}, 101: {'t2'}}

    p = classify(
        groups=groups, targets=targets, existing_objects=existing,
        members_by_obj=members, spectra_map={},
        changed_hashes=set(),
    )

    assert len(p.complex_overlaps) == 0
    assert len(p.merges) == 1
    assert {o['id'] for o in [p.merges[0].survivor] + p.merges[0].losers} == {100, 101}


def test_stale_cross_field_membership_classifies_as_insert():
    # Regression: a target whose object_id FK points to an object that is NOT
    # among the field's existing_objects (a stale / cross-field membership left
    # by a prior bad deploy — e.g. the (0,0) ghost that collapsed every
    # fixed-slit target onto one object in another field). Must be treated as
    # unassigned and classified as an Insert, not KeyError on obj_by_id[oid].
    targets = [_target(1, 't1', 150.0, 2.0, 999999)]  # 999999 lives elsewhere
    groups = [[0]]
    existing: list[dict] = []  # ghost object not fetched for this field
    members: dict[int, set[str]] = {}

    p = classify(
        groups=groups, targets=targets, existing_objects=existing,
        members_by_obj=members, spectra_map={},
        changed_hashes=set(),
    )

    assert len(p.inserts) == 1
    assert p.splits == [] and p.merges == [] and p.complex_overlaps == []
    assert p.orphans == []


def test_split_plus_merge_overlap_is_detected_not_silently_dropped():
    # A splits into clusters X={t1,t4} and Y={t2,t3}. X also absorbs B={t4}.
    # Regression test for the pre-fix bug where merge step skipped X
    # because the split step had already marked it handled, silently
    # orphaning B and losing its inspection state.
    targets = [
        _target(1, 't1', 150.0, 2.0, 100),
        _target(2, 't2', 150.2, 2.2, 100),
        _target(3, 't3', 150.2, 2.2, 100),
        _target(4, 't4', 150.0, 2.0, 101),  # was in B, now clusters with t1
    ]
    groups = [[0, 3], [1, 2]]  # X={t1,t4}, Y={t2,t3}
    existing = [
        _obj(100, 'OBJ-A', 150.1, 2.1),
        _obj(101, 'OBJ-B', 150.0, 2.0),
    ]
    members = {100: {'t1', 't2', 't3'}, 101: {'t4'}}

    p = classify(
        groups=groups, targets=targets, existing_objects=existing,
        members_by_obj=members, spectra_map={},
        changed_hashes=set(),
    )

    # Must be surfaced as a complex_overlap, NOT as a pure split with B orphaned.
    assert len(p.complex_overlaps) == 1
    ov = p.complex_overlaps[0]
    assert ov.split_source['id'] == 100
    assert [s['id'] for s in ov.merge_sources] == [101]
    # And critically, B must NOT be silently orphaned.
    assert all(o.object['id'] != 101 for o in p.orphans)
    # The split should NOT be double-recorded either.
    assert len(p.splits) == 0
    assert len(p.merges) == 0


def test_batch_upsert_spectra_flags_null_to_hash_as_reprocessed():
    # Regression for the pre-fix bug where `old_hash is not None` gated the
    # "reprocessed" signal, so a row with pre-rollout NULL hash would be
    # silently treated as clean on its first re-upload with a real hash.
    client = _mock_supabase_client(
        [{'target_id': 't1', 'grating': 'prism', 'file_hash': None}]
    )
    _, changed = batch_upsert_spectra(
        client,
        [{'target_id': 't1', 'grating': 'prism', 'file_hash': 'sha256:A'}],
    )
    assert changed == {('t1', 'prism')}


def test_batch_upsert_spectra_does_not_flag_brand_new_row():
    client = _mock_supabase_client([])
    _, changed = batch_upsert_spectra(
        client,
        [{'target_id': 't1', 'grating': 'prism', 'file_hash': 'sha256:A'}],
    )
    assert changed == set()


def test_batch_upsert_spectra_does_not_flag_unchanged_hash():
    client = _mock_supabase_client(
        [{'target_id': 't1', 'grating': 'prism', 'file_hash': 'sha256:A'}]
    )
    _, changed = batch_upsert_spectra(
        client,
        [{'target_id': 't1', 'grating': 'prism', 'file_hash': 'sha256:A'}],
    )
    assert changed == set()


def test_batch_upsert_spectra_flags_hash_change():
    client = _mock_supabase_client(
        [{'target_id': 't1', 'grating': 'prism', 'file_hash': 'sha256:A'}]
    )
    _, changed = batch_upsert_spectra(
        client,
        [{'target_id': 't1', 'grating': 'prism', 'file_hash': 'sha256:B'}],
    )
    assert changed == {('t1', 'prism')}


def test_unchanged_1to1_match_is_skipped_not_updated():
    # Regression: a 1:1 match whose aggregates equal the existing object's
    # stored aggregates and whose membership is unchanged must NOT be added
    # to proposals.updates. Otherwise every reconcile run rewrites every
    # object and bumps updated_at, which bloats incremental sync cursors
    # downstream.
    targets = [_target(1, 't1', 150.0, 2.0, 100)]
    groups = [[0]]
    obj = _obj(100, 'OBJ-A', 150.0, 2.0)
    obj.update({
        'n_targets': 1, 'n_spectra': 0,
        'programs': ['prog'], 'gratings': [], 'observations': ['obs'],
        'max_snr': None, 'max_exposure_time': None,
    })
    # object_id has to match the IAU name build_cluster_aggregates() will
    # generate from the centroid, since that is one of the compared fields.
    from campfire.deploy.objects import generate_iau_name
    obj['object_id'] = generate_iau_name(150.0, 2.0)

    p = classify(
        groups=groups, targets=targets, existing_objects=[obj],
        members_by_obj={100: {'t1'}}, spectra_map={},
        changed_hashes=set(),
    )

    assert p.unchanged == 1
    assert p.updates == []
    assert p.inserts == []


def test_changed_aggregates_still_classified_as_update():
    # Flip side of the skip: if the aggregates differ (e.g. stored n_spectra
    # is stale), the cluster must be appended to proposals.updates so the
    # row gets rewritten. Guards against the skip-helper being too permissive.
    targets = [_target(1, 't1', 150.0, 2.0, 100)]
    groups = [[0]]
    obj = _obj(100, 'OBJ-A', 150.0, 2.0)
    obj.update({
        'n_targets': 1, 'n_spectra': 5,  # stale — cluster has 0 spectra
        'programs': ['prog'], 'gratings': [], 'observations': ['obs'],
        'max_snr': None, 'max_exposure_time': None,
    })
    from campfire.deploy.objects import generate_iau_name
    obj['object_id'] = generate_iau_name(150.0, 2.0)

    p = classify(
        groups=groups, targets=targets, existing_objects=[obj],
        members_by_obj={100: {'t1'}}, spectra_map={},
        changed_hashes=set(),
    )

    assert p.unchanged == 0
    assert len(p.updates) == 1


def test_inactive_1to1_match_is_not_skipped():
    # Inactive 1:1 matches need a reactivation write; they must always
    # land on proposals.updates regardless of aggregate equality.
    targets = [_target(1, 't1', 150.0, 2.0, 100)]
    groups = [[0]]
    obj = _obj(100, 'OBJ-A', 150.0, 2.0, active=False)
    obj.update({
        'n_targets': 1, 'n_spectra': 0,
        'programs': ['prog'], 'gratings': [], 'observations': ['obs'],
        'max_snr': None, 'max_exposure_time': None,
    })
    from campfire.deploy.objects import generate_iau_name
    obj['object_id'] = generate_iau_name(150.0, 2.0)

    p = classify(
        groups=groups, targets=targets, existing_objects=[obj],
        members_by_obj={100: {'t1'}}, spectra_map={},
        changed_hashes=set(),
    )

    assert p.unchanged == 0
    assert len(p.updates) == 1


def _agg(object_id='J1', ra=150.0, dec=2.0,
         member_db_ids=(1,), member_ids=('t1',)):
    return ClusterAggregates(
        object_id=object_id, ra=ra, dec=dec,
        n_targets=len(member_db_ids), n_spectra=0,
        programs=['prog'], gratings=[], observations=['obs'],
        max_snr=None, max_exposure_time=None,
        member_target_db_ids=list(member_db_ids),
        member_target_ids=set(member_ids),
    )


def test_apply_proposals_rpc_payload_and_parse():
    # Deploy-path apply (no splits/merges) must go through the atomic RPC,
    # send member_target_db_ids inside each element (so the FK assignment
    # shares the transaction), drop the non-serializable member_target_ids
    # set, and parse the returned id maps into (stats, changed_ids).
    proposals = Proposals(
        inserts=[Insert(cluster_idx=0,
                        aggregates=_agg('JINS', member_db_ids=(10, 11),
                                        member_ids=('t10', 't11')))],
        updates=[Update(cluster_idx=1, object={'id': 500, 'is_active': True},
                        aggregates=_agg('JUPD', member_db_ids=(20,),
                                        member_ids=('t20',)),
                        staleness_reason='new_target')],
        orphans=[Orphan(object={'id': 900})],
    )

    client = MagicMock()
    client.rpc.return_value.execute.return_value = MagicMock(data={
        'insert_id_map': {'JINS': 1001},
        'revived_ids': [],
        'updated_ids': [500],
        'inserted_count': 1,
        'revived_count': 0,
        'updated_count': 1,
        'reactivated_count': 0,
        'orphaned_count': 1,
        'target_fks_set': 3,
    })

    stats, changed = apply_proposals(client, 'egs', proposals)

    name, params = client.rpc.call_args[0]
    assert name == 'apply_object_reconciliation'
    assert params['p_field'] == 'egs'
    assert params['p_orphan_ids'] == [900]
    ins = params['p_inserts'][0]
    assert ins['object_id'] == 'JINS'
    assert ins['member_target_db_ids'] == [10, 11]
    assert 'member_target_ids' not in ins
    upd = params['p_updates'][0]
    assert upd['object_db_id'] == 500
    assert upd['staleness_reason'] == 'new_target'
    assert upd['reactivate'] is False

    assert stats['inserted'] == 1
    assert stats['updated'] == 1
    assert stats['soft_deleted'] == 1
    assert stats['target_fks_set'] == 3
    assert stats['staleness_new_target'] == 1
    # changed_ids = inserted ∪ revived ∪ updated (orphans excluded).
    assert changed == {1001, 500}


def test_apply_proposals_rpc_reactivate_flag_for_inactive_update():
    # An inactive 1:1 match carries reactivate=True so the RPC flips is_active.
    proposals = Proposals(
        updates=[Update(cluster_idx=0, object={'id': 7, 'is_active': False},
                        aggregates=_agg('JX'), staleness_reason=None)],
    )
    client = MagicMock()
    client.rpc.return_value.execute.return_value = MagicMock(data={
        'insert_id_map': {}, 'revived_ids': [], 'updated_ids': [7],
        'reactivated_count': 1, 'orphaned_count': 0, 'target_fks_set': 1,
    })

    _stats, changed = apply_proposals(client, 'egs', proposals)

    assert client.rpc.call_args[0][1]['p_updates'][0]['reactivate'] is True
    assert changed == {7}


def test_apply_proposals_dispatches_to_legacy_when_splits_present():
    # Splits/merges keep the legacy Python apply (photometry/list migration);
    # they must NOT hit the atomic RPC path.
    proposals = Proposals(
        splits=[Split(object={'id': 1, 'ra': 150.0, 'dec': 2.0},
                      daughters=[_agg('JD1'), _agg('JD2')],
                      daughter_cluster_indices=[0, 1],
                      inheritor_index=0)],
    )
    client = MagicMock()
    with patch.object(reconcile, '_apply_proposals_legacy',
                      return_value=({'split': 1}, {1})) as legacy, \
            patch.object(reconcile, '_apply_proposals_rpc') as rpc:
        stats, _changed = apply_proposals(client, 'egs', proposals)

    legacy.assert_called_once()
    rpc.assert_not_called()
    assert stats == {'split': 1}


def test_apply_proposals_empty_skips_rpc():
    client = MagicMock()
    stats, changed = apply_proposals(client, 'egs', Proposals())
    client.rpc.assert_not_called()
    assert stats == {}
    assert changed == set()


def test_independent_split_and_merge_both_apply():
    # A → {X, Y} (split), Z ← {B, C} (merge). No cluster overlap.
    targets = [
        _target(1, 't1', 150.0, 2.0, 100),
        _target(2, 't2', 150.3, 2.3, 100),
        _target(3, 't3', 151.0, 3.0, 101),
        _target(4, 't4', 151.0, 3.0, 102),
    ]
    groups = [[0], [1], [2, 3]]  # X={t1}, Y={t2}, Z={t3,t4}
    existing = [
        _obj(100, 'OBJ-A', 150.15, 2.15),
        _obj(101, 'OBJ-B', 151.0, 3.0),
        _obj(102, 'OBJ-C', 151.0, 3.0),
    ]
    members = {100: {'t1', 't2'}, 101: {'t3'}, 102: {'t4'}}

    p = classify(
        groups=groups, targets=targets, existing_objects=existing,
        members_by_obj=members, spectra_map={},
        changed_hashes=set(),
    )

    assert len(p.complex_overlaps) == 0
    assert len(p.splits) == 1
    assert p.splits[0].object['id'] == 100
    assert len(p.merges) == 1
