"""
skymatch: per-visit JWST Image3Pipeline skymatch step.

Reads a single visit's worth of canonical exposure files, runs
``Image3Pipeline`` (skymatch only, all other substeps skipped) inside a
private scratch directory, and atomically promotes each scratch output
back to its canonical path. The skymatch substep with ``subtract=True``
mutates SCI in place and updates ``meta.background.*``.

JWST Image3Pipeline writes fresh ImageModel outputs that don't carry our
``SRCMASK`` / ``CFMASK`` extensions through. We extract those extensions
from each canonical *before* skymatch runs and re-attach them via
``atomic_save(..., extra_hdus=...)`` after.
"""

import json
import os
import tempfile

from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp


# Extensions that are not part of the JWST datamodel schema and therefore
# get dropped on a pipeline round-trip. Pulled out before, re-attached after.
EXTRA_EXT_NAMES = ('SRCMASK', 'CFMASK')


def _capture_extras(canonical):
    """Return a list of ``ImageHDU`` copies for any non-schema extensions."""
    extras = []
    with fits.open(canonical) as hdul:
        for name in EXTRA_EXT_NAMES:
            if name not in hdul:
                continue
            hdu = hdul[name]
            extras.append(fits.ImageHDU(
                hdu.data.copy(), header=hdu.header.copy(), name=name,
            ))
    return extras


def _none_if_string_none(val):
    """JWST steps want Python ``None`` where TOML can only carry the string."""
    if val == 'none' or val == 'None':
        return None
    return val


def skymatch_step(visit_files, field, step_config, overwrite=False):
    """Run skymatch on one visit's worth of canonical exposures.

    Parameters
    ----------
    visit_files : list of str
        Canonical exposure paths for a single JWST visit.
    field : Field
    step_config : dict
        ``[nircam.skymatch]`` (legacy ``[nircam.stage3.skymatch]``).
    overwrite : bool
    """
    if not visit_files:
        return

    if not overwrite and all(
            cfp.has_step(f, 'CFP_SMAT') for f in visit_files):
        log("Skipping skymatch: CFP_SMAT set on every visit member")
        return

    visit = os.path.basename(visit_files[0]).split('_')[0]
    log(f"Running skymatch on visit {visit} ({len(visit_files)} exposures)")

    # Capture extras before the round-trip so we can re-attach them after
    saved_extras = {
        os.path.basename(f).removesuffix('.fits'): _capture_extras(f)
        for f in visit_files
    }

    params = {
        'assign_mtwcs': {'skip': True},
        'tweakreg': {'skip': True},
        'skymatch': {
            'skymethod': step_config.get('skymethod', 'match'),
            'match_down': step_config.get('match_down', True),
            'subtract': step_config.get('subtract', True),
            'stepsize': _none_if_string_none(step_config.get('stepsize', None)),
            'skystat': step_config.get('skystat', 'mode'),
            'dqbits': step_config.get('dqbits', '~DO_NOT_USE+NON_SCIENCE'),
            'lower': _none_if_string_none(step_config.get('lower', None)),
            'upper': _none_if_string_none(step_config.get('upper', None)),
            'nclip': step_config.get('nclip', 10),
            'binwidth': step_config.get('binwidth', 0.1),
        },
        'outlier_detection': {'skip': True},
        'resample': {'skip': True},
        'source_catalog': {'skip': True},
    }

    from jwst.associations.lib.rules_level3_base import DMS_Level3_Base
    from jwst.associations import asn_from_list
    from jwst.pipeline import calwebb_image3
    from jwst.datamodels import ImageModel

    with tempfile.TemporaryDirectory(prefix='skymatch-') as scratch:
        asn_file = os.path.join(scratch, f'sky_{visit}_asn.json')
        asn = asn_from_list.asn_from_list(
            visit_files, rule=DMS_Level3_Base,
            product_name=f'skymatch_{visit}',
        )
        with open(asn_file, 'w') as fp:
            _, serialized = asn.dump(format='json')
            fp.write(serialized)

        try:
            calwebb_image3.Image3Pipeline.call(
                asn_file, output_dir=scratch, steps=params,
                save_results=True,
            )
        except Exception:
            log(f"skymatch failed on visit {visit}")
            raise

        # Match scratch outputs to their canonical paths and promote
        scratch_outputs = sorted(
            f for f in os.listdir(scratch)
            if f.endswith('.fits') and f != f'sky_{visit}_asn.json'
        )

        for canonical in visit_files:
            rootname = os.path.basename(canonical).removesuffix('.fits')
            matches = [f for f in scratch_outputs if f.startswith(rootname)]
            if not matches:
                log(f"skymatch: no scratch output found for {rootname}")
                continue
            if len(matches) > 1:
                # Prefer the one with the shortest suffix (closest to canonical)
                matches.sort(key=len)
            scratch_out = os.path.join(scratch, matches[0])

            with ImageModel(scratch_out) as model:
                atomic_save(
                    model, canonical,
                    header_updates=cfp.format(CFP_SMAT=None),
                    extra_hdus=saved_extras.get(rootname),
                )
            log(f"  skymatch promoted: {rootname}")
