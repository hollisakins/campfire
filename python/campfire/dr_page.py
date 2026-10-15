"""Data releases P6b-3: the release page document (design D-P6-3).

A directory holding ``README.md`` (the markdown shown on ``/releases/<tag>``)
plus its figures is pushed with ``campfire dr page push <tag> <dir>``: the
files go to the OSN data bucket under the release's immutable prefix and
``dr_page_set`` records the markdown on the release row and the figures as
manifest rows of component ``page``. The page is authored while the release
is unpublished and frozen with it; the database refuses a published tag, and
the CLI checks the row before uploading anything.

Keys are **content-addressed**: ``dr/<tag>/page/<sha16>/<name>``, where
``<sha16>`` is the first 16 hex characters of the asset's sha256, computed
from the bytes that are uploaded. No key is ever overwritten or
deleted-then-replaced: a changed figure gets a new key, so nothing ever
writes a key a published release holds. Every PUT is conditional
(``If-None-Match: *``); on 412 the object there is read back and hashed:
the same bytes are reused, different bytes fail the push (a collision on the
prefix with different content is a corruption signal, never something to
overwrite). Deletion authority comes from the database: ``dr_page_set``
returns ``removed_keys``, the keys of the page rows it retired, and the CLI
deletes exactly those after the call succeeds. Orphans (uploaded, then the
call refused) are never referenced and are swept only by ``dr purge``.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_ASSETS = 50
MAX_MARKDOWN_BYTES = 1024 * 1024
README = 'README.md'
EDITABLE_STATES = frozenset({'frozen', 'failed', 'exporting', 'exported', 'copying', 'ready'})


class PageError(ValueError):
    """A page directory or release that cannot be pushed (the message says why)."""


@dataclass
class PageAsset:
    name: str
    path: Path
    size_bytes: int
    content_hash: str          # sha256:<hex>
    content_type: str
    data: bytes = field(repr=False, default=b'')

    @property
    def sha16(self) -> str:
        return self.content_hash[len('sha256:'):][:16]

    def storage_key(self, tag: str) -> str:
        return page_key(tag, self.content_hash, self.name)


@dataclass
class PageDocument:
    markdown: str
    assets: list[PageAsset] = field(default_factory=list)


def page_prefix(tag: str) -> str:
    return f"dr/{tag}/page/"


def page_key(tag: str, content_hash: str, name: str) -> str:
    """``dr/<tag>/page/<sha16>/<name>`` for a ``sha256:<hex>`` content hash."""
    if not re.match(r'^sha256:[0-9a-f]{64}$', content_hash or ''):
        raise PageError(f"page asset {name} needs a sha256 content hash, got {content_hash!r}")
    return f"{page_prefix(tag)}{content_hash[7:23]}/{name}"


def asset_name(tag: str, storage_key: str) -> str:
    """The asset name of a page key (``<sha16>/<name>`` after the prefix)."""
    prefix = page_prefix(tag)
    rest = storage_key[len(prefix):] if storage_key.startswith(prefix) else storage_key
    return rest.split('/', 1)[1] if '/' in rest else rest


def collect_page_dir(directory: str | os.PathLike) -> PageDocument:
    """Read ``README.md`` and every other regular file of *directory* (no
    subdirectories, no hidden files), each read once and hashed from the
    bytes that will be uploaded. Validates names and sizes the way
    ``dr_page_set`` does, so a bad directory fails before any upload."""
    d = Path(directory)
    if not d.is_dir():
        raise PageError(f"{d} is not a directory")
    readme = d / README
    if not readme.is_file():
        raise PageError(f"{d} has no {README}")
    markdown = readme.read_text(encoding='utf-8')
    if not markdown.strip():
        raise PageError(f"{readme} is empty")
    if len(markdown.encode('utf-8')) > MAX_MARKDOWN_BYTES:
        raise PageError(f"{readme} exceeds 1 MB")
    assets: list[PageAsset] = []
    for p in sorted(d.iterdir()):
        if p.name == README or p.name.startswith('.'):
            continue
        if not p.is_file():
            raise PageError(f"{p} is not a regular file (subdirectories are not pushed)")
        if not re.match(NAME_RE, p.name):
            raise PageError(f"bad asset name {p.name!r} (letters, digits, . _ -; no leading dot)")
        data = p.read_bytes()
        size = len(data)
        if size < 1 or size > MAX_ASSET_BYTES:
            raise PageError(f"{p.name} is {size:,} bytes; assets must be between 1 byte and 25 MB")
        ctype = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
        assets.append(PageAsset(p.name, p, size, 'sha256:' + hashlib.sha256(data).hexdigest(), ctype, data))
    if len(assets) > MAX_ASSETS:
        raise PageError(f"{len(assets)} assets; at most {MAX_ASSETS}")
    return PageDocument(markdown, assets)


def fetch_release_state(client, tag: str) -> str | None:
    rows = (client.table('dr_releases').select('tag,state').eq('tag', tag).limit(1).execute().data) or []
    return rows[0].get('state') if rows else None


def check_editable(client, tag: str) -> str:
    """The release's state, or a PageError when its page cannot be set."""
    state = fetch_release_state(client, tag)
    if state is None:
        raise PageError(f"no release {tag} (or not visible to this account)")
    if state in ('published', 'withdrawn'):
        raise PageError(f"release {tag} is {state}; its page is frozen with it")
    if state not in EDITABLE_STATES:
        raise PageError(f"release {tag} is {state}; set its page once the freeze has landed")
    return state


def fetch_page(client, tag: str) -> dict | None:
    """The release's page: ``{tag, state, page_md, assets: [...]}`` or None
    when the tag is not visible to the caller."""
    rows = (client.table('dr_releases').select('tag,state,page_md')
            .eq('tag', tag).limit(1).execute().data) or []
    if not rows:
        return None
    assets = (client.table('dr_manifest')
              .select('storage_key,content_hash,size_bytes,updated_at')
              .eq('tag', tag).eq('component', 'page').order('storage_key').execute().data) or []
    for a in assets:
        a['name'] = asset_name(tag, a['storage_key'])
    return {'tag': rows[0]['tag'], 'state': rows[0].get('state'), 'page_md': rows[0].get('page_md'), 'assets': assets}


# -- object store helpers ----------------------------------------------------

def _status_code(exc: Exception) -> int | None:
    from campfire.deploy.retain import _status_code as code
    return code(exc)


def _object_sha256(s3, bucket: str, key: str) -> str:
    """``sha256:<hex>`` of the object's bytes (page assets are ≤ 25 MB)."""
    body = s3.get_object(Bucket=bucket, Key=key)['Body']
    h = hashlib.sha256()
    for chunk in iter(lambda: body.read(1 << 20), b''):
        h.update(chunk)
    return 'sha256:' + h.hexdigest()


def _put_asset(s3, bucket: str, tag: str, asset: PageAsset, log) -> str:
    """PUT one asset under If-None-Match; returns 'uploaded' | 'reused'. A 412
    is an object already under that content-addressed key: read back and
    hashed, the same bytes are reused; different bytes fail the push (never
    overwritten, never deleted)."""
    key = asset.storage_key(tag)
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=asset.data, ContentType=asset.content_type, IfNoneMatch='*')
        return 'uploaded'
    except Exception as e:  # noqa: BLE001
        if _status_code(e) != 412:
            raise
    found = _object_sha256(s3, bucket, key)
    if found == asset.content_hash:
        log(f"  {key}: already there (same bytes), reused")
        return 'reused'
    raise PageError(f"{key} already holds different bytes ({found}) than {asset.name} ({asset.content_hash}); "
                    f"a content-addressed key with other content is a corruption signal and is never overwritten")


def push_page(client, s3, bucket: str, tag: str, directory: str | os.PathLike, *,
              dry_run: bool = False, log=None) -> dict:
    """Upload the directory's assets and set the page. Returns the
    ``dr_page_set`` summary (or the plan under ``dry_run``) with ``uploads``
    (per asset: uploaded / reused) and ``deleted`` (the ``removed_keys`` the
    database retired, deleted from the bucket after the call)."""
    say = log or (lambda m: None)
    doc = collect_page_dir(directory)
    state = check_editable(client, tag)
    plan = [{'name': a.name, 'storage_key': a.storage_key(tag), 'content_hash': a.content_hash,
             'size_bytes': a.size_bytes, 'content_type': a.content_type} for a in doc.assets]
    say(f"{tag} ({state}): {len(doc.markdown.encode('utf-8')):,} bytes of markdown, {len(plan)} asset(s)")
    for a in plan:
        say(f"  {a['storage_key']}  {a['size_bytes']:,} B")
    if dry_run:
        return {'tag': tag, 'state': state, 'dry_run': True, 'markdown_bytes': len(doc.markdown.encode('utf-8')),
                'assets': plan}
    uploads = {a.name: _put_asset(s3, bucket, tag, a, say) for a in doc.assets}
    payload = [{'name': a['name'], 'storage_key': a['storage_key'], 'content_hash': a['content_hash'],
                'size_bytes': a['size_bytes'], 'backend': 'osn', 'bucket': 'data'} for a in plan]
    try:
        resp = client.rpc('dr_page_set', {'p_tag': tag, 'p_markdown': doc.markdown, 'p_assets': payload}).execute()
    except Exception as e:  # noqa: BLE001
        left = sorted(a['storage_key'] for a in plan)
        msg = getattr(e, 'message', None) or str(e)
        raise PageError(f"dr_page_set refused after the uploads: {msg}. The uploaded keys stay under "
                        f"{page_prefix(tag)} as unreferenced content-addressed objects (swept only by `dr purge`): "
                        + (', '.join(left) if left else 'none')) from e
    data = resp.data
    if not isinstance(data, dict):
        raise RuntimeError(f"dr_page_set returned an unexpected payload: {data!r}")
    # exactly the keys the database retired in its transaction, nothing else
    removed = [k for k in (data.get('removed_keys') or []) if isinstance(k, str) and k.startswith(page_prefix(tag))]
    for k in removed:
        s3.delete_object(Bucket=bucket, Key=k)
        say(f"  deleted retired {k}")
    data = dict(data)
    data['uploads'] = uploads
    data['deleted'] = removed
    return data


def format_page(page: dict) -> str:
    lines = [f"{page['tag']}: {page.get('state')}"]
    assets = page.get('assets') or []
    if assets:
        lines.append(f"  assets ({len(assets)}):")
        for a in assets:
            lines.append(f"    {a['name']}  {int(a.get('size_bytes') or 0):,} B  {(a.get('content_hash') or '')[:19]}…")
    else:
        lines.append("  assets: none")
    md = page.get('page_md')
    lines.append('')
    lines.append(md if md else '(no page markdown set)')
    return '\n'.join(lines)
