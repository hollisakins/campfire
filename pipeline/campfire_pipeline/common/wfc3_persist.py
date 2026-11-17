"""The archive's WFC3/IR persistence predictions: where they are, and fetching
them. Used by ``cfpipe download`` (HST); the step that applies them
(``hst/steps/persist.py``) only reads what is on disk.

STScI runs a persistence model on every WFC3/IR exposure and serves the result
per visit (the "WFC3 Persistence Project",
https://archive.stsci.edu/prepds/persist/; WFC3 Data Handbook section 8.2).
The products are not in MAST's API ("It is not possible to obtain the data from
the standard HST retrieval screen", STScI's IR persistence page): a visit is
one archive,

    <base>/<proposal>/Visit<VV>/<proposal>.Visit<VV>.tar.gz

holding ``<rootname>_persist.fits`` (total predicted persistence),
``_extper.fits`` (from earlier visits only) and ``_flt_cor.fits`` (exposure
minus model) for every exposure of the visit, in every filter. Only the first
is kept, as

    raw/wfc3ir/<proposal>/persist/<rootname>_persist.fits
"""

import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request

ARCHIVE_URL = 'https://archive.stsci.edu/pub/wfc3_persist'
SUFFIX = '_persist.fits'


class PredictionUnavailable(Exception):
    """The archive did not hand over a visit's persistence products."""


def visit_of(rootname):
    """The visit of an HST rootname ``ipppssoot``: the two characters ``ss``,
    as the archive writes them (upper case)."""
    return rootname[4:6].upper()


def persist_dir(raw_root, proposal):
    """``raw/wfc3ir/<proposal>/persist`` given ``raw/wfc3ir``."""
    return os.path.join(str(raw_root), str(proposal), 'persist')


def product_path(raw_root, proposal, rootname):
    return os.path.join(persist_dir(raw_root, proposal), rootname + SUFFIX)


def visit_url(base_url, proposal, visit):
    return (f'{base_url.rstrip("/")}/{proposal}/Visit{visit}/'
            f'{proposal}.Visit{visit}.tar.gz')


def fetch_visit(proposal, visit, out_dir, *, base_url=ARCHIVE_URL, timeout=600):
    """Download one visit's persistence archive and keep its
    ``*_persist.fits`` members in ``out_dir``. Returns the rootnames kept.

    Raises :class:`PredictionUnavailable` with the reason when the archive
    cannot be fetched or read.
    """
    url = visit_url(base_url, proposal, visit)
    os.makedirs(out_dir, exist_ok=True)
    request = urllib.request.Request(
        url, headers={'User-Agent': 'campfire-pipeline'})
    kept = []
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        tar_path = os.path.join(tmp, 'visit.tar.gz')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, \
                    open(tar_path, 'wb') as out:
                shutil.copyfileobj(response, out)
        except urllib.error.HTTPError as e:
            raise PredictionUnavailable(
                f'{url} answered HTTP {e.code} ({e.reason})') from e
        except (urllib.error.URLError, OSError) as e:
            raise PredictionUnavailable(f'{url} could not be fetched: {e}') from e
        try:
            with tarfile.open(tar_path, 'r:*') as tar:
                for member in tar:
                    # Only the name's last component is used: nothing a
                    # member's path says decides where its file is written.
                    name = os.path.basename(member.name)
                    if not member.isfile() or not name.endswith(SUFFIX):
                        continue
                    part = os.path.join(tmp, name)
                    with tar.extractfile(member) as src, open(part, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(part, os.path.join(out_dir, name))
                    kept.append(name[:-len(SUFFIX)])
        except (tarfile.TarError, EOFError) as e:
            raise PredictionUnavailable(f'{url} is not a readable archive: {e}') from e
    return sorted(kept)


def fetch_predictions(rootnames_by_proposal, raw_root, *, base_url=ARCHIVE_URL,
                      timeout=600, dry_run=False, log=print):
    """Fetch the predictions of the given exposures that are not on disk yet.

    ``rootnames_by_proposal`` is ``{proposal: [rootname, ...]}``. One download
    per visit that has an exposure without a file. Returns
    ``{'present': n, 'fetched': n, 'missing': {rootname: reason}}``.
    """
    result = {'present': 0, 'fetched': 0, 'missing': {}}
    visits = {}
    for proposal, roots in rootnames_by_proposal.items():
        for root in roots:
            if os.path.exists(product_path(raw_root, proposal, root)):
                result['present'] += 1
            else:
                visits.setdefault((str(proposal), visit_of(root)), []).append(root)
    for (proposal, visit), roots in sorted(visits.items()):
        url = visit_url(base_url, proposal, visit)
        if dry_run:
            log(f'  would fetch {url} ({len(roots)} exposure(s) need a prediction)')
            continue
        try:
            got = fetch_visit(proposal, visit, persist_dir(raw_root, proposal),
                              base_url=base_url, timeout=timeout)
        except PredictionUnavailable as e:
            for root in roots:
                result['missing'][root] = str(e)
            continue
        for root in roots:
            if root in got:
                result['fetched'] += 1
            else:
                result['missing'][root] = (
                    f'the archive of program {proposal} visit {visit} has no '
                    f'prediction for it')
    return result
