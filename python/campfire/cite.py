"""``campfire cite`` and ``Campfire.cite()`` (P5-3, docs/design-data-releases.md §4.5).

One DOI per data release; proprietary programs are described, not cited. The
sentence a reader puts in a paper is built from the release record (the files
route's answer, kept as ``release.json`` in a pulled release), the programs the
reader holds in it, and the roster the release routes answer per caller in
``programs`` (``dr_program_roster``: ``{slug: {is_public, spectra,
cfpipe_versions, crds_contexts}}`` for the programs public at the freeze, the
set the DOI record holds, plus the reader's own proprietary ones; never stored
on the release row, which every account reads whole)::

    CAMPFIRE DR2026.1 (doi:10.5281/zenodo.123456), plus programs ember and
    zenith reduced with cfpipe 0.7.0 and CRDS context jwst_1535.pmap as in
    that release.

Work from live has no citation: generate a provenance file instead
(``campfire provenance``).
"""
from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

LIVE_SENTENCE = "live: generate a provenance file (campfire provenance) and cite the next data release"


def roster(release: dict) -> dict:
    """The per-program roster of a release record as this reader was
    answered it (``programs``), ``{}`` when the record carries none."""
    r = release.get('programs')
    return dict(r) if isinstance(r, dict) else {}


def reader_programs(release: dict) -> list[str]:
    """The programs whose files the reader holds: the ``spectra`` stream's
    scopes of the files route's answer (``release.json`` in a pulled tree)."""
    files = release.get('files') or []
    return sorted({f.get('scope') for f in files if f.get('stream') == 'spectra' and f.get('scope')})


def _join(names: Iterable[str]) -> str:
    names = list(names)
    if len(names) <= 1:
        return ''.join(names)
    return ', '.join(names[:-1]) + ' and ' + names[-1]


def _union(roster_: dict, programs: Iterable[str], key: str) -> list[str]:
    out: set[str] = set()
    for p in programs:
        for v in (roster_.get(p) or {}).get(key) or []:
            if v:
                out.add(str(v))
    return sorted(out)


def versions_from_store(store, programs: Iterable[str]) -> dict:
    """The distinct cfpipe versions and CRDS contexts of a store's spectra in
    *programs* (exact for a pulled release; the roster is the fallback)."""
    programs = sorted(set(programs))
    if not programs:
        return {'cfpipe_versions': [], 'crds_contexts': []}
    marks = ','.join('?' * len(programs))
    rows = store._conn.execute(
        f"SELECT DISTINCT cfpipe_version, crds_context FROM spectra WHERE program_slug IN ({marks})",
        programs).fetchall()
    return {'cfpipe_versions': sorted({r[0] for r in rows if r[0]}),
            'crds_contexts': sorted({r[1] for r in rows if r[1]})}


def cite_sentence(release: dict, programs: Optional[Iterable[str]] = None, *,
                  versions_of: Optional[Callable[[list[str]], dict]] = None) -> str:
    """The citation sentence for *release* as read through *programs* (default:
    every program the reader holds in it). A program the roster marks public
    is covered by the DOI; the others are described with the pipeline
    versions they were reduced with: *versions_of(programs)* when given (the
    pulled store's own rows, :func:`versions_from_store`), else the roster's
    union over those programs."""
    tag = release.get('tag') or '?'
    doi = release.get('doi')
    head = f"CAMPFIRE {tag} (doi:{doi})" if doi else f"CAMPFIRE {tag} (unpublished: no DOI yet)"
    held = reader_programs(release)
    chosen = sorted(set(programs)) if programs is not None else held
    if programs is not None and held:
        chosen = [p for p in chosen if p in held]
    r = roster(release)
    proprietary = [p for p in chosen if p in r and not r[p].get('is_public')]
    if not proprietary:
        return head + "."
    if versions_of is not None:   # the pulled store's own rows: the authority when there is one
        versions = versions_of(proprietary) or {}
        cfpipe = list(versions.get('cfpipe_versions') or [])
        crds = list(versions.get('crds_contexts') or [])
    else:
        cfpipe = _union(r, proprietary, 'cfpipe_versions')
        crds = _union(r, proprietary, 'crds_contexts')
    how = []
    if cfpipe:
        how.append(f"cfpipe {_join(cfpipe)}")
    if crds:
        how.append(f"CRDS context {_join(crds)}")
    noun = "program" if len(proprietary) == 1 else "programs"
    reduced = f" reduced with {' and '.join(how)}" if how else ""
    return f"{head}, plus {noun} {_join(proprietary)}{reduced} as in that release."


def _day(value) -> str:
    if not value:
        return '—'
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        return str(value)[:10]


def write_readme(root: Path, release: dict, *, versions_of: Optional[Callable[[list[str]], dict]] = None) -> Path:
    """``README.md`` at the top of a pulled release tree: the citation sentence
    and the facts behind it (design §4.6: every pulled DR tree carries the
    citation). Rewritten on every load; never read back."""
    from ._version import __version__

    tag = release.get('tag') or root.name
    held = reader_programs(release)
    r = roster(release)
    public = [p for p in held if (r.get(p) or {}).get('is_public')]
    private = [p for p in held if p in r and not r[p].get('is_public')]
    counts = release.get('counts_loaded') or {}
    lines = [f"# CAMPFIRE {tag}", "",
             "Cite this release as:", "",
             f"    {cite_sentence(release, versions_of=versions_of)}", ""]
    if release.get('doi'):
        lines.append(f"DOI: {release['doi']}")
    lines += [f"Frozen: {_day(release.get('frozen_at'))}. Published: {_day(release.get('published_at'))}. "
              f"Release format v{release.get('format_version') or 1}.",
              f"Loaded here: {_day(release.get('loaded_at'))} by campfire-python {__version__}.", ""]
    if held:
        lines.append(f"Programs held in this copy: {', '.join(held)}.")
        if public or private:
            lines.append(f"  public, on the DOI record: {', '.join(public) or 'none'}; "
                         f"proprietary, described in the sentence: {', '.join(private) or 'none'}.")
        lines.append("")
    if counts:
        lines += ["Rows loaded: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ".", ""]
    lines += ["Open it:", "",
              "    from campfire import Campfire",
              f"    cf = Campfire(dr={tag!r})",
              f"    cf.cite()                         # this sentence, for the programs you select",
              "",
              f"Bytes: `campfire pull --dr {tag} --obs ... | --program ... | --all` lands the",
              "release's archived files under products/. The live tree is never touched.", ""]
    if release.get('known_issues'):
        lines += ["Known issues:", "", textwrap.indent(str(release['known_issues']).strip(), "    "), ""]
    if release.get('notes'):
        lines += ["Notes:", "", textwrap.indent(str(release['notes']).strip(), "    "), ""]
    lines.append(f"Written by campfire pull --dr on {datetime.now(timezone.utc).date().isoformat()}; "
                 f"regenerated on every load.")
    path = root / 'README.md'
    tmp = path.with_name('README.md.part')
    tmp.write_text("\n".join(lines) + "\n", encoding='utf-8')
    tmp.replace(path)
    return path
