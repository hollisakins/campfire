"""Data releases P6b-3: the release page document (design D-P6-3).

A directory holding ``README.md`` (the markdown shown on ``/releases/<tag>``)
plus its figures is pushed with ``campfire dr page push <tag> <dir>``: the
files go to the OSN data bucket under ``dr/<tag>/page/<name>`` (the release's
immutable prefix, the same credentials ``dr export`` uses) and
``dr_page_set`` records the markdown on the release row and the figures as
manifest rows of component ``page``. The page is authored while the release
is unpublished and frozen with it; the database refuses a published tag, and
the CLI checks the row before uploading anything.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_ASSETS = 50
MAX_MARKDOWN_BYTES = 1024 * 1024
README = 'README.md'


class PageError(ValueError):
    """A page directory that cannot be pushed (the message says why)."""


@dataclass
class PageAsset:
    name: str
    path: Path
    size_bytes: int
    content_hash: str          # sha256:<hex>
    content_type: str

    def storage_key(self, tag: str) -> str:
        return page_key(tag, self.name)


@dataclass
class PageDocument:
    markdown: str
    assets: list[PageAsset] = field(default_factory=list)


def page_prefix(tag: str) -> str:
    return f"dr/{tag}/page/"


def page_key(tag: str, name: str) -> str:
    return page_prefix(tag) + name


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return 'sha256:' + h.hexdigest()


def collect_page_dir(directory: str | os.PathLike) -> PageDocument:
    """Read ``README.md`` and every other regular file of *directory* (no
    subdirectories, no hidden files). Validates names and sizes the way
    ``dr_page_set`` does, so a bad directory fails before any upload."""
    import re
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
        size = p.stat().st_size
        if size < 1 or size > MAX_ASSET_BYTES:
            raise PageError(f"{p.name} is {size:,} bytes; assets must be between 1 byte and 25 MB")
        ctype = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
        assets.append(PageAsset(p.name, p, size, _sha256(p), ctype))
    if len(assets) > MAX_ASSETS:
        raise PageError(f"{len(assets)} assets; at most {MAX_ASSETS}")
    return PageDocument(markdown, assets)


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
    prefix = page_prefix(tag)
    for a in assets:
        a['name'] = a['storage_key'][len(prefix):] if a['storage_key'].startswith(prefix) else a['storage_key']
    return {'tag': rows[0]['tag'], 'state': rows[0].get('state'), 'page_md': rows[0].get('page_md'), 'assets': assets}


def push_page(client, s3, bucket: str, tag: str, directory: str | os.PathLike, *,
              dry_run: bool = False, log=None) -> dict:
    """Upload the directory's assets and set the page. Returns the
    ``dr_page_set`` summary (or the plan under ``dry_run``)."""
    say = log or (lambda m: None)
    doc = collect_page_dir(directory)
    row = (client.table('dr_releases').select('tag,state').eq('tag', tag).limit(1).execute().data) or []
    if not row:
        raise PageError(f"no release {tag} (or not visible to this account)")
    state = row[0].get('state')
    if state in ('published', 'withdrawn'):
        raise PageError(f"release {tag} is {state}; its page is frozen with it")
    if state in ('draft', 'freezing'):
        raise PageError(f"release {tag} is {state}; set its page once the freeze has landed")
    plan = [{'name': a.name, 'storage_key': a.storage_key(tag), 'content_hash': a.content_hash,
             'size_bytes': a.size_bytes, 'content_type': a.content_type} for a in doc.assets]
    say(f"{tag} ({state}): {len(doc.markdown.encode('utf-8')):,} bytes of markdown, {len(plan)} asset(s)")
    for a in plan:
        say(f"  {a['storage_key']}  {a['size_bytes']:,} B  {a['content_hash'][:19]}…")
    if dry_run:
        return {'tag': tag, 'state': state, 'dry_run': True, 'markdown_bytes': len(doc.markdown.encode('utf-8')),
                'assets': plan}
    for asset in doc.assets:
        with open(asset.path, 'rb') as f:
            s3.put_object(Bucket=bucket, Key=asset.storage_key(tag), Body=f, ContentType=asset.content_type)
    payload = [{'name': a['name'], 'storage_key': a['storage_key'], 'content_hash': a['content_hash'],
                'size_bytes': a['size_bytes'], 'backend': 'osn', 'bucket': 'data'} for a in plan]
    resp = client.rpc('dr_page_set', {'p_tag': tag, 'p_markdown': doc.markdown, 'p_assets': payload}).execute()
    data = resp.data
    if not isinstance(data, dict):
        raise RuntimeError(f"dr_page_set returned an unexpected payload: {data!r}")
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
