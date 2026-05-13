"""
Shared flat-field helpers used by ``wisp_step`` and ``striping_step``.

Both steps need to apply a NIRCam flat in-memory (without writing a
flat-fielded copy to disk). The CRDS lookup falls back to a custom-flat
override when present, and the apply call is wrapped with a short retry
loop because parallel workers occasionally collide on CRDS cache files
during a cold-fetch.
"""

import os
from time import sleep

from campfire_pipeline.common.io import log


def resolve_flat(model, field, use_custom):
    """Pick the flat reference: custom (if requested + present) else CRDS."""
    crds_dict = {
        'INSTRUME': 'NIRCAM',
        'DETECTOR': model.meta.instrument.detector,
        'FILTER': model.meta.instrument.filter,
        'PUPIL': model.meta.instrument.pupil,
        'DATE-OBS': model.meta.observation.date,
        'TIME-OBS': model.meta.observation.time,
    }

    if use_custom:
        fn = crds_dict['FILTER'].upper()
        det = crds_dict['DETECTOR'].upper()
        flatfile = os.path.join(
            field.flats_dir, f'flat_nircam_{fn}_{det}_CLEAR.fits',
        )
        if os.path.exists(flatfile):
            return flatfile
        log(f"Custom flat {os.path.basename(flatfile)} not found; "
            f"falling back to CRDS")

    try:
        crds_context = os.environ['CRDS_CONTEXT']
    except KeyError:
        import crds
        crds_context = crds.get_default_context()
    import crds
    refs = crds.getreferences(crds_dict, reftypes=['flat'], context=crds_context)
    return refs.get('flat')


def apply_flat_with_retry(model, flatfile, delays=(0, 3, 10)):
    """Apply flat in-memory, retrying on transient CRDS races."""
    from jwst.datamodels import FlatModel
    from jwst.flatfield.flat_field import do_correction

    last_exc = None
    for delay in delays:
        if delay:
            sleep(delay)
        try:
            with FlatModel(flatfile) as flat:
                model, _ = do_correction(model, flat)
            return model
        except Exception as e:
            last_exc = e
    raise last_exc
