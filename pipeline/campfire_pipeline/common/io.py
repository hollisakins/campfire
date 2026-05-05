"""
Shared I/O utilities: logging and filename helpers.
"""

import os
from datetime import datetime


def log(*args, **kwargs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}]", *args, **kwargs)


def atomic_save(model_or_hdul, path, header_updates=None, extra_hdus=None):
    """Save a JWST datamodel or astropy HDUList atomically.

    Writes to ``<path>.tmp`` and then ``os.replace``-s into place. ``os.replace``
    is atomic on POSIX, so a crash mid-save can leave a stray ``.tmp`` file but
    will never corrupt the canonical ``path``.

    Parameters
    ----------
    model_or_hdul : object
        Either a JWST ``DataModel`` (anything with ``.save(path)``) or an
        astropy ``HDUList`` (anything with ``.writeto(path, overwrite=True)``).
    path : str
        Destination path. Parent directory must already exist. Should end
        in a known extension (``.fits`` or ``.asdf``); the temporary path
        used for the staging write inserts ``.tmp`` *before* that extension
        so JWST datamodels' filetype dispatch still works.
    header_updates : dict, optional
        ``{key: (value, comment)}`` or ``{key: value}`` entries to apply to
        the primary header before the rename. Lets callers stamp a CFP
        provenance keyword in the same atomic operation that writes the
        mutated data, so a crash between save and stamp is not possible.
    extra_hdus : list of astropy.io.fits.ImageHDU, optional
        Extra extensions to append to the saved file. Any existing extension
        with the same ``EXTNAME`` is removed first (replace-or-append). Used
        for ``SRCMASK`` (algorithmic source mask) and ``CFMASK`` (user region
        mask) extensions that aren't part of the JWST datamodel schema.
    """
    # Insert .tmp before the file extension. JWST datamodels dispatch
    # save format on the extension, so e.g. `path + '.tmp'` would error
    # with "unknown filetype .tmp".
    base, ext = os.path.splitext(path)
    tmp = f'{base}.tmp{ext}' if ext else f'{path}.tmp'
    if hasattr(model_or_hdul, 'save'):
        model_or_hdul.save(tmp)
    else:
        model_or_hdul.writeto(tmp, overwrite=True)
    if header_updates or extra_hdus:
        from astropy.io import fits
        with fits.open(tmp, mode='update') as hdul:
            if header_updates:
                for key, val in header_updates.items():
                    hdul[0].header[key] = val
            if extra_hdus:
                for hdu in extra_hdus:
                    name = hdu.name
                    # Drop any existing extension of the same name first.
                    existing = [i for i, h in enumerate(hdul) if h.name == name]
                    for i in reversed(existing):
                        del hdul[i]
                    hdul.append(hdu)
    os.replace(tmp, path)


def files_to_glob(filenames):
    """
    Compress a list of filenames into a minimal glob-style string by collapsing
    varying tokens into {opt1,opt2,...} syntax.

    Example:
        ['jw_00002_nrs1_cal.fits', 'jw_00003_nrs2_cal.fits']
        -> 'jw_0000{2,3}_nrs{1,2}_cal.fits'
    """
    split = [f.split('_') for f in filenames]

    # Sanity check: all filenames should have the same number of tokens
    n_tokens = len(split[0])
    if not all(len(s) == n_tokens for s in split):
        raise ValueError("Filenames have inconsistent structure (different number of '_'-separated tokens)")

    result_tokens = []
    for i in range(n_tokens):
        # Unique values at this token position, preserving order
        seen = {}
        values = [seen.setdefault(s[i], s[i]) for s in split if s[i] not in seen]

        if len(values) == 1:
            result_tokens.append(values[0])
        else:
            prefix = os.path.commonprefix(values)
            suffixes = [v[len(prefix):] for v in values]
            result_tokens.append(f"{prefix}{{{','.join(suffixes)}}}")

    return '_'.join(result_tokens)
