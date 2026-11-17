"""The storage-plane vocabulary: modality → instrument → product.

``campfire pull`` and ``campfire push`` take a modality subcommand
(``spectroscopy`` / ``spec``, ``imaging`` / ``im``) because the way data is
selected follows the modality, not the instrument: spectroscopy is chosen by
observation, program and grating; imaging by field, filter and tile. An
instrument is an option within a modality (``--instrument``, every instrument
of the modality by default), so a new instrument (MIRI, HST) is a new entry
here, never a new flag. A product name means the same thing whichever verb
uses it, and ``exposures`` is unambiguous because the modality says which.

Rules both verbs follow:

* the scope (``--obs`` / ``--program`` / ``--field`` / ``--all``) is a place;
* ``--products`` says what, defaulting to the finals for ``pull`` and to
  everything for ``push``;
* narrowers (``--grating``, ``--filters``, ``--tile``) only narrow, and a
  narrower that matches nothing is an error, never a note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import click


@dataclass(frozen=True)
class ProductKind:
    """One user-facing product name.

    ``types`` are the layout product types ``pull`` fetches for it (``push``
    also uploads the derived files the pipeline writes beside them; see
    ``campfire.deploy.push``). ``pullable`` is False for a product the client
    download engine cannot place yet.
    """
    types: tuple[str, ...]
    final: bool = False
    pullable: bool = True


@dataclass(frozen=True)
class Modality:
    name: str
    alias: str
    #: instrument → product name → kind, in display order.
    instruments: dict[str, dict[str, ProductKind]] = field(default_factory=dict)

    def product_names(self, instruments: Optional[Iterable[str]] = None) -> list[str]:
        names: list[str] = []
        for inst in instruments or self.instruments:
            for name in self.instruments[inst]:
                if name not in names:
                    names.append(name)
        return names

    def finals(self, instruments: Optional[Iterable[str]] = None) -> list[str]:
        return [n for n in self.product_names(instruments)
                if any(self.instruments[i].get(n, _NONE).final
                       for i in (instruments or self.instruments))]

    def kinds(self, products: Iterable[str],
              instruments: Optional[Iterable[str]] = None) -> list[ProductKind]:
        out = []
        for inst in instruments or self.instruments:
            for name in products:
                kind = self.instruments[inst].get(name)
                if kind is not None:
                    out.append(kind)
        return out

    def pull_types(self, products: Iterable[str],
                   instruments: Optional[Iterable[str]] = None) -> list[str]:
        types: list[str] = []
        for kind in self.kinds(products, instruments):
            for t in kind.types:
                if t not in types:
                    types.append(t)
        return types


_NONE = ProductKind(types=())

SPECTROSCOPY = Modality("spectroscopy", "spec", {
    "nirspec": {
        "spectra": ProductKind(("nirspec_spec",), final=True),
        "exposures": ProductKind(("nirspec_spectrum_exposure",)),
        "rates": ProductKind(("nirspec_rate",), pullable=False),
    },
})

IMAGING = Modality("imaging", "im", {
    "nircam": {
        "mosaics": ProductKind(("nircam_mosaic",), final=True),
        "exposures": ProductKind(("nircam_exposure",)),
        "expmaps": ProductKind(("nircam_expmap",)),
    },
})

MODALITIES = {m.name: m for m in (SPECTROSCOPY, IMAGING)}


# ---------------------------------------------------------------------------
# Option parsing
# ---------------------------------------------------------------------------

def _split(values: Sequence[str]) -> list[str]:
    """Accept both ``--products a b`` and ``--products a,b``."""
    out: list[str] = []
    for v in values or ():
        for part in str(v).split(","):
            part = part.strip().lower()
            if part and part not in out:
                out.append(part)
    return out


def resolve_instruments(modality: Modality, values: Sequence[str]) -> list[str]:
    chosen = _split(values)
    if not chosen:
        return list(modality.instruments)
    unknown = [v for v in chosen if v not in modality.instruments]
    if unknown:
        raise click.UsageError(
            f"{modality.name} has no instrument {', '.join(unknown)}; "
            f"choose from: {', '.join(modality.instruments)}.")
    return chosen


def resolve_products(modality: Modality, instruments: Sequence[str], values: Sequence[str],
                     *, verb: str) -> list[str]:
    """The product names of a pull or push. ``pull`` defaults to the finals,
    ``push`` to everything; a name not offered by the chosen instruments, or one
    ``pull`` cannot fetch, is a usage error."""
    available = modality.product_names(instruments)
    chosen = _split(values)
    if not chosen:
        if verb == "pull":
            return modality.finals(instruments)
        return available
    unknown = [v for v in chosen if v not in available]
    if unknown:
        raise click.UsageError(
            f"{modality.name} has no product {', '.join(unknown)}; "
            f"choose from: {', '.join(available)}.")
    if verb == "pull":
        blocked = [n for n in chosen
                   if not any(k.pullable for k in modality.kinds([n], instruments))]
        if blocked:
            raise click.UsageError(
                f"{', '.join(blocked)} cannot be pulled yet; "
                f"pull one of: {', '.join(n for n in available if n not in blocked)}.")
    return chosen


def normalize_names(values: Sequence[str]) -> list[str]:
    """Narrower values (filters, tiles, gratings) as given, comma or space
    separated, without duplicates; filters are compared case-insensitively by
    the callers."""
    out: list[str] = []
    for v in values or ():
        for part in str(v).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


# ---------------------------------------------------------------------------
# Mosaic tiles
# ---------------------------------------------------------------------------

def mosaic_tile(field_name: str, basename: str) -> Optional[str]:
    """The tile a mosaic filename names, or None.

    Mosaic names follow ``mosaic_nircam_<filter>_<field>_<scale>_<tile>[_<epoch>]_<ext>``
    (``[nircam.mosaic].mosaic_name``); the field is known, the filter and pixel
    scale carry no underscore, and the tile is the next segment.
    """
    m = re.match(rf"^mosaic_nircam_[^_]+_{re.escape(field_name)}_[^_]+_([^_.]+)", basename)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# The verb group: aliases + the old flag syntax answered with help
# ---------------------------------------------------------------------------

_SPEC_FLAGS = {"--obs", "--program", "--grating", "--source-ids"}
_IMAGING_FLAGS = {"--filters", "--filter"}
_NEUTRAL_FLAGS = {"--field", "--all", "--dr", "--intermediate"}


def _flag(token: str) -> str:
    return token.split("=", 1)[0]


def migration_hint(verb: str, args: Sequence[str]) -> str:
    """Rewrite an old-syntax invocation into the new one(s)."""
    flags = {_flag(a) for a in args if a.startswith("--")}
    modalities = []
    if flags & _SPEC_FLAGS:
        modalities.append(SPECTROSCOPY)
    if flags & _IMAGING_FLAGS:
        modalities.append(IMAGING)
    if not modalities:
        modalities = [SPECTROSCOPY, IMAGING]

    lines = [f"`campfire {verb}` now takes a modality first: "
             f"`campfire {verb} spectroscopy` (spec) or `campfire {verb} imaging` (im).", ""]
    if len(modalities) == 2 and "--field" in flags:
        lines.append("A field holds both spectroscopy and imaging; pick one:")
    else:
        lines.append("Try:")
    for mod in modalities:
        rewritten: list[str] = []
        it = iter(args)
        for a in it:
            f = _flag(a)
            if f == "--intermediate":
                names = mod.product_names()
                if verb == "pull":
                    names = [n for n in names
                             if any(k.pullable for k in mod.kinds([n]))]
                rewritten += ["--products", ",".join(names)]
            elif f == "--filter":
                rewritten.append(a.replace("--filter", "--filters", 1))
            else:
                rewritten.append(a)
        lines.append(f"  campfire {verb} {mod.alias} " + " ".join(rewritten))
    lines += ["", f"See `campfire {verb} spec --help` and `campfire {verb} im --help`."]
    return "\n".join(lines)


class ModalityGroup(click.Group):
    """A verb group whose subcommands are the modalities.

    Accepts each modality's short alias (``spec`` / ``im``), lists it in the
    help, and answers the pre-0.8 flag syntax (``campfire pull --obs X``) with
    the equivalent new command instead of click's bare "no such option".
    """

    def __init__(self, *args, legacy_flags: Iterable[str] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self.legacy_flags = set(legacy_flags)
        self.aliases: dict[str, str] = {}

    def add_modality(self, cmd: click.Command, alias: str) -> None:
        self.add_command(cmd)
        self.aliases[alias] = cmd.name

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self.aliases.get(cmd_name, cmd_name))

    def resolve_command(self, ctx, args):
        name, cmd, rest = super().resolve_command(ctx, args)
        return (cmd.name if cmd else name), cmd, rest

    def format_commands(self, ctx, formatter):
        reverse = {v: k for k, v in self.aliases.items()}
        rows = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            label = f"{name} ({reverse[name]})" if name in reverse else name
            rows.append((label, cmd.get_short_help_str(limit=60)))
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)

    def parse_args(self, ctx, args):
        names = set(self.commands) | set(self.aliases)
        for token in args:
            if token in names:
                break
            if token.startswith("--") and _flag(token) in self.legacy_flags:
                raise click.UsageError(migration_hint(ctx.info_name or "pull", args), ctx)
        return super().parse_args(ctx, args)
