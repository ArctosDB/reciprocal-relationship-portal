import logging
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, override

import duckdb

from vulpes.engines.abc import CSVStreamingResponse, QueryEngine
from vulpes.engines.duckdb.schema import build as build_schema
from vulpes.engines.duckdb.taxa import TaxonIndex
from vulpes.engines.duckdb.translate import (
    DEFAULT_COLUMNS,
    EXPORT_COLUMNS,
    LOCALITY_COLUMNS,
    MAP_COLUMNS,
    MAP_PRESET,
    NESTED,
    RELATED_COLUMNS,
    Translator,
    nested,
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
from vulpes.models.search import DETECTION_FIELDS, RANK_COLUMNS, RANKS, BadQueryError, Rank, Search
from vulpes.synonyms import Classification, Synonyms, SynonymsUnavailableError

logger = logging.getLogger(__name__)

MAX_CLASSIFICATIONS = 3
"""The most classifications of one name matched, each one more request. Past
the commonest few, the rest are mostly misfiled records."""


def _compact(value: Any) -> Any:  # noqa: ANN401
    """A nested value without its empty fields, the way a document would hold it."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if v is not None and v != ""}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


class DuckDbQueryEngine(QueryEngine):
    def __init__(self, path: str, code_tables: Path | None = None, synonyms: Synonyms | None = None) -> None:
        """`code_tables` is the directory of Arctos code tables, by default the
        `code-tables` directory beside the database. `synonyms` answers a search
        asking for them; without it, such a search notes they are unavailable."""
        self._synonyms = synonyms
        self.name = "duckdb"
        self.path = path
        # Read-only: the file is replaced whole by the importer, never written here.
        # Only use self.conn.cursor() in functions; a cursor is safe on its own thread.
        self.conn = duckdb.connect(path, read_only=True)

        # The schema never changes while the process runs: the importer swaps
        # in a whole new file, and `vulpes.sh reload` replaces the workers to read it.
        with self.conn.cursor() as cursor:
            self.record_columns: tuple[str, ...] = tuple(r[0] for r in cursor.execute("DESCRIBE records").fetchall())
            self._schema = build_schema(
                cursor,
                code_tables or Path(path).parent / "code-tables",
                columns=[*self.record_columns, *LOCALITY_COLUMNS, *NESTED, *RELATED_COLUMNS],
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
        search = Search.parse(query, self.relationships)
        if not search.expand:
            return search
        taxa = dict.fromkeys(taxon for row in search.taxa for taxon in row.taxa())
        return replace(search, synonyms={(rank, name): self._synonyms_of(rank, name) for rank, name in taxa})

    def _synonyms_of(self, rank: str, name: str) -> tuple[str, ...] | None:
        """The other names Catalogue of Life gives the taxon `name` names.
        `None` when they could not be looked up."""
        if self._synonyms is None:
            return None
        key = " ".join(name.lower().split())
        # A name the snapshot files under several classifications, a homonym
        # in Arctos itself, is matched once under each.
        classifications = self._classifications(rank, key) or ((),)
        try:
            found = [
                self._synonyms.lookup(None if rank == "scientific_name" else rank, key, classification)
                for classification in classifications
            ]
        except SynonymsUnavailableError as e:
            logger.warning("synonyms of %s %r unavailable: %s", rank, name, e)
            return None
        return tuple(dict.fromkeys(n for names in found for n in names if n.lower() != key))

    @lru_cache(maxsize=4096)  # noqa: B019 - the engine lives as long as the process
    def _classifications(self, rank: str, name: str) -> tuple[Classification, ...]:
        """The higher classifications the snapshot's records of `name`, lowercased,
        carry at `rank`, most common first, as hints for ChecklistBank's matcher.
        A scientific name counts the records whose name starts with it."""
        if rank == "scientific_name":
            above: tuple[Rank, ...] = RANKS[:-1]
            where = "starts_with(lower(scientific_name), $name)"
        else:
            above = RANKS[: next(i for i, r in enumerate(RANKS) if r.id == rank)]
            column = RANK_COLUMNS[rank]
            where = f"list_contains(list_transform(string_split(lower({column}), ';'), lambda x: trim(x)), $name)"
        if not above:
            return ()
        # A rank column joining several determinations offers its first.
        picked = ", ".join(f"nullif(trim(split_part({r.column}, ';', 1)), '')" for r in above)
        sql = f"""SELECT {picked} FROM records WHERE {where}
            GROUP BY ALL ORDER BY count(*) DESC LIMIT {MAX_CLASSIFICATIONS}"""  # noqa: S608
        with self.conn.cursor() as cursor:
            rows = cursor.execute(sql, {"name": name}).fetchall()
        return tuple(tuple((r.id, v) for r, v in zip(above, row, strict=True) if v) for row in rows)

    def _columns(self, search: Search, default: Sequence[str], *, related: bool) -> list[str]:
        """The columns of a page or an export, in order. `related` says whether
        its rows are linked to anything, which is when the related columns hold
        a value: always on a related-records table, and on the matched records
        when the search asked for a relation."""
        preset = MAP_COLUMNS if search.cols == (MAP_PRESET,) else None
        if preset or not search.cols:
            columns = list(preset or default)
            if related:
                # Beside the `guid` they pair with.
                columns[1:1] = RELATED_COLUMNS
            return columns
        for column in search.cols:
            if column not in self._schema.columns:
                msg = f"`cols` names `{column}`, which is not a column"
                raise BadQueryError(msg)
        return list(dict.fromkeys(search.cols))

    @override
    def download(self, query: ArctosQuery) -> CSVStreamingResponse:
        search = self._parse(query)
        related = search.tab is not None
        columns = self._columns(search, EXPORT_COLUMNS, related=related or search.constrains_relations)
        sql = Translator(search)

        with self.conn.cursor() as cursor:
            # Refused before any row moves, rather than cut short: a truncated
            # export is a plausible, wrong table.
            _, rows = cursor.execute(*sql.picked_counts(related=related)).fetchall()[0]
            limit = self._schema.limits.max_export_rows
            if rows > limit:
                msg = f"this export holds {rows:,} rows, more than the {limit:,} one download carries. Narrow the search"
                raise BadQueryError(msg)

            # DuckDB writes the CSV itself. The file is sent in pieces and then removed.
            with tempfile.NamedTemporaryFile(prefix="arctos_", suffix=".csv", delete=False) as file:
                path = Path(file.name)
            try:
                cursor.execute(*sql.export(columns, self.record_columns, str(path), related=related))
            except BaseException:
                path.unlink(missing_ok=True)
                raise

        def chunks() -> Iterator[bytes]:
            try:
                with path.open("rb") as csv:
                    while chunk := csv.read(1 << 16):
                        yield chunk
            finally:
                path.unlink(missing_ok=True)

        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        return CSVStreamingResponse(
            chunks(),
            headers={"Content-Disposition": f'attachment; filename="arctos_{stamp}.csv"'},
        )

    @override
    def relations(self, query: ArctosQuery) -> SearchResults:
        search = self._parse(query)
        columns = self._columns(search, DEFAULT_COLUMNS, related=True)
        sql = Translator(search)

        with self.conn.cursor() as cursor:
            specimens, reached = cursor.execute(*sql.picked_counts(related=True)).fetchall()[0]
            flat = [c for c in columns if c in self.record_columns and c != "guid"]
            locality = [c for c in columns if c in LOCALITY_COLUMNS]
            records = self._records(cursor, columns, sql.related_page(flat, locality), None)

        return SearchResults(
            total=Total(value=reached, relation="eq"),
            summary=Summary(context=specimens, matched=reached),
            column_order=columns,
            records=records,
        )

    def _records(
        self,
        cursor: duckdb.DuckDBPyConnection,
        columns: Sequence[str],
        page: tuple[str, dict[str, Any]],
        reached: Callable[[list[int]], tuple[str, dict[str, Any]]] | None,
    ) -> list[dict[str, Any]]:
        """The records of one page statement, each with its nested columns.
        `reached` is the statement giving the related columns for the page's
        ids, when the page statement does not carry them itself."""
        result = cursor.execute(*page)
        names = [d[0] for d in result.description]
        rows = [dict(zip(names, row, strict=True)) for row in result.fetchall()]
        ids = [row.pop("collection_object_id") for row in rows]

        # Each nested column is one more statement over the page's ids.
        lookups: dict[str, dict[int, Any]] = {}
        if ids:
            for column in columns:
                if column in NESTED:
                    lookups[column] = dict(cursor.execute(nested(column), {"ids": ids}).fetchall())

            if reached and any(c in RELATED_COLUMNS for c in columns):
                found = cursor.execute(*reached(ids)).fetchall()
                for i, column in enumerate(RELATED_COLUMNS, start=1):
                    lookups[column] = {row[0]: row[i] for row in found}

        records = []
        for record_id, row in zip(ids, rows, strict=True):
            for column, by_id in lookups.items():
                row[column] = _compact(by_id.get(record_id))
            # A record holds only the fields it has a value for, in display order.
            records.append({c: row[c] for c in columns if row.get(c) is not None})
        return records

    @override
    def search(self, query: ArctosQuery) -> SearchResults:
        search = self._parse(query)
        columns = self._columns(search, DEFAULT_COLUMNS, related=search.constrains_relations)
        sql = Translator(search)

        with self.conn.cursor() as cursor:
            taxon, context, matched, *per_row = cursor.execute(*sql.counts()).fetchall()[0]

            flat = [c for c in columns if c in self.record_columns and c != "guid"]
            locality = [c for c in columns if c in LOCALITY_COLUMNS]
            reached = sql.reached if search.constrains_relations else None
            records = self._records(cursor, columns, sql.page(flat, locality), reached)

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
