from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, override

import duckdb

from vulpes.engines.abc import CSVStreamingResponse, QueryEngine
from vulpes.engines.duckdb.schema import build as build_schema
from vulpes.engines.duckdb.taxa import TaxonIndex
from vulpes.engines.duckdb.translate import (
    DEFAULT_COLUMNS,
    LOCALITY_COLUMNS,
    NESTED,
    RELATED_GUID,
    Translator,
)
from vulpes.models.query import ArctosQuery
from vulpes.models.responses import (
    AttrCount,
    Distinct,
    Facet,
    Facets,
    RelationStats,
    Schema,
    SearchResults,
    SearchSummary,
    Stats,
    Summary,
    Taxon,
    Total,
    TypedFacet,
    Years,
)
from vulpes.models.search import DETECTION_FIELDS, BadQueryError, Search


def _compact(value: Any) -> Any:  # noqa: ANN401
    """A nested value without its empty fields, the way a document would hold it."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if v is not None and v != ""}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


class DuckDbQueryEngine(QueryEngine):
    def __init__(self, path: str, code_tables: Path | None = None) -> None:
        """`code_tables` is the directory of Arctos code tables, by default the
        `code-tables` directory beside the database."""
        self.name = "duckdb"
        self.path = path
        # Read-only: the file is replaced whole by the importer, never written here.
        # Only use self.conn.cursor() in functions; a cursor is safe on its own thread.
        self.conn = duckdb.connect(path, read_only=True)

        # The schema never changes while the process runs: the importer swaps
        # in a whole new file, and the service is restarted to read it.
        with self.conn.cursor() as cursor:
            self.record_columns: tuple[str, ...] = tuple(r[0] for r in cursor.execute("DESCRIBE records").fetchall())
            self._schema = build_schema(
                cursor,
                code_tables or Path(path).parent / "code-tables",
                columns=[*self.record_columns, *LOCALITY_COLUMNS, *NESTED, RELATED_GUID],
                snapshot=self._snapshot_date(cursor),
            )

            self._taxa = TaxonIndex(cursor)

        self.relationships: frozenset[str] = frozenset(r.value for r in self._schema.relations)
        # The summary facets attribute values only for the types that have a
        # code table: free-text and numeric types have no values worth ranking.
        self.categorical: frozenset[str] = frozenset(
            t.id for t in self._schema.attribute_types if t.vocabulary and t.id not in DETECTION_FIELDS
        )

    def _snapshot_date(self, cursor: duckdb.DuckDBPyConnection) -> date:
        """The day of the flat export the database was built from, which the
        importer records. A file built before it did has only its own age."""
        if cursor.execute("SELECT 1 FROM duckdb_tables() WHERE table_name = 'snapshot'").fetchall():
            recorded: date = cursor.execute("SELECT snapshot_date FROM snapshot").fetchall()[0][0]
            return recorded
        return datetime.fromtimestamp(Path(self.path).stat().st_mtime, tz=UTC).date()

    def _parse(self, query: ArctosQuery) -> Search:
        return Search.parse(query, self.relationships)

    def _columns(self, search: Search) -> list[str]:
        """The page's columns, in display order."""
        if not search.cols:
            columns = list(DEFAULT_COLUMNS)
            if search.constrains_relations:
                # Beside the `guid` it pairs with.
                columns.insert(1, RELATED_GUID)
            return columns
        for column in search.cols:
            if column not in self._schema.columns:
                msg = f"`cols` names `{column}`, which is not a column"
                raise BadQueryError(msg)
        return list(dict.fromkeys(search.cols))

    @override
    def download(self, query: ArctosQuery) -> CSVStreamingResponse:
        raise NotImplementedError

    @override
    def relations(self, query: ArctosQuery) -> SearchResults:
        raise NotImplementedError

    @override
    def search(self, query: ArctosQuery) -> SearchResults:
        search = self._parse(query)
        columns = self._columns(search)
        sql = Translator(search)

        with self.conn.cursor() as cursor:
            taxon, context, matched, *per_row = cursor.execute(*sql.counts()).fetchall()[0]

            flat = [c for c in columns if c in self.record_columns and c != "guid"]
            locality = [c for c in columns if c in LOCALITY_COLUMNS]
            page = cursor.execute(*sql.page(flat, locality))
            names = [d[0] for d in page.description]
            rows = [dict(zip(names, row, strict=True)) for row in page.fetchall()]
            ids = [row.pop("collection_object_id") for row in rows]

            # Each nested column is one more statement over the page's ids.
            nested: dict[str, dict[int, Any]] = {}
            if ids:
                for column in columns:
                    if column in NESTED:
                        nested[column] = dict(cursor.execute(NESTED[column], {"ids": ids}).fetchall())
                    elif column == RELATED_GUID and search.constrains_relations:
                        nested[column] = dict(cursor.execute(*sql.related_guids(ids)).fetchall())

        records = []
        for record_id, row in zip(ids, rows, strict=True):
            for column, by_id in nested.items():
                row[column] = _compact(by_id.get(record_id))
            # A record holds only the fields it has a value for, in display order.
            records.append({c: row[c] for c in columns if row.get(c) is not None})

        return SearchResults(
            # DuckDB counts exactly, so there is never a `gte`.
            total=Total(value=matched, relation="eq"),
            summary=Summary(
                taxon=taxon,
                context=context,
                matched=matched,
                attrs=[AttrCount(attr=row.raw, count=n) for row, n in zip(search.attrs, per_row, strict=True)],
            ),
            column_order=columns,
            records=records,
        )

    @override
    def schema(self) -> Schema:
        return self._schema

    @override
    def summary(self, query: ArctosQuery) -> SearchSummary:
        search = self._parse(query)
        with self.conn.cursor() as cursor:
            rows = cursor.execute(*Translator(search).summary(self.categorical)).fetchall()

        # One bucket of numbers per set: None for the matched records, else a relationship.
        numbers: defaultdict[str | None, dict[str, int | None]] = defaultdict(dict)
        facets: defaultdict[str | None, defaultdict[str, list[tuple[str | None, str, int]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for grp, stat, kind, value, n in rows:
            if value is None:
                numbers[grp][stat] = n
            else:
                facets[grp][stat].append((kind, value, n))

        def stats(grp: str | None) -> dict[str, Any]:
            got, lists = numbers[grp], facets[grp]

            def plain(name: str) -> list[Facet]:
                top = sorted(lists[name], key=lambda f: (-f[2], f[1]))
                return [Facet(value=value, count=n) for _, value, n in top]

            def typed(name: str) -> list[TypedFacet]:
                top = sorted(lists[name], key=lambda f: (-f[2], f[0] or "", f[1]))
                return [TypedFacet(type=kind or "", value=value, count=n) for kind, value, n in top]

            low, high = got.get("year_min"), got.get("year_max")
            return {
                "matched": got.get("matched") or 0,
                "distinct": Distinct(
                    taxa=got.get("taxa_distinct") or 0,
                    localities=got.get("localities_distinct") or 0,
                    collectors=got.get("collectors_distinct") or 0,
                ),
                "years": None if low is None or high is None else Years(min=low, max=high),
                "facets": Facets(
                    taxa=plain("taxa"),
                    countries=plain("countries"),
                    states=plain("states"),
                    collections=plain("collections"),
                    detections=typed("detections"),
                    attributes=typed("attributes"),
                    parts=typed("parts"),
                ),
            }

        # The relationships the query names come first, in its order and present
        # even when nothing was reached: the portal draws a tab for each. Then
        # whatever a Related-taxon row reached without naming.
        reached = {grp.lower(): grp for grp in numbers if grp is not None}
        named = [reached.pop(kind.lower(), kind) for kind in search.relationships()]
        related = [
            RelationStats(relation=grp, pairings=numbers[grp].get("pairings") or 0, **stats(grp))
            for grp in (*named, *sorted(reached.values()))
        ]

        return SearchSummary(records=Stats(**stats(None)), related=related, description=search.description())

    @override
    def taxa(self, q: str, rank: str | None, limit: int) -> list[Taxon]:
        return self._taxa.matching(q, rank, limit)

    @override
    def close(self) -> None:
        self.conn.close()
