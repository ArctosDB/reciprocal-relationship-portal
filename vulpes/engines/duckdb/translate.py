"""A parsed `Search` as DuckDB SQL.

Every statement is built from the same predicates, so the page, its counts and
the summary cannot disagree about which records a search selects. The layout
these queries lean on, and the measurements behind it, are in
`importer/build_duckdb.py`.

How each filter reads the tables:

taxon       A rank matches its own column on `records`, case-insensitively. A
            rank column joins the names of several determinations with `;`, and
            any one of them matches. `scientific_name` is not a rank: it matches
            as a phrase of whole words.
relations   `related_records` is the edge table, both directions stored as
            Arctos records them. A row's relationship and Related taxon must
            hold for the same edge, and the edge must leave a record the row's
            own taxon selects. A Related taxon matches the rank columns of the
            record the edge points at; an edge pointing outside the database
            only knows the family and identification Arctos cached on it.
attr, part  A row is a semi join on `attributes` or `parts`; a negated row is
            the anti join.
dates       Both ends must hold for one event. A record with no event falls back
            to the dates on the record itself.
tab         Not a filter. It names the table being viewed: absent, the matched
            records; a relationship, the records they reach by it.

Values never reach the SQL text. They are bound as parameters; only column
names from this module's own tables are formatted in.
"""

# ruff: noqa: S608
# Every statement here is formatted SQL, for the reason above.

import re
from collections.abc import Collection, Iterable, Sequence
from typing import Any

from vulpes.models.search import DETECTION_FIELDS, PER_PAGE, RANK_COLUMNS, AttrRow, Search, TaxonRow

FACET_SIZE = 10
"""Each summary facet lists at most this many values."""

PART_FACETS = ("part_name", "disposition")
"""The part fields with a code table."""

# Not a letter or a digit: what separates the words of a phrase.
_GAP = r"[^\pL\pN]"
_PARAM = re.compile(r"\$(\w+)")

# Year-only and year-month dates read as their first day, as a date parser does.
# Anything not starting with a digit (blank, or a BCE year) is not comparable.
_PAD = "left({col}, 10) || substr('-01-01', length(left({col}, 10)) - 3)"
_YEAR = "CASE WHEN regexp_matches({col}, '^\\d{{4}}') THEN CAST(left({col}, 4) AS INTEGER) END"

# The nested columns, one query each, as `(record id, list)`. `%IDS%` stands for
# the query giving the record ids wanted: a page's ids, or an export's rows.
# Scanning a narrow id column is cheap; no table here is laid out for id lookups.
_PAGE_IDS = "SELECT unnest($ids::BIGINT[])"
NESTED = {
    "relations": """
        SELECT src_id, list({
            'relationship': relationship,
            'related_guid': dst_guid,
            'related_identifier': dst_value,
            'related_identifier_type': identifier_type,
            'related_identification': dst_identification,
            'related_family': dst_family,
            'related_geography': dst_geography
        } ORDER BY relationship, dst_value)
        FROM related_records
        WHERE src_id IN (%IDS%)
        GROUP BY src_id
    """,
    # The localities are fetched by the ids the events name. Joined straight to
    # the table, the same lookup read all of it (72 ms against 9).
    "events": """
        WITH e AS MATERIALIZED (
            SELECT * FROM specimen_events WHERE id IN (%IDS%)
        ),
        l AS (
            SELECT locality_id, higher_geog, spec_locality, dec_lat, dec_long
            FROM localities
            WHERE locality_id IN (SELECT locality_id FROM e)
        )
        SELECT e.id, list({
            'specimen_event_id': e.specimen_event_id,
            'specimen_event_type': e.specimen_event_type,
            'began_date': e.began_date,
            'ended_date': e.ended_date,
            'verbatim_date': e.verbatim_date,
            'verificationstatus': e.verificationstatus,
            'collecting_method': e.collecting_method,
            'collecting_source': e.collecting_source,
            'habitat': e.habitat,
            'locality_id': e.locality_id,
            'higher_geog': l.higher_geog,
            'spec_locality': l.spec_locality,
            'dec_lat': l.dec_lat,
            'dec_long': l.dec_long
        } ORDER BY e.order_by_vs, e.specimen_event_id)
        FROM e
        LEFT JOIN l ON l.locality_id = e.locality_id
        GROUP BY e.id
    """,
    "attributedetail": """
        SELECT id, list({
            'attribute_type': attribute_type,
            'attribute_value': attribute_value,
            'attribute_units': attribute_units,
            'attribute_date': attribute_date,
            'attribute_method': attribute_method,
            'attribute_remark': attribute_remark,
            'attribute_determiner': determiner_name
        } ORDER BY attribute_type, attribute_value)
        FROM attributes
        WHERE id IN (%IDS%)
        GROUP BY id
    """,
    "partdetail": """
        SELECT p.id, list({
            'part_id': p.part_id,
            'part_name': p.part_name,
            'condition': p.condition,
            'part_count': p.part_count,
            'disposition': p.disposition,
            'part_barcode': p.part_barcode,
            'container_path': p.container_path,
            'part_remark': p.part_remark,
            'part_attributes': pa.attributes
        } ORDER BY p.part_name, p.part_id)
        FROM parts p
        LEFT JOIN (
            SELECT part_id, list({
                'attribute_type': attribute_type,
                'attribute_value': attribute_value,
                'attribute_units': attribute_units,
                'attribute_date': attribute_date,
                'attribute_method': attribute_method,
                'attribute_remark': attribute_remark,
                'attribute_determiner': determiner_name
            } ORDER BY attribute_type, attribute_value) AS attributes
            FROM part_attributes
            WHERE id IN (%IDS%)
            GROUP BY part_id
        ) pa ON pa.part_id = p.part_id
        WHERE p.id IN (%IDS%)
        GROUP BY p.id
    """,
}

RELATED_GUID = "related_guid"
"""On a matched record: the guids of the records it reaches by the relations
the search asked for. Not the record's whole `relations` list, which holds
every link it has in Arctos."""

LOCALITY_COLUMNS = (
    "spec_locality",
    "higher_geog",
    "dec_lat",
    "dec_long",
    "coordinateuncertaintyinmeters",
)
"""Columns of the record's primary locality, which live on `localities`."""

EXPORT_COLUMNS = (
    "guid",
    "scientific_name",
    "country",
    "state_prov",
    "dec_lat",
    "dec_long",
    "attributedetail",
    "relations",
)
"""The columns of a download that names none. Not every column: the column
count sets the cost of an export."""

MAP_COLUMNS = (
    "guid",
    "scientific_name",
    "country",
    "state_prov",
    "dec_lat",
    "dec_long",
    "coordinateuncertaintyinmeters",
)
"""`cols=map`: what a map needs and nothing else, the columns
`vulpes/config/berkeleymapper.xml` describes. BerkeleyMapper builds a balloon
for every marker up front, so a nested column would stop the map drawing."""

MAP_PRESET = "map"

DEFAULT_COLUMNS = (
    "guid",
    "scientific_name",
    "relations",
    "country",
    "state_prov",
    "spec_locality",
    "events",
    "attributedetail",
    "partdetail",
)


def nested(column: str, ids: str = _PAGE_IDS) -> str:
    """The statement for one nested column of the records `ids` selects, by
    default the page ids bound as `$ids`."""
    return NESTED[column].replace("%IDS%", ids)


def _any(clauses: Sequence[str]) -> str:
    return clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"


def _all(clauses: Sequence[str]) -> str:
    return " AND ".join(clauses) if clauses else "TRUE"


class Translator:
    """One search's statements. Every statement shares `params`; `statement`
    pairs a statement with the parameters it actually names, because DuckDB
    refuses a parameter the text does not use."""

    def __init__(self, search: Search) -> None:
        self.search = search
        self.params: dict[str, Any] = {}

        relation_rows = [row for row in search.taxa if row.constrains_relations]
        self._edges = self._edges_cte(relation_rows) if relation_rows else None

        taxon = [self._taxon("r", row.rank, row.name) for row in search.taxa if not row.constrains_relations]
        if relation_rows:
            taxon.append("r.collection_object_id IN (SELECT src_id FROM edges)")
        self._taxon_block = _any(taxon) if taxon else None

        self._scope = self._scope_clauses()
        # One entry per `attr` row, negation included: what that row filters for.
        self._attr_rows = [("NOT " if row.negated else "") + self._attribute(row) for row in search.attrs]

    def statement(self, sql: str, **extra: object) -> tuple[str, dict[str, Any]]:
        params = {**self.params, **extra}
        return sql, {name: params[name] for name in set(_PARAM.findall(sql))}

    def _bind(self, value: object) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f"${name}"

    def _lowered(self, values: Iterable[str]) -> str:
        return ", ".join(f"lower({self._bind(v)})" for v in values)

    # ---- predicates -------------------------------------------------------

    def _phrase(self, col: str, text: str) -> str:
        """Whole words, in order, any case, whatever punctuation lies between:
        "Sandia Mountains" matches "Sandia Mountains, Cibola National Forest"
        and not "Mountains of Sandia"."""
        words = re.findall(r"\w+", text)
        if not words:
            return "FALSE"
        pattern = f"(?i)(^|{_GAP})" + f"{_GAP}+".join(re.escape(w) for w in words) + f"({_GAP}|$)"
        # The substring test is cheap and leaves the regex few rows to read.
        longest = max(words, key=len)
        return f"(contains(lower({col}), lower({self._bind(longest)})) AND regexp_matches({col}, {self._bind(pattern)}))"

    def _taxon(self, alias: str, rank: str, name: str) -> str:
        if rank == "scientific_name":
            return self._phrase(f"{alias}.scientific_name", name)
        col = f"{alias}.{RANK_COLUMNS[rank]}"
        value = f"lower({self._bind(name)})"
        return (
            f"(lower({col}) = {value} OR (contains({col}, ';') AND list_contains("
            f"list_transform(string_split(lower({col}), ';'), lambda x: trim(x)), {value})))"
        )

    def _related(self, rank: str, name: str) -> str:
        """A Related taxon, tested on one edge `e`."""
        resolved = f"e.dst_id IN (SELECT d.collection_object_id FROM records d WHERE {self._taxon('d', rank, name)})"
        # An edge pointing outside the database carries only what Arctos cached.
        if rank == "scientific_name":
            return f"({resolved} OR {self._phrase('e.dst_identification', name)})"
        if rank == "family":
            return f"({resolved} OR (e.dst_id IS NULL AND lower(e.dst_family) = lower({self._bind(name)})))"
        return resolved

    def _edge(self, row: TaxonRow) -> str:
        """The edges one taxon row asks for."""
        clauses = []
        if row.relationships:
            clauses.append(f"lower(e.relationship) IN ({self._lowered(row.relationships)})")
        if row.name:
            specimen = self._taxon("s", row.rank, row.name)
            clauses.append(f"e.src_id IN (SELECT s.collection_object_id FROM records s WHERE {specimen})")
        if row.related:
            clauses.append(_any([self._related(rank, name) for rank, name in row.related]))
        return "(" + _all(clauses) + ")"

    def _edges_cte(self, rows: Sequence[TaxonRow]) -> str:
        return f"""edges AS (
            SELECT e.src_id, e.dst_id, e.dst_guid, e.relationship
            FROM related_records e
            WHERE {_any([self._edge(row) for row in rows])}
        )"""

    def _attribute(self, row: AttrRow) -> str:
        """Whether the record carries the row's attribute. A value is exact,
        several values OR, and no value asks only for the type."""
        clauses = [f"lower(a.attribute_type) = lower({self._bind(row.type)})"]
        if row.values:
            clauses.append(f"lower(a.attribute_value) IN ({self._lowered(row.values)})")
        if self.search.method:
            # The method and the value must be the same attribute's.
            clauses.append(self._method())
        return f"r.collection_object_id IN (SELECT a.id FROM attributes a WHERE {_all(clauses)})"

    def _method(self) -> str:
        return f"contains(lower(a.attribute_method), lower({self._bind(self.search.method)}))"

    def _part(self, row: AttrRow) -> str:
        col = f"p.{row.type}"  # one of PART_FIELDS, checked by the parse
        if not row.values:
            clause = f"{col} IS NOT NULL"
        elif row.type == "part_barcode":
            clause = f"{col} IN ({', '.join(self._bind(v) for v in row.values)})"
        else:
            clause = f"lower({col}) IN ({self._lowered(row.values)})"
        negation = "NOT " if row.negated else ""
        return f"r.collection_object_id {negation}IN (SELECT p.id FROM parts p WHERE {clause})"

    def _dated(self, col: str) -> str:
        """`col` falls inside the search's date range."""
        padded = _PAD.format(col=col)
        clauses = [f"{col} >= '0'"]
        if self.search.from_:
            day = self.search.from_.isoformat()
            # The plain comparisons are implied by the padded ones. They cost
            # nothing and spare most rows the string building.
            clauses += [f"{col} >= {self._bind(day[:4])}", f"{padded} >= {self._bind(day)}"]
        if self.search.to:
            day = self.search.to.isoformat()
            clauses += [f"{col} <= {self._bind(day + '~')}", f"{padded} <= {self._bind(day)}"]
        return _all(clauses)

    def _scope_clauses(self) -> list[str]:
        """Everything but the taxon and attribute rows."""
        s = self.search
        clauses = [self._part(row) for row in s.parts]
        if s.prefixes:
            # Exact, so the scan skips the row groups of other collections.
            clauses.append(f"r.guid_prefix IN ({', '.join(self._bind(p) for p in s.prefixes)})")
        if s.countries:
            clauses.append(f"lower(r.country) IN ({self._lowered(s.countries)})")
        if s.states:
            clauses.append(f"lower(r.state_prov) IN ({self._lowered(s.states)})")
        if s.collector:
            clauses.append(
                "r.collection_object_id IN (SELECT ra.id FROM record_agents ra WHERE ra.agent_role = 'collector' "
                f"AND ra.agent_id IN (SELECT agent_id FROM agents WHERE agent_name = {self._bind(s.collector)}))"
            )
        if s.locality:
            # The record's own locality. Unlike the date, it is not held to an event.
            phrase = self._phrase("l.spec_locality", s.locality)
            clauses.append(f"r.locality_id IN (SELECT l.locality_id FROM localities l WHERE {phrase})")
        if s.from_ or s.to:
            # A record's own date is always one of its events' when it has any,
            # so the first half only adds the records with no event at all.
            clauses.append(
                f"(({self._dated('r.began_date')}) OR r.collection_object_id IN "
                f"(SELECT e.id FROM specimen_events e WHERE {self._dated('e.began_date')}))"
            )
        return clauses

    def _attr_block(self, rows: Sequence[str]) -> list[str]:
        """The attribute rows combined by `attr_op`. `rows` are the row
        predicates, or the names of columns holding them."""
        block = list(rows) if self.search.attr_op == "and" else [_any(rows)] if rows else []
        if self.search.method and not rows:
            block.append(f"r.collection_object_id IN (SELECT a.id FROM attributes a WHERE {self._method()})")
        return block

    def _where(self) -> str:
        """The whole form."""
        taxon = [self._taxon_block] if self._taxon_block else []
        return _all([*taxon, *self._scope, *self._attr_block(self._attr_rows)])

    def _with(self, *ctes: str) -> str:
        every = [cte for cte in (self._edges, *ctes) if cte]
        return "WITH " + ",\n".join(every) if every else ""

    # ---- statements -------------------------------------------------------

    def counts(self) -> tuple[str, dict[str, Any]]:
        """The funnel: `taxon`, `context`, `matched`, then one count per `attr`
        row. Each row is counted against `context`, not against `matched`."""
        rows = [f"a{i}" for i in range(len(self._attr_rows))]
        flags = [f"({row}) AS a{i}" for i, row in enumerate(self._attr_rows)]
        attrs = self._attr_block(rows) if rows else []
        if not rows and self.search.method:
            # Without attribute rows the method stands alone, and still narrows.
            flags.append(f"({self._attr_block([])[0]}) AS lone")
            attrs = ["lone"]
        matched = _all(["scope", *attrs])

        if self._taxon_block:
            # Scan the taxon's records once and flag what else each one passes.
            source = f"""(
                SELECT ({_all(self._scope)}) AS scope{"".join(", " + f for f in flags)}
                FROM records r WHERE {self._taxon_block}
            )"""
            taxon = "count(*)"
        else:
            # No taxon row: the widest set is every record, and the scope can
            # filter the scan itself.
            source = f"""(
                SELECT TRUE AS scope{"".join(", " + f for f in flags)}
                FROM records r WHERE {_all(self._scope)}
            )"""
            taxon = "(SELECT count(*) FROM records)"
        per_row = "".join(f", count(*) FILTER (scope AND {row})" for row in rows)
        return self.statement(f"""
            {self._with()}
            SELECT {taxon}, count(*) FILTER (scope), count(*) FILTER ({matched}){per_row}
            FROM {source}
        """)

    def page(self, record_columns: Collection[str], locality_columns: Collection[str]) -> tuple[str, dict[str, Any]]:
        """One page of matched records in guid order, id first."""
        select = ", ".join(f"r.{c}" for c in ("collection_object_id", "guid", "locality_id", *record_columns))
        locality = "".join(f", l.{c}" for c in locality_columns)
        offset = (self.search.page - 1) * PER_PAGE
        page = f"""page AS (
                SELECT {select} FROM records r
                WHERE {self._where()}
                ORDER BY r.guid LIMIT {PER_PAGE} OFFSET {offset}
            )"""
        return self.statement(f"""
            {self._with(page)}
            SELECT page.* EXCLUDE (locality_id){locality}
            FROM page LEFT JOIN localities l ON l.locality_id = page.locality_id
            ORDER BY page.guid
        """)

    def related_guids(self, ids: Sequence[int]) -> tuple[str, dict[str, Any]]:
        """For the records `ids`, the guids each reaches by the edges asked for."""
        return self.statement(
            f"""
            {self._with()}
            SELECT src_id, list(DISTINCT dst_guid ORDER BY dst_guid)
            FROM edges
            WHERE src_id IN ({_PAGE_IDS}) AND dst_guid IS NOT NULL
            GROUP BY src_id
            """,
            ids=list(ids),
        )

    def _picked(self, *, related: bool) -> list[str]:
        """The CTEs ending in `picked(id, related_guid)`: the rows of the table
        being viewed, each with the guids on the other end of its links.

        Viewing the specimens, a row is a matched record and `related_guid` the
        records it reaches. Viewing a relationship's tab, a row is a record the
        matched ones reach by that relationship, and `related_guid` points back
        at them. The predicate stays on the specimen's side of the link either
        way, so the related table never reasons about inverses, and no other
        filter of the form reaches the related records.
        """
        hits = f"hits AS MATERIALIZED (SELECT r.collection_object_id AS id, r.guid FROM records r WHERE {self._where()})"
        if not self._edges:
            # No row asked for a relation: nothing is reached, and nothing reaches.
            only = "FROM hits WHERE FALSE" if related else "FROM hits"
            return [hits, f"picked AS (SELECT id, NULL::VARCHAR[] AS related_guid {only})"]
        if related:
            # A tab shows one relationship of those the rows asked for.
            tab = f"AND lower(e.relationship) = lower({self._bind(self.search.tab)})" if self.search.tab else ""
            picked = f"""picked AS (
                SELECT e.dst_id AS id, list(DISTINCT h.guid ORDER BY h.guid) AS related_guid
                FROM edges e JOIN hits h ON h.id = e.src_id
                WHERE e.dst_id IS NOT NULL {tab}
                GROUP BY e.dst_id
            )"""
        else:
            picked = """picked AS (
                SELECT h.id, x.guids AS related_guid
                FROM hits h LEFT JOIN (
                    SELECT src_id, list(DISTINCT dst_guid ORDER BY dst_guid) AS guids
                    FROM edges WHERE dst_guid IS NOT NULL GROUP BY src_id
                ) x ON x.src_id = h.id
            )"""
        return [hits, picked]

    def picked_counts(self, *, related: bool) -> tuple[str, dict[str, Any]]:
        """How many records the form matched, and how many rows the table holds."""
        return self.statement(f"""
            {self._with(*self._picked(related=related))}
            SELECT (SELECT count(*) FROM hits), (SELECT count(*) FROM picked)
        """)

    def related_page(self, record_columns: Collection[str], locality_columns: Collection[str]) -> tuple[str, dict[str, Any]]:
        """One page of the related records in guid order, id first, each with
        the matched records it was reached from."""
        select = ", ".join(f"r.{c}" for c in ("collection_object_id", "guid", "locality_id", *record_columns))
        locality = "".join(f", l.{c}" for c in locality_columns)
        offset = (self.search.page - 1) * PER_PAGE
        page = f"""page AS (
                SELECT {select}, p.related_guid
                FROM picked p JOIN records r ON r.collection_object_id = p.id
                ORDER BY r.guid LIMIT {PER_PAGE} OFFSET {offset}
            )"""
        return self.statement(f"""
            {self._with(*self._picked(related=True), page)}
            SELECT page.* EXCLUDE (locality_id){locality}
            FROM page LEFT JOIN localities l ON l.locality_id = page.locality_id
            ORDER BY page.guid
        """)

    def export(
        self, columns: Sequence[str], record_columns: Collection[str], path: str, *, related: bool
    ) -> tuple[str, dict[str, Any]]:
        """Write every row of the table as CSV to `path`, in guid order, with
        `columns` as the header. DuckDB writes the file itself, which is several
        times quicker than moving the rows through Python.

        A nested column holds its JSON text and `related_guid` its guids joined
        with `"; "`, because a CSV cell holds one value.
        """
        flat = [c for c in columns if c in record_columns and c != "guid"]
        out = f"""out AS MATERIALIZED (
                SELECT p.id, p.related_guid, r.guid, r.locality_id{"".join(f", r.{c}" for c in flat)}
                FROM picked p JOIN records r ON r.collection_object_id = p.id
            )"""
        ctes, joins, select = [out], [], []
        for column in columns:
            if column in NESTED:
                ctes.append(f"n_{column}(id, v) AS ({nested(column, 'SELECT id FROM out')})")
                joins.append(f"LEFT JOIN n_{column} ON n_{column}.id = out.id")
                select.append(f"to_json(n_{column}.v) AS {column}")
            elif column in LOCALITY_COLUMNS:
                select.append(f"l.{column}")
            elif column == RELATED_GUID:
                select.append(f"array_to_string(out.related_guid, '; ') AS {column}")
            else:
                select.append(f"out.{column}")
        if any(c in LOCALITY_COLUMNS for c in columns):
            joins.append("LEFT JOIN localities l ON l.locality_id = out.locality_id")
        target = path.replace("'", "''")
        return self.statement(f"""
            COPY (
                {self._with(*self._picked(related=related), *ctes)}
                SELECT {", ".join(select)}
                FROM out {" ".join(joins)}
                ORDER BY out.guid
            ) TO '{target}' (FORMAT csv, HEADER)
        """)

    def summary(self, categorical: Collection[str]) -> tuple[str, dict[str, Any]]:
        """Every number of the summary as `(grp, stat, type, value, n)` rows.

        `grp` is NULL for the matched records and a relationship for the
        records reached by it. Both go through the same aggregates as one set
        keyed by `grp`, so the two blocks cannot differ in shape. `categorical`
        is the attribute types that have a code table, detections aside.
        """
        columns = (
            "r.collection_object_id AS id, r.scientific_name, r.country, r.state_prov, r.guid_prefix, "
            "r.began_date, r.ended_date"
        )
        ctes = [f"matched AS MATERIALIZED (SELECT NULL::VARCHAR AS grp, {columns} FROM records r WHERE {self._where()})"]
        pairings = ""
        if self._edges:
            ctes += [
                # One link per pair, however many times Arctos records it.
                """links AS MATERIALIZED (
                    SELECT DISTINCT relationship AS grp, src_id, dst_guid, dst_id
                    FROM edges
                    WHERE src_id IN (SELECT id FROM matched) AND dst_guid IS NOT NULL
                )""",
                f"""sets AS MATERIALIZED (
                    SELECT * FROM matched
                    UNION ALL
                    SELECT k.grp, {columns}
                    FROM (SELECT DISTINCT grp, dst_id FROM links WHERE dst_id IS NOT NULL) k
                    JOIN records r ON r.collection_object_id = k.dst_id
                )""",
            ]
            pairings = "UNION ALL SELECT grp, 'pairings', NULL, NULL, count(*) FROM links GROUP BY grp"
        else:
            ctes.append("sets AS (SELECT * FROM matched)")

        detections = ", ".join(self._bind(t) for t in DETECTION_FIELDS)
        typed = [f"a.attribute_type IN ({detections})"]
        if categorical:
            typed.append(f"a.attribute_type IN ({', '.join(self._bind(t) for t in sorted(categorical))})")
        part_facets = " UNION ALL ".join(
            f"""SELECT s.grp, 'parts', '{field}', p.{field}, count(DISTINCT p.id)
                FROM parts p JOIN sets s ON s.id = p.id WHERE p.{field} IS NOT NULL GROUP BY ALL"""
            for field in PART_FACETS
        )
        began, ended = _YEAR.format(col="began_date"), _YEAR.format(col="coalesce(nullif(ended_date, ''), began_date)")
        record_facets = " UNION ALL ".join(
            f"SELECT grp, '{name}' AS stat, NULL AS type, {col} AS value, count(*) AS n "
            f"FROM sets WHERE {col} <> '' GROUP BY ALL"
            for name, col in (
                ("taxa", "scientific_name"),
                ("countries", "country"),
                ("states", "state_prov"),
                ("collections", "guid_prefix"),
            )
        )
        return self.statement(f"""
            {self._with(*ctes)},
            events AS MATERIALIZED (
                SELECT s.grp, e.locality_id, e.began_date, e.ended_date
                FROM specimen_events e JOIN sets s ON s.id = e.id
            ),
            -- every event of every record, plus the record's own dates for those with no event
            dates AS (
                SELECT grp, began_date, ended_date FROM events
                UNION ALL
                SELECT grp, began_date, ended_date FROM sets
            ),
            facets AS (
                {record_facets}
                UNION ALL
                SELECT s.grp, CASE WHEN {typed[0]} THEN 'detections' ELSE 'attributes' END,
                    a.attribute_type, a.attribute_value, count(DISTINCT a.id)
                FROM attributes a JOIN sets s ON s.id = a.id
                WHERE {_any(typed)} AND a.attribute_value <> ''
                GROUP BY ALL
                UNION ALL
                {part_facets}
            )
            SELECT grp, 'matched', NULL, NULL, count(*) FROM sets GROUP BY grp
            UNION ALL
            SELECT grp, 'taxa_distinct', NULL, NULL, count(DISTINCT scientific_name) FROM sets GROUP BY grp
            UNION ALL
            SELECT grp, 'localities_distinct', NULL, NULL, count(DISTINCT locality_id) FROM events GROUP BY grp
            UNION ALL
            SELECT s.grp, 'collectors_distinct', NULL, NULL, count(DISTINCT ra.agent_id)
            FROM record_agents ra JOIN sets s ON s.id = ra.id WHERE ra.agent_role = 'collector' GROUP BY s.grp
            UNION ALL
            SELECT grp, 'year_min', NULL, NULL, min({began}) FROM dates GROUP BY grp
            UNION ALL
            SELECT grp, 'year_max', NULL, NULL, max({ended}) FROM dates GROUP BY grp
            {pairings}
            UNION ALL
            (SELECT * FROM facets
             QUALIFY row_number() OVER (PARTITION BY grp, stat ORDER BY n DESC, type, value) <= {FACET_SIZE})
        """)
