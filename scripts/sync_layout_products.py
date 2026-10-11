#!/usr/bin/env python3
"""Mirror the layout product registry into web/lib/layout-products.json.

``layout/campfire_layout/products.py`` is the single authority for product
types (name, tree, scope keys, subdir / filename / legacy-prefix builders,
bucket, suffix, lifecycle, compression). The web portal mirrors the contract in
``web/lib/layout.ts`` and builds from ``web/`` alone, so the table is committed
as JSON and this script keeps it in step with the python registry (the
``sync_linelist.py`` pattern; the proposal's §3.10 layout parity check).

Builders in the registry are python lambdas over a ``Scope``. They are
serialized as templates with ``{field}`` placeholders (``nirspec/{obs}``) by
evaluating each builder against a Scope whose every field is its own
placeholder, then self-checked against the real builder on a probe scope — so
a builder that is not a plain substitution of scope fields fails the generator
instead of silently producing a template the TS side cannot render.

Also carried: the two ordered suffix-dispatch tables ``bijection.py`` uses to
recover a product from a filename inside a known directory (the TS parser
needs the same order). ``FINAL_PRODUCT_TYPES`` / ``INTERMEDIATE_PRODUCT_TYPES``
are *not* layout data (they live in ``python/campfire/db/store.py``) and are
deliberately not mirrored here.

The script puts the in-repo ``layout/`` first on ``sys.path`` so the committed
JSON always reflects the committed ``products.py`` (never an installed copy);
``campfire_layout`` is stdlib-only, so a bare python3 >= 3.11 suffices (CI).

    python scripts/sync_layout_products.py          # rewrite web/lib/layout-products.json
    python scripts/sync_layout_products.py --check  # exit 1 (with a diff) if it is stale
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYOUT_SRC = ROOT / 'layout'
DST = ROOT / 'web' / 'lib' / 'layout-products.json'

sys.path.insert(0, str(LAYOUT_SRC))

import campfire_layout as L  # noqa: E402
from campfire_layout import bijection  # noqa: E402
from campfire_layout.scope import Scope  # noqa: E402

_PLACEHOLDER = re.compile(r'\{(\w+)\}')
# Shape every template must have: literal text and {field} placeholders only.
_TEMPLATE_SHAPE = re.compile(r'^(?:[^{}]|\{\w+\})*$')

# One concrete value per Scope field for the builder-vs-template self-check.
_PROBE = {
    'obs': 'obs_1', 'field': 'field1', 'filt': 'f444w', 'pid': '1727',
    'data_subdir': 'sub1', 'source_id': 'src1', 'detector': 'nrs1',
    'exposure': 'exp1', 'object_id': '42', 'catalog_id': 'cat1', 'tile': 'ne',
    'pixel_scale': '30mas', 'zoom': 3, 'x': 4, 'y': 5,
}


def _scope_field_names() -> list[str]:
    return [f.name for f in fields(Scope)]


def _template_scope() -> Scope:
    return Scope(**{n: f'{{{n}}}' for n in _scope_field_names()})


def _render(template: str, values: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)


def _templatize(name: str, label: str, builder) -> str | None:
    """Turn a Scope -> str builder into a '{field}' template, or None if absent."""
    if builder is None:
        return None
    template = builder(_template_scope())
    if not _TEMPLATE_SHAPE.match(template):
        raise SystemExit(
            f'sync_layout_products: {name}.{label} builder produced {template!r}, '
            'which is not a plain {field} template')
    used = set(_PLACEHOLDER.findall(template))
    unknown = used - set(_scope_field_names())
    if unknown:
        raise SystemExit(f'sync_layout_products: {name}.{label} references non-Scope fields {sorted(unknown)}')
    # The template must reproduce the builder on a concrete scope.
    probe = Scope(**_PROBE)
    if _render(template, _PROBE) != builder(probe):
        raise SystemExit(
            f'sync_layout_products: {name}.{label} builder is not a pure template '
            f'({template!r} renders {_render(template, _PROBE)!r}, builder gives {builder(probe)!r})')
    return template


def _dispatch_table(name: str, table) -> list[list[str]]:
    out = []
    for suffix, ptype in table:
        spec = L.PRODUCTS.get(ptype)
        if spec is None:
            raise SystemExit(f'sync_layout_products: {name} dispatches to unknown product {ptype!r}')
        if spec.suffix != suffix:
            raise SystemExit(
                f'sync_layout_products: {name} entry {suffix!r} -> {ptype!r} disagrees with '
                f'ProductSpec.suffix {spec.suffix!r}')
        out.append([suffix, ptype])
    return out


def build() -> dict:
    products = []
    for spec in L.PRODUCTS.values():   # registration order (deterministic, groups families)
        products.append({
            'name': spec.name,
            'instrument': spec.instrument.value if spec.instrument is not None else None,
            'tree': spec.tree,
            'lifecycle': spec.lifecycle.value,
            'bucket': spec.bucket,
            'scope_keys': list(spec.scope_keys),
            'subdir': _templatize(spec.name, 'subdir', spec.subdir),
            'filename': _templatize(spec.name, 'filename', spec.filename),
            'suffix': spec.suffix,
            'legacy_prefix': _templatize(spec.name, 'legacy_prefix', spec.legacy_prefix),
            'mirrored': bool(spec.mirrored),
            'scheme_invariant': bool(spec.scheme_invariant),
            'compressed_suffixes': list(spec.compressed_suffixes),
        })
    return {
        '_comment': (
            'GENERATED by scripts/sync_layout_products.py from layout/campfire_layout '
            '(products.py registry + bijection.py suffix-dispatch tables); do not edit. '
            'subdir / filename / legacy_prefix are templates whose {name} placeholders are '
            'Scope field names; products are in registration order; dispatch tables are '
            'ordered most-specific first and must be matched in that order.'
        ),
        'products': products,
        'dispatch': {
            'nirspec_obs_suffixes': _dispatch_table('nirspec_obs_suffixes', bijection._NIRSPEC_OBS_SUFFIXES),
            'nircam_filter_suffixes': _dispatch_table('nircam_filter_suffixes', bijection._NIRCAM_FILTER_SUFFIXES),
        },
    }


def render(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


def main(argv) -> int:
    check = '--check' in argv
    text = render(build())
    current = DST.read_text(encoding='utf-8') if DST.exists() else None
    rel = DST.relative_to(ROOT)
    if current == text:
        print(f'sync_layout_products: {rel} up to date')
        return 0
    if check:
        print(f'sync_layout_products: {rel} is {"missing" if current is None else "out of date"}; '
              'run `python scripts/sync_layout_products.py` and commit the result', file=sys.stderr)
        if current is not None:
            sys.stderr.writelines(difflib.unified_diff(
                current.splitlines(keepends=True), text.splitlines(keepends=True),
                fromfile=f'{rel} (committed)', tofile=f'{rel} (generated)'))
        return 1
    DST.parent.mkdir(parents=True, exist_ok=True)
    DST.write_text(text, encoding='utf-8')
    print(f'sync_layout_products: wrote {rel}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
