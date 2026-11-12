#!/usr/bin/env python
"""Snapshot and compare the pixels of a combine run, bit for bit.

Used to prove that a refactor of the shared mosaic machinery changes nothing
(docs/hst/design.md, section 3.3). Run ``snapshot`` on the output directories of
a combine run before the change, re-run the same combine after it, snapshot
again, and ``compare``:

    # before the refactor
    cfpipe nircam combine --field rj0911 --filters f277w f444w --overwrite
    python pipeline/scripts/mosaic_baseline.py snapshot \\
        $CAMPFIRE_ROOT/products/nircam/rj0911/f277w \\
        $CAMPFIRE_ROOT/products/nircam_work/rj0911/f277w \\
        -o before.json

    # after it: same commands, then
    python pipeline/scripts/mosaic_baseline.py compare before.json after.json

What is recorded for every ``*.fits`` under the given directories:

* a SHA-256 of the stored bytes of each image HDU, keyed ``EXTNAME,EXTVER``
  (tile-compressed images are hashed decompressed, so recompression alone is
  not a difference);
* a SHA-256 of the WCS-defining header cards (``manifest.compute_wcs_hash``).

and for every ``*_manifest.json``: its content with the fields that legitimately
differ between two runs removed (timestamps, pipeline version, file size and
mtime of the inputs).

Not recorded, on purpose: table HDUs (``ASDF`` and ``HDRTAB`` carry save
timestamps), headers other than the WCS cards, and the drizzle context cube
``CON`` unless ``--with-context`` is given (it can be tens of GiB).

``compare`` exits 0 when the two snapshots are identical and 1 otherwise,
listing every file and extension that differs.
"""

import argparse
import hashlib
import json
import os
import sys

from astropy.io import fits

from campfire_pipeline.imaging.manifest import compute_wcs_hash

_VOLATILE_MANIFEST_KEYS = ('created_at', 'pipeline_version')
_VOLATILE_INPUT_KEYS = ('size', 'mtime_ns')
_CHUNK_ROWS = 1024


def _hash_image(hdu):
    """SHA-256 of an image HDU's pixel values, read in row chunks."""
    data = hdu.data
    if data is None or data.size == 0:
        return None
    h = hashlib.sha256()
    h.update(f'{data.dtype.str}|{data.shape}'.encode())
    if data.ndim < 2:
        h.update(data.tobytes())
    else:
        flat = data.reshape(-1, data.shape[-1])
        for start in range(0, flat.shape[0], _CHUNK_ROWS):
            h.update(flat[start:start + _CHUNK_ROWS].tobytes())
    return h.hexdigest()


def snapshot_fits(path, with_context=False):
    record = {'wcs': compute_wcs_hash(path), 'hdus': {}}
    # memmap keeps a multi-GiB mosaic out of RAM; compressed HDUs decompress
    # as they are read.
    with fits.open(path, memmap=True, do_not_scale_image_data=True) as hdul:
        for hdu in hdul:
            if not isinstance(hdu, (fits.PrimaryHDU, fits.ImageHDU,
                                    fits.CompImageHDU)):
                continue
            if hdu.name == 'CON' and not with_context:
                continue
            digest = _hash_image(hdu)
            if digest is not None:
                record['hdus'][f'{hdu.name},{hdu.ver}'] = digest
    return record


def snapshot_manifest(path):
    with open(path) as fh:
        doc = json.load(fh)
    for key in _VOLATILE_MANIFEST_KEYS:
        doc.pop(key, None)
    for entry in doc.get('inputs', []):
        for key in _VOLATILE_INPUT_KEYS:
            entry.pop(key, None)
    blob = json.dumps(doc, sort_keys=True).encode()
    return {'manifest': hashlib.sha256(blob).hexdigest()}


def snapshot(directories, with_context=False):
    files = {}
    for root in directories:
        root = os.path.abspath(root)
        # Three trailing path levels (e.g. nircam/<field>/<filter> versus
        # nircam_work/<field>/<filter>): enough to keep the canonical and
        # working trees apart, and the same before and after a refactor.
        label = '/'.join(root.split(os.sep)[-3:])
        for dirpath, _dirs, names in os.walk(root):
            for name in sorted(names):
                full = os.path.join(dirpath, name)
                rel = f'{label}/{os.path.relpath(full, root)}'
                if name.endswith('.fits') and '.tmp' not in name:
                    files[rel] = snapshot_fits(full, with_context=with_context)
                elif name.endswith('_manifest.json'):
                    files[rel] = snapshot_manifest(full)
    return {'schema': 'campfire-mosaic-baseline/1', 'files': files}


def compare(a, b):
    """Return a list of human-readable differences between two snapshots."""
    fa, fb = a['files'], b['files']
    diffs = []
    for name in sorted(set(fa) - set(fb)):
        diffs.append(f'only in first:  {name}')
    for name in sorted(set(fb) - set(fa)):
        diffs.append(f'only in second: {name}')
    for name in sorted(set(fa) & set(fb)):
        ra, rb = fa[name], fb[name]
        if 'manifest' in ra or 'manifest' in rb:
            if ra != rb:
                diffs.append(f'manifest differs: {name}')
            continue
        if ra.get('wcs') != rb.get('wcs'):
            diffs.append(f'WCS cards differ: {name}')
        ha, hb = ra.get('hdus', {}), rb.get('hdus', {})
        for ext in sorted(set(ha) | set(hb)):
            if ha.get(ext) != hb.get(ext):
                state = ('missing in first' if ext not in ha else
                         'missing in second' if ext not in hb else 'differs')
                diffs.append(f'pixels {state}: {name} [{ext}]')
    return diffs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    snap = sub.add_parser('snapshot', help='hash the outputs under directories')
    snap.add_argument('directories', nargs='+')
    snap.add_argument('-o', '--output', required=True)
    snap.add_argument('--with-context', action='store_true',
                      help='also hash the drizzle context cube (CON)')
    cmp_ = sub.add_parser('compare', help='compare two snapshots')
    cmp_.add_argument('first')
    cmp_.add_argument('second')
    args = parser.parse_args(argv)

    if args.command == 'snapshot':
        result = snapshot(args.directories, with_context=args.with_context)
        with open(args.output, 'w') as fh:
            json.dump(result, fh, indent=1, sort_keys=True)
        n_hdu = sum(len(r.get('hdus', {})) for r in result['files'].values())
        print(f"{len(result['files'])} files, {n_hdu} image HDUs -> {args.output}")
        return 0

    with open(args.first) as fh:
        first = json.load(fh)
    with open(args.second) as fh:
        second = json.load(fh)
    diffs = compare(first, second)
    if not diffs:
        print(f"identical: {len(first['files'])} files")
        return 0
    print(f'{len(diffs)} difference(s):')
    for line in diffs:
        print('  ' + line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
