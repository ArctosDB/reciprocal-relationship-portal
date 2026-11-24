import tempfile
from collections.abc import Callable, Iterator, Sequence
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
    classifications,
    layouts,
    nested,
)
from vulpes.models.query import ArctosQuery
from vulpes.models.responses import (
    AttrCount,
    Schema,
    SearchResults,
    SearchSummary,
    Summary,
    Taxon,
    Total,
)
from vulpes.models.search import DETECTION_FIELDS, BadQueryError, Search
from vulpes.synonyms import Classification, Synonyms

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
        # The snapshot never changes while the process runs.
        self._classifications = lru_cache(maxsize=4096)(self._classify)
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
            self._layouts = layouts(cursor)

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
        synonyms = self._synonyms
        resolve = None if synonyms is None else lambda taxa: synonyms.expand(taxa, self._classifications)
        return Search.parse(query, self.relationships, resolve)

    def _classify(self, rank: str, name: str) -> tuple[Classification, ...]:
        """The higher classifications the snapshot files `name`, lowercased, under
        at `rank`, most common first, as hints for ChecklistBank's matcher."""
        sql, above = classifications(rank, MAX_CLASSIFICATIONS)
        if not above:
            return ()
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
        sql = Translator(search, self.record_columns)

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
                cursor.execute(*sql.export(columns, str(path), self._layouts, related=related))
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
        sql = Translator(search, self.record_columns)

        with self.conn.cursor() as cursor:
            specimens, reached = cursor.execute(*sql.picked_counts(related=True)).fetchall()[0]
            records = self._records(cursor, columns, sql.related_page(columns), None)

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
            records.append({c: row[c] for c in columns if row.get(c) is not None})
        return records

    @override
    def search(self, query: ArctosQuery) -> SearchResults:
        search = self._parse(query)
        columns = self._columns(search, DEFAULT_COLUMNS, related=search.constrains_relations)
        sql = Translator(search, self.record_columns)

        with self.conn.cursor() as cursor:
            taxon, context, matched, *per_row = cursor.execute(*sql.counts()).fetchall()[0]

            reached = sql.reached if search.constrains_relations else None
            records = self._records(cursor, columns, sql.page(columns), reached)

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
        sql = Translator(search, self.record_columns)
        with self.conn.cursor() as cursor:
            rows = cursor.execute(*sql.summary(self.categorical)).fetchall()
        records, related = sql.read_summary(rows)
        return SearchSummary(
            records=records,
            related=related,
            description=search.description(),
            synonyms=search.synonyms,
        )

    @override
    def taxa(self, q: str, rank: str | None, limit: int) -> list[Taxon]:
        return self._taxa.matching(q, rank, limit)

    @override
    def close(self) -> None:
        self.conn.close()
