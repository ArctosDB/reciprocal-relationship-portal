from collections.abc import Iterable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResponseModel(BaseModel):
    model_config = ConfigDict(use_attribute_docstrings=True)


class Rank(ResponseModel):
    id: str
    label: str
    field: str


class AttributeType(ResponseModel):
    id: str
    label: str
    description: str
    vocabulary: str | None
    """The code table holding the values, a key of `vocabularies`."""
    units_table: str | None


class VocabValue(ResponseModel):
    value: str
    description: str
    documentation_url: str


class Facet(ResponseModel):
    """An observed value and how many records carry it."""

    value: str
    count: int


class Relation(ResponseModel):
    value: str
    description: str


class GuidPrefix(Facet):
    institution: str
    collection_cde: str
    """The collection's full label, or empty when the code matches none."""


class Sort(ResponseModel):
    id: str
    label: str


class Limits(ResponseModel):
    page_size: int
    """The pager divides by this. The page size is fixed; there is no `per_page`
    parameter, so the client uses this, not `max_per_page`."""
    max_per_page: int
    max_result_window: int
    max_export_rows: int


class Schema(ResponseModel):
    """Everything the search form needs to render itself. Code-table lists are
    complete and carry no counts. Aggregated lists are observed and carry counts."""

    snapshot_date: date
    columns: list[str]
    """Every column a download may name in `cols`, in dump order."""
    default_columns: list[str]
    """The columns of a search page that names none in `cols`. A search that
    constrains relations also gets the related columns, after `guid`."""
    ranks: list[Rank]
    """The ranks that hold values, `scientific_name` first."""
    attribute_types: list[AttributeType]
    part_fields: list[AttributeType]
    """The part fields a `part` row may name, in the same shape as an attribute
    type so the form renders both with one component."""
    vocabularies: dict[str, list[VocabValue]]
    """Every code table a `vocabulary` names, parents before their children."""
    countries: list[Facet]
    states: list[Facet]
    collectors: list[Facet]
    relations: list[Relation]
    guid_prefixes: list[GuidPrefix]
    sorts: list[Sort]
    limits: Limits
    nonpublic_types_dropped: list[str]


class Total(ResponseModel):
    """`relation` is `eq` when `value` is exact, and `gte` when the matches pass
    the count cap, so a client can render "10,000+" instead of a wrong number."""

    value: int
    relation: Literal["eq", "gte"]


class AttrCount(ResponseModel):
    attr: str
    """The `attr` row as the request sent it."""
    count: int


class Summary(ResponseModel):
    """The search as a funnel. Each `attr` row is counted against `context`, not
    `matched`, so the row counts do not sum to `matched`."""

    taxon: int | None = Field(None, exclude_if=lambda taxon: taxon is None)
    """The taxon rows alone, before any other filter narrows them. Absent from
    a related-records table, where `context` is the records the form matched
    and `matched` the distinct records they reach."""
    context: int
    """The records the form selects without its `attr` rows."""
    matched: int
    """The records the whole form selects."""
    attrs: list[AttrCount] = Field(default_factory=list, exclude_if=lambda attrs: not attrs)
    """One per `attr` row, in request order. Absent when there are none."""


class SearchResults(ResponseModel):
    total: Total
    summary: Summary
    column_order: list[str]
    """The field names of each record in display order, with `related_guid` second
    when the search names a relationship."""
    records: list[dict[str, Any]]
    """One document per hit, keyed by field name. Nested fields stay as JSON."""


class TypedFacet(Facet):
    """A `(type, value)` pair and how many records carry it. `type` and `value`
    are what `attr=type|value` (or `part=field|value`) matches."""

    type: str


class Distinct(ResponseModel):
    taxa: int
    """Distinct lowest identified taxa, by `scientific_name`."""
    localities: int
    """Distinct localities across every event of the records."""
    collectors: int
    """Distinct collector agents."""


class Years(ResponseModel):
    min: int
    max: int


class Facets(ResponseModel):
    """The top values by record count, descending. A record holding several
    values counts once under each. An empty list means none recorded."""

    taxa: list[Facet] = Field(default_factory=list)
    countries: list[Facet] = Field(default_factory=list)
    states: list[Facet] = Field(default_factory=list)
    collections: list[Facet] = Field(default_factory=list)
    detections: list[TypedFacet] = Field(default_factory=list)
    """The detection types: `detected`, `not detected`, `examined for` and
    `not examined for`."""
    attributes: list[TypedFacet] = Field(default_factory=list)
    """Every other attribute type with a code table. Free-text and numeric
    types are left out."""
    parts: list[TypedFacet] = Field(default_factory=list)
    """The part fields with a code table."""


class Stats(ResponseModel):
    """Statistics over one whole result set."""

    matched: int = 0
    distinct: Distinct = Field(default_factory=lambda: Distinct(taxa=0, localities=0, collectors=0))
    years: Years | None = None
    """Min and max collecting year. `null` when no record carries a date."""
    facets: Facets = Field(default_factory=Facets)


class RelationStats(Stats):
    """One relationship, and the records reached by it."""

    relation: str
    pairings: int = 0
    """The distinct (matched record, related record) links of this relationship,
    before the related records collapse to distinct records."""


class Checklist(ResponseModel):
    """A dataset on ChecklistBank."""

    key: int
    """Its ChecklistBank dataset key: `https://www.checklistbank.org/dataset/<key>`."""
    alias: str | None
    title: str | None
    version: str | None
    doi: str | None
    citation: str | None
    """Plain text, ready to cite."""


class TaxonName(ResponseModel):
    name: str
    authorship: str | None
    """`Pallas, 1811`."""
    link: str | None
    """The name's page at the checklist it came from, e.g. its ITIS record,
    which lists the references behind it."""
    source: int | None
    """The `key` of the checklist in `Synonymy.sources` it came from. `null`
    for a name the release's editors added themselves."""


class AcceptedName(TaxonName):
    references: list[str]
    """What its checklist cites for it."""


class TaxonSynonymy(ResponseModel):
    """What one searched taxon turned out to be."""

    rank: str
    name: str
    """As searched."""
    status: Literal["accepted", "synonym", "unmatched", "unavailable"]
    """`accepted`: the name is the accepted one. `synonym`: `accepted` names the
    taxon it is a synonym of. `unmatched`: the release does not know the name,
    and it was searched alone. `unavailable`: ChecklistBank could not be asked,
    and it was searched alone."""
    accepted: AcceptedName | None
    synonyms: list[TaxonName]
    """Every synonym of the accepted name, the searched name among them when it
    is one. All were searched for."""

    @property
    def added(self) -> tuple[str, ...]:
        """The names the search also matches: the accepted name and every
        synonym, but the name itself."""
        names = [n.name for n in (self.accepted, *self.synonyms) if n is not None]
        return tuple(dict.fromkeys(n for n in names if n.lower() != self.name.lower()))


class Synonymy(ResponseModel):
    release: Checklist | None
    """The Catalogue of Life release every name was read from. `null` when no
    name matched."""
    taxa: list[TaxonSynonymy]
    """One per searched taxon, or more when the records file one name under
    classifications the release places in different taxa."""
    sources: list[Checklist]
    """The checklists the release took the names from, by `key`."""

    @classmethod
    def unavailable(cls, taxa: Iterable[tuple[str, str]]) -> "Synonymy":
        """Every `(rank, name)` searched alone, the release not asked."""
        found = [
            TaxonSynonymy(rank=r, name=n, status="unavailable", accepted=None, synonyms=[]) for r, n in dict.fromkeys(taxa)
        ]
        return cls(release=None, taxa=found, sources=[])

    def of(self, rank: str, name: str) -> list[TaxonSynonymy]:
        return [t for t in self.taxa if (t.rank, t.name) == (rank, name)]

    def names(self, rank: str, name: str) -> tuple[str, ...]:
        """The other names `(rank, name)` also matches."""
        return tuple(dict.fromkeys(n for t in self.of(rank, name) for n in t.added))


class SearchSummary(ResponseModel):
    records: Stats
    """Statistics over the set `/search` matches."""
    related: list[RelationStats]
    """One entry per relationship: those the query names, in the order it names
    them and with zeros when nothing is reached, then any a Related-taxon row
    reached without naming. Empty when the query constrains no relation."""
    description: str | None
    """One sentence restating the query. `null` for an empty form."""
    synonyms: Synonymy | None = None
    """What the taxa searched are according to the Catalogue of Life, and whom
    it follows. `null` unless the query asked for `synonyms`."""


class Taxon(ResponseModel):
    """One distinct `(rank, name)` pair of the snapshot."""

    rank: str
    name: str
    record_count: int
    parent_rank: str | None
    """The rank above, with `parent_name`: both are set or neither. It tells
    apart two taxa that share a name."""
    parent_name: str | None


class TaxaResults(ResponseModel):
    matches: list[Taxon]
    """Most records first."""
