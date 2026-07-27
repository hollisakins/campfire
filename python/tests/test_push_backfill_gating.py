"""`plan_remote_push` must not write to the registry unless it is really pushing.

Planning is also what a dry run does: `campfire push --dry-run` and scoped
`campfire status` both call `plan_remote_push` and then return. The legacy
`wcs_hash` reconciliation produces registry UPDATEs, so it is gated behind an
explicit `apply_backfill` — otherwise a command advertised as a read-only diff
mutates `storage_objects`.
"""
import types

import numpy as np
import pytest
from astropy.io import fits
from campfire_layout import KeyScheme, Scope, storage_key

from campfire.deploy import push as push_mod
from campfire.deploy.r2 import UploadTask
from campfire.storage.hashing import compute_file_hash, exposure_identity


class _FakeQuery:
    """Records the writes it is asked to perform; reads return seeded rows."""

    def __init__(self, rows, updates):
        self._rows = rows
        self._updates = updates
        self._payload = None

    def select(self, *_a, **_k):
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, *_a):
        return self

    def in_(self, *_a):
        return self

    def is_(self, *_a):
        return self

    def execute(self):
        if self._payload is not None:
            self._updates.append(self._payload)
            return types.SimpleNamespace(data=[])
        return types.SimpleNamespace(data=list(self._rows))


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def table(self, name):
        return _FakeQuery(self._rows if name == 'storage_objects' else [],
                          self.updates)


@pytest.fixture
def legacy_exposure(tmp_path):
    """An exposure + the pre-`wcs_hash` registry row that reconciles to it."""
    sci = fits.ImageHDU(np.arange(16, dtype='f4').reshape(4, 4), name='SCI')
    for key, value in {
        'CTYPE1': 'RA---TAN', 'CTYPE2': 'DEC--TAN',
        'CRPIX1': 2.0, 'CRPIX2': 2.0, 'CRVAL1': 150.0, 'CRVAL2': 2.0,
        'CD1_1': -8.6e-6, 'CD2_2': 8.6e-6,
    }.items():
        sci.header[key] = value
    dq = fits.ImageHDU(np.zeros((4, 4), dtype='i4'), name='DQ')
    path = tmp_path / 'jw001_nrca1.fits'
    fits.HDUList([fits.PrimaryHDU(), sci, dq]).writeto(path)

    key = storage_key('nircam_exposure', Scope(field='cosmos', filt='f444w'),
                      path.name, scheme=KeyScheme.CANONICAL)
    task = UploadTask(path, key, 'application/fits')
    sci_dq, wcs = exposure_identity(path)
    row = {
        'storage_key': key, 'status': 'active', 'backend': 'osn',
        'content_hash': compute_file_hash(path),   # cloud bytes ARE these bytes
        'sci_dq_hash': sci_dq, 'wcs_hash': None,   # ...but no recorded WCS
    }
    return task, row, wcs


def test_planning_alone_does_not_write_to_the_registry(legacy_exposure):
    task, row, wcs = legacy_exposure
    client = _FakeClient([row])

    plan, _rows = plan_remote_push_default(client, [task])

    # The reconciliation is still *derived* — a dry run reports it accurately.
    assert plan.unchanged == [task]
    assert plan.backfill == [(task.r2_key, wcs)]
    assert 'reconciled' in plan.summary()
    # ...but nothing was written.
    assert client.updates == []


def test_backfill_written_only_when_opted_in(legacy_exposure):
    task, row, wcs = legacy_exposure
    client = _FakeClient([row])

    push_mod.plan_remote_push(client, None, [task], backend='osn',
                              progress=False, apply_backfill=True)

    assert client.updates == [{'wcs_hash': wcs}]


def plan_remote_push_default(client, tasks):
    """Call with the default gating — what a dry run gets."""
    return push_mod.plan_remote_push(client, None, tasks, backend='osn',
                                     progress=False)
