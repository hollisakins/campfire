"""Science-content hash of a FITS file (data-release design §4.6, plan P0b).

``file_hash`` (sha256 of the whole file) changes whenever anything in the file
changes, including the run-time cards the pipeline stamps on every rerun
(``CMPFRTIM``), the data-release stamp a freeze adds (``CMPFRDR``), and the
``DATE`` / ``CHECKSUM`` / ``DATASUM`` cards writers refresh. The *science hash*
is the identity that survives all of that: it covers every HDU's data block
byte for byte and every header card except a fixed volatile set. Two files
with the same science hash carry the same science; a stamp or a no-op rerun
does not change it.

Definition (``SCIENCE_HASH_VERSION`` = 1; change the version if any rule here
changes, because every stored hash becomes incomparable):

    sha256 over, for each HDU in file order,
        b"HDU" + index
        + b"HEADER" + "\\n".join(sorted(f"{keyword}={value!r}" for every card
              whose keyword is not in VOLATILE_CARDS and is not
              COMMENT / HISTORY / blank))
        + b"DATA" + len(data_bytes) + data_bytes      (raw on-disk block,
              unpadded, scaling NOT applied; empty when the HDU has no data)

Header values go through astropy's parsed card value (so string padding,
card formatting and CONTINUE splitting do not matter) and ``repr`` (so
``1.0`` and ``1`` stay distinct, as they are distinct in the file). Data bytes
are taken from the on-disk data segment, so BSCALE/BZERO, byte order and
variable-length-array heaps are covered exactly as stored.

One implementation on purpose: the pipeline (``linefit_stage`` writes
``SCIHASH``), deploy (``spectra.science_hash`` / ``storage_objects.science_hash``)
and the backfill script all import this module, so there is nothing to keep
in step.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from astropy.io import fits

SCIENCE_HASH_VERSION = 1

#: Header cards excluded from the hash: run-time stamps and integrity cards
#: that writers refresh without the science changing.
VOLATILE_CARDS = frozenset({
    "CMPFRTIM",   # CAMPFIRE reduction timestamp, rewritten every run
    "CMPFRDR",    # data-release stamp written by the freeze byte pass
    "DATE",       # file-write date refreshed by astropy / jwst writers
    "CHECKSUM",   # astropy checksum cards (depend on everything above)
    "DATASUM",
})

#: Commentary keywords (never part of the identity).
_COMMENTARY = frozenset({"COMMENT", "HISTORY", ""})


def _header_lines(header: fits.Header) -> list[str]:
    lines = []
    for card in header.cards:
        kw = card.keyword
        if kw in VOLATILE_CARDS or kw in _COMMENTARY:
            continue
        try:
            value = card.value
        except Exception:  # unparsable card: fall back to its raw image
            value = card.image.strip()
        lines.append(f"{kw}={value!r}")
    lines.sort()
    return lines


def science_hash(path: str | Path) -> str:
    """Return ``'sha256:<hex>'`` over the file's science content (see module doc)."""
    h = hashlib.sha256()
    path = Path(path)
    with path.open("rb") as raw, fits.open(raw, memmap=False, lazy_load_hdus=False,
                                            do_not_scale_image_data=True) as hdul:
        for i, hdu in enumerate(hdul):
            h.update(b"HDU" + str(i).encode())
            h.update(b"HEADER" + "\n".join(_header_lines(hdu.header)).encode("utf-8"))
            # Raw data segment as stored: offset/size come from the parsed
            # layout; size is the unpadded length (heap included for tables).
            size = int(getattr(hdu, "_data_size", 0) or 0)
            offset = getattr(hdu, "_data_offset", None)
            h.update(b"DATA" + str(size).encode())
            if size and offset is not None:
                raw.seek(int(offset))
                remaining = size
                while remaining > 0:
                    chunk = raw.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise OSError(f"{path}: data segment of HDU {i} truncated")
                    h.update(chunk)
                    remaining -= len(chunk)
    return f"sha256:{h.hexdigest()}"


def file_hash(path: str | Path) -> str:
    """Whole-file ``'sha256:<hex>'`` (the existing ``file_hash`` identity), here
    so callers that want both read the file through one module."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"
