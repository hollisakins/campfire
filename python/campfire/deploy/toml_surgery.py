"""Comment-preserving single-section updates to the data-management TOMLs.

``campfire config pull`` regenerates individual ``[section]``s of
programs.toml / observations.toml / fields.toml from the cloud registry. The
files are hand-crafted — comments, ordering, spacing all carry meaning to the
reducers — so a whole-file ``toml.dump`` is unacceptable. tomlkit lets us
replace exactly one top-level section (in place, keeping its position) and
leave every other byte of the file alone. Comments *inside* a replaced section
are necessarily lost — the cloud holds values, not prose — which is the
expected cost of taking the cloud's version of that section.

Reading for comparison purposes must NOT use this module: hashes are computed
from the plain-``tomllib`` parse (``config_sync.load_sections``) so local and
cloud state hash identically.
"""

from __future__ import annotations

from pathlib import Path

import tomlkit


def _to_item(value):
    """Plain python value -> tomlkit item, rendering dicts as real ``[a.b]``
    sub-tables (not inline tables) and lists-of-dicts as ``[[a.b]]`` arrays of
    tables, matching how the files are written by hand."""
    if isinstance(value, dict):
        table = tomlkit.table()
        for k, v in value.items():
            table[k] = _to_item(v)
        return table
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        aot = tomlkit.aot()
        for v in value:
            aot.append(_to_item(v))
        return aot
    return tomlkit.item(value)


def _salvage_trailing_comments(item) -> list:
    """Pop trailing standalone comment/blank lines off a (sub-)table's body.

    A comment sitting immediately ABOVE the next section's header belongs,
    counterintuitively, to the still-open previous table — TOML has no other
    place to put it. Deleting that table must not delete the next section's
    introduction, so the trailing prose is lifted out for re-attachment.
    """
    if isinstance(item, tomlkit.items.AoT):
        return _salvage_trailing_comments(item[-1]) if len(item) else []
    if not isinstance(item, tomlkit.items.Table):
        return []
    body = item.value.body
    salvaged = []
    while body and body[-1][0] is None:
        salvaged.insert(0, body.pop()[1])
    # Pure whitespace with no comment isn't worth re-attaching.
    if not any(isinstance(i, tomlkit.items.Comment) for i in salvaged):
        return []
    return salvaged


def _merge_table(table, new: dict) -> None:
    """Update a tomlkit table in place to match *new*, key by key.

    Replacing a table object wholesale would also discard the standalone
    comment lines tomlkit stores in its body (see
    :func:`_salvage_trailing_comments`). Key-wise updates leave body comments
    untouched; only keys that actually changed move, and comments trailing a
    deleted sub-table are re-attached to the parent so the next section keeps
    its introduction.
    """
    for k in [k for k in table.keys() if k not in new]:
        salvaged = _salvage_trailing_comments(table[k])
        del table[k]
        for item in salvaged:
            table.value.append(None, item)
    for k, v in new.items():
        existing = table.get(k)
        if isinstance(existing, tomlkit.items.Table) and isinstance(v, dict):
            _merge_table(existing, v)
        else:
            table[k] = _to_item(v)


def set_section(path: Path, name: str, section: dict) -> None:
    """Replace (or append) the top-level ``[name]`` section of *path*.

    An existing section keeps its position in the file (and the comments in
    its body that it can keep — values come from the cloud, prose stays where
    the keys it annotates still exist); a new one is appended. The file (and
    its parent directory) is created if missing.
    """
    path = Path(path)
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    existing = doc.get(name)
    if isinstance(existing, tomlkit.items.Table):
        _merge_table(existing, section)
    else:
        doc[name] = _to_item(section)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))
