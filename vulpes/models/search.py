"""The search model.

`Search.parse` reads an `ArctosQuery` once and raises every `BadQueryError` the
grammar can raise. Each engine translates the parsed `Search`, so no engine
re-reads the raw rows.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, NamedTuple

from vulpes.models.query import ArctosQuery
from vulpes.synonyms import Expansion, Names


class Rank(NamedTuple):
    id: str
    label: str
    column: str
    """The dump column holding this rank."""


# Highest first: the form's render order and the rank chain. `scientific_name` is
# not a rank, it is handled where it is used.
RANKS = (
    Rank("phylum", "Phylum", "phylum"),
    Rank("class", "Class", "phylclass"),
    Rank("order", "Order", "phylorder"),
    Rank("family", "Family", "family"),
    Rank("subfamily", "Subfamily", "subfamily"),
    Rank("genus", "Genus", "genus"),
    Rank("species", "Species", "species"),
)
RANK_COLUMNS = {r.id: r.column for r in RANKS}

# Detection attribute types roll up into a flat keyword field on the record.
DETECTION_FIELDS = {
    "detected": "detected",
    "not detected": "not_detected",
    "examined for": "examined_for",
    "not examined for": "not_examined_for",
}

# Only the keyword part fields: the identifier fields are not indexed, and the
# text fields answer a different kind of question.
PART_FIELDS = ("part_name", "disposition", "condition", "part_barcode")

PER_PAGE = 100


class BadQueryError(ValueError):
    """A row outside its grammar. The client never emits one, so it is a
    hand-edited URL, and salvaging it would answer a search nobody asked for."""


def split_pipes(raw: str) -> list[str]:
    """Split on unescaped `|`. A literal `|` is written `\\|` and a literal `\\`
    is written `\\\\`. Each segment is trimmed after the split, not before it."""
    out: list[str] = []
    cur: list[str] = []
    chars = iter(raw)
    for c in chars:
        if c == "\\":
            cur.append(next(chars, ""))
        elif c == "|":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    out.append("".join(cur))
    return [s.strip() for s in out]


_BARE = 2

NULL_RELATIONSHIP = "null"
"""The relationship a taxon row names for records related to nothing."""
"""The fields of a taxon row that names only `rank|name`. A longer row holds
the relationships, then two fields per Related taxon."""


def _check_rank(rank: str) -> None:
    if rank != "scientific_name" and rank not in RANK_COLUMNS:
        msg = f"no rank named `{rank}`"
        raise BadQueryError(msg)


def _describe_taxon(rank: str, name: str, synonyms: Names) -> str:
    text = name if rank == "scientific_name" else f"{rank} {name}"
    found = synonyms.get((rank, name), ())
    if found is None:
        return f"{text} (synonyms unavailable)"
    if found:
        return f"{text} (or its synonym{'s' if len(found) > 1 else ''} {', '.join(found)})"
    return text


@dataclass(frozen=True)
class TaxonRow:
    rank: str
    name: str
    """Empty when the row constrains the relation only: "anything that is a
    parasite of a Sorex" names no taxon on the specimen itself."""
    relationships: tuple[str, ...]
    related: tuple[tuple[str, str], ...]
    """`(rank id, name)` pairs, each rank already checked."""
    unrelated: bool = False
    """The relationships named `null`: the row also takes records with no
    relationship at all. It ORs with the other relationships, like they do
    with each other, and is not one of `relationships`."""

    @classmethod
    def parse(cls, raw: str, known: Collection[str]) -> "TaxonRow":
        """`rank|name`, `rank|name|relationships`, or that followed by a
        `rank|name` pair per Related taxon. The relationships stay `;`-joined in
        the third segment, because the segment count identifies the shape.
        `null` among them asks for records related to nothing.

        The searched name may be empty when the row names a relationship or a
        Related taxon; segment 0 is then ignored rather than checked."""
        segments = split_pipes(raw)
        n = len(segments)
        if n < _BARE or (n > _BARE and n % 2 == 0):
            msg = f"`taxon` has {n} fields, not 2 or 3 + 2 per related taxon"
            raise BadQueryError(msg)

        related = tuple(zip(segments[3::2], segments[4::2], strict=True))
        for rank, name in related:
            if not name:
                msg = "a related taxon carries an empty name"
                raise BadQueryError(msg)
            _check_rank(rank)

        kinds = tuple(k for k in (k.strip() for k in segments[2].split(";")) if k) if n > _BARE else ()
        unrelated = any(k.lower() == NULL_RELATIONSHIP for k in kinds)
        relationships = tuple(k for k in kinds if k.lower() != NULL_RELATIONSHIP)
        if related and unrelated and not relationships:
            msg = "`taxon` names a related taxon for `null`, which relates to nothing"
            raise BadQueryError(msg)
        for kind in relationships:
            if kind not in known:
                msg = f"no relationship named `{kind}`"
                raise BadQueryError(msg)

        rank, name = segments[0], segments[1]
        if name or not (relationships or related or unrelated):
            if not name:
                msg = "`taxon` carries an empty name"
                raise BadQueryError(msg)
            _check_rank(rank)
        return cls(rank, name, relationships, related, unrelated)

    @property
    def constrains_relations(self) -> bool:
        return bool(self.relationships or self.related)

    def taxa(self) -> Iterable[tuple[str, str]]:
        """Every `(rank, name)` the row searches for, its own and the Related ones."""
        if self.name:
            yield self.rank, self.name
        yield from self.related

    def describe(self, synonyms: Names) -> str:
        """This row in plain English, for `Search.description`."""
        text = _describe_taxon(self.rank, self.name, synonyms) if self.name else "any record"
        if self.relationships:
            text = f"{text} with relationship {' or '.join(self.relationships)}"
        if self.related:
            text = f"{text} to {' or '.join(_describe_taxon(rank, name, synonyms) for rank, name in self.related)}"
        if self.unrelated:
            text = f"{text}{', or' if self.constrains_relations else ''} with no relationships"
        return text


@dataclass(frozen=True)
class AttrRow:
    raw: str
    """The row as the request sent it. The summary labels its count with it."""
    type: str
    values: tuple[str, ...]
    negated: bool
    any: bool = False
    """The row is `null` or `!null` and names no type: it asks whether the
    record has any row at all. `null` is the negated form, "has none"."""

    @classmethod
    def parse(cls, raw: str, field: str = "attr") -> "AttrRow":
        """`[!]type|value|value…`. The `!` sits before the escaped type and is not
        itself escaped. A type with no value asks only whether the record has it.
        `null` asks for records with no row, `!null` for those with any."""
        negated = raw.startswith("!")
        atype, *values = split_pipes(raw.removeprefix("!"))
        values = [v for v in values if v]
        if atype.lower() == "null":
            if values:
                msg = f"`{field}` row `null` takes no values"
                raise BadQueryError(msg)
            return cls(raw, atype, (), negated=not negated, any=True)
        if not atype:
            msg = f"`{field}` carries an empty type"
            raise BadQueryError(msg)
        return cls(raw, atype, tuple(values), negated)

    def describe(self, noun: str = "attributes") -> str:
        """`sex male or female`, `no sex recorded`, `detected recorded`, and for
        `null` rows `no attributes` or `any attributes`."""
        if self.any:
            return f"{'no' if self.negated else 'any'} {noun}"
        prefix = "no " if self.negated else ""
        if not self.values:
            return f"{prefix}{self.type} recorded"
        return f"{prefix}{self.type} {' or '.join(self.values)}"

    @classmethod
    def parse_part(cls, raw: str) -> "AttrRow":
        row = cls.parse(raw, "part")
        if not row.any and row.type not in PART_FIELDS:
            msg = f"`part` names `{row.type}`, which is not a searchable part field"
            raise BadQueryError(msg)
        return row


def _present(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(v for v in (v.strip() for v in values or ()) if v)


def _one(value: str | None) -> str | None:
    return (value or "").strip() or None


@dataclass(frozen=True)
class Search:
    """A search, parsed once. Every field is trimmed, non-blank, and checked.
    `tab` names a relationship the schema lists."""

    taxa: tuple[TaxonRow, ...]
    attrs: tuple[AttrRow, ...]
    attr_op: Literal["and", "or"]
    parts: tuple[AttrRow, ...]
    prefixes: tuple[str, ...]
    countries: tuple[str, ...]
    states: tuple[str, ...]
    from_: date | None
    to: date | None
    method: str | None
    """Qualifies the `attr` rows that are not negated. With a negated row it
    is dropped, as the form does: "no detection by this method" would still
    match a specimen whose positive was found another way."""
    locality: str | None
    collector: str | None
    tab: str | None
    page: int
    cols: tuple[str, ...]
    """The columns asked for, unchecked: each engine knows its own columns."""
    expand: bool = False
    """The request asked for synonyms. The engine looks them up into `synonyms`."""
    synonyms: Expansion = field(default_factory=Expansion)

    @classmethod
    def parse(cls, query: ArctosQuery, known: Collection[str]) -> "Search":
        """`known` is the relationships the schema lists."""
        tab = _one(query.tab)
        if tab is not None and tab not in known:
            msg = f"no relationship named `{tab}`"
            raise BadQueryError(msg)
        attrs = tuple(AttrRow.parse(raw) for raw in _present(query.attr))
        return cls(
            taxa=tuple(TaxonRow.parse(raw, known) for raw in _present(query.taxon)),
            attrs=attrs,
            attr_op=query.attr_op,
            parts=tuple(AttrRow.parse_part(raw) for raw in _present(query.part)),
            prefixes=_present(query.prefix),
            countries=_present(query.country),
            states=_present(query.state),
            from_=query.from_,
            to=query.to,
            method=None if any(row.negated for row in attrs) else _one(query.method),
            locality=_one(query.locality),
            collector=_one(query.collector),
            tab=tab,
            page=query.page or 1,
            cols=_present((query.cols or "").split(",")),
            expand=query.synonyms,
        )

    @property
    def constrains_relations(self) -> bool:
        return any(row.constrains_relations for row in self.taxa)

    def names(self, rank: str, name: str) -> tuple[str, ...]:
        """`name` and the synonyms it also matches at `rank`."""
        return (name, *(self.synonyms.names.get((rank, name)) or ()))

    def relationships(self) -> tuple[str, ...]:
        """The relationships the rows name, deduplicated, in the order they were
        named. A row naming a Related taxon and no relationship names nothing
        here: it asks for any relationship that reaches that taxon."""
        out: dict[str, str] = {}
        for row in self.taxa:
            for kind in row.relationships:
                out.setdefault(kind.lower(), kind)
        return tuple(out.values())

    def _describe_filters(self) -> list[str]:
        """The taxon, attribute and part rows, each as a clause."""
        parts: list[str] = []
        if self.taxa:
            parts.append(", or ".join(row.describe(self.synonyms.names) for row in self.taxa))
        if self.attrs:
            rows = f" {self.attr_op} ".join(row.describe() for row in self.attrs)
            parts.append(f"with {rows}" + (f' by a method containing "{self.method}"' if self.method else ""))
        parts.extend(f"with {row.describe('parts')}" for row in self.parts if row.any)
        if fields := [row for row in self.parts if not row.any]:
            parts.append("having parts with " + " and ".join(row.describe() for row in fields))
        return parts

    def _describe_scope(self) -> list[str]:
        """The collection, place, collector and dates, each as a clause."""
        dates = {
            (True, True): f"collected {self.from_} to {self.to}",
            (True, False): f"collected from {self.from_}",
            (False, True): f"collected up to {self.to}",
        }
        clauses = (
            (self.prefixes, f"in {', '.join(self.prefixes)}"),
            (self.countries, f"from {', '.join(self.countries)}"),
            (self.states, f"in {', '.join(self.states)}"),
            (self.locality, f'at "{self.locality}"'),
            (self.collector, f"collected by {self.collector}"),
            (self.from_ or self.to, dates.get((self.from_ is not None, self.to is not None), "")),
        )
        return [text for present, text in clauses if present]

    def description(self) -> str | None:
        """One sentence restating the search in plain English, for a researcher
        to confirm they searched what they meant to. `None` for an empty form."""
        parts = [*self._describe_filters(), *self._describe_scope()]
        if not parts:
            return None
        sentence = " ".join(parts)
        return sentence[0].upper() + sentence[1:] + "."
