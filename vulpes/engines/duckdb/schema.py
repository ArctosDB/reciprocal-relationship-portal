"""Everything the search form needs to render itself, built once at startup.

Two sources, with different promises:

- Code tables, fetched from Arctos apart from this service, are complete. A
  controlled value with no records still ships: an option that vanished between
  snapshots would change the meaning of a saved URL.
- Aggregations over the database are observed. They carry counts and list only
  the values the data holds.
"""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from vulpes.models.responses import (
    AttributeType,
    Facet,
    GuidPrefix,
    Limits,
    Rank,
    Relation,
    Schema,
    Sort,
    VocabValue,
)
from vulpes.models.search import PART_FIELDS, PER_PAGE, RANKS

MAX_RESULT_WINDOW = 100_000
"""How far the pager may browse. Nothing in DuckDB sets a bound; this one keeps
the offset of a page small enough to stay quick."""

MAX_EXPORT_ROWS = 100_000

PART_VOCABULARIES = {"part_name": "ctspecimen_part_name", "disposition": "ctdisposition"}
"""The code table holding each part field's values. A field not listed here is
free text."""

# Keys that are metadata on a code-table row. The one key left over is the
# value column, which each table names differently: `sex_cde`, `caste`, ...
_NOT_THE_VALUE = frozenset(
    {
        "description",
        "issue_url",
        "documentation_url",
        "collections",
        "recommend_for_collection_type",
        "search_terms",
        "value_code_table",
        "unit_code_table",
        "public",
        "definition",
        "collection_type",
    }
)

Row = dict[str, Any]


def _load(directory: Path, table: str) -> list[Row]:
    """Arctos ships a code table as `{"data": [...]}` or as a bare array."""
    doc = json.loads((directory / f"{table}.json").read_text("utf-8"))
    rows = doc.get("data", []) if isinstance(doc, dict) else doc
    return [row for row in rows if isinstance(row, dict)]


def _text(row: Row, key: str) -> str:
    value = row.get(key)
    # Arctos returns an array for some url fields. The first element is the url.
    if isinstance(value, list):
        value = value[0] if value else ""
    return value.strip() if isinstance(value, str) else ""


def _is_public(row: Row) -> bool:
    return str(row.get("public", 1)) != "0"


def _vocabulary(directory: Path, table: str) -> list[VocabValue]:
    rows = _load(directory, table)
    key = next((k for k in rows[0] if k not in _NOT_THE_VALUE), None) if rows else None
    if key is None:
        # A table with no rows still ships: every `vocabulary` must be a key of the map.
        return []
    values = [
        VocabValue(
            value=_text(row, key), description=_text(row, "description"), documentation_url=_text(row, "documentation_url")
        )
        for row in rows
        if _text(row, key)
    ]
    # A parent before its children, `ectoparasite` before `ectoparasite: flea`.
    # The form indents the hierarchy by this order.
    return sorted(values, key=lambda v: (v.value.count(":"), v.value.lower()))


def _collection_label(prefix: str, labels: Sequence[str]) -> tuple[str, str]:
    """`MSB:Mamm` as `("MSB", "Mammalogy")`. The code table lists only the full
    labels, so the abbreviation is matched as the start of one."""
    institution, found, code = prefix.partition(":")
    if not found:
        return "", ""
    return institution, next((label for label in labels if label.lower().startswith(code.lower())), "")


def _facets(cursor: duckdb.DuckDBPyConnection, column: str) -> list[tuple[str, int]]:
    """The observed values of one `records` column, the most used first."""
    return cursor.execute(
        f"SELECT trim({column}) AS value, count(*) AS n FROM records "  # noqa: S608 - the column is this module's own
        f"WHERE trim({column}) <> '' GROUP BY value ORDER BY n DESC, value"
    ).fetchall()


def build(cursor: duckdb.DuckDBPyConnection, directory: Path, columns: Sequence[str], snapshot: date) -> Schema:
    """`directory` holds the code tables. `columns` is every column a search may
    name in `cols`. `snapshot` is the day the data dates from."""
    types = _load(directory, "ctattribute_type")
    # A type Arctos marks non-public never reaches the client.
    public = [t for t in types if _is_public(t)]
    tables = dict.fromkeys(
        [*(_text(t, "value_code_table") for t in public), *PART_VOCABULARIES.values()],
    )
    tables.pop("", None)

    labels = [_text(row, "collection_cde") for row in _load(directory, "ctcollection_cde")]

    # A rank with no values in the snapshot is not offered. `scientific_name` is
    # not a rank, it is a whole-name match offered in the same dropdown, first.
    held = cursor.execute(f"SELECT {', '.join(f'count({r.column})' for r in RANKS)} FROM records").fetchall()[0]  # noqa: S608
    ranks = [Rank(id="scientific_name", label="Scientific name", field="scientific_name")]
    ranks += [Rank(id=r.id, label=r.label, field=r.column) for r, n in zip(RANKS, held, strict=True) if n]

    collectors = cursor.execute("""
        SELECT a.agent_name, count(DISTINCT ra.id) AS n
        FROM record_agents ra JOIN agents a ON a.agent_id = ra.agent_id
        WHERE ra.agent_role = 'collector' AND a.agent_name <> ''
        GROUP BY a.agent_name ORDER BY n DESC, a.agent_name
    """).fetchall()

    return Schema(
        snapshot_date=snapshot,
        columns=list(columns),
        ranks=ranks,
        attribute_types=sorted(
            (
                AttributeType(
                    id=_text(t, "attribute_type"),
                    label=_text(t, "attribute_type"),
                    description=_text(t, "description"),
                    vocabulary=_text(t, "value_code_table") or None,
                    units_table=_text(t, "unit_code_table") or None,
                )
                for t in public
            ),
            key=lambda t: t.id.lower(),
        ),
        part_fields=[
            AttributeType(
                id=field,
                label=field.replace("_", " "),
                description="",
                vocabulary=PART_VOCABULARIES.get(field),
                units_table=None,
            )
            for field in PART_FIELDS
        ],
        vocabularies={table: _vocabulary(directory, table) for table in sorted(tables)},
        countries=[Facet(value=v, count=n) for v, n in _facets(cursor, "country")],
        states=[Facet(value=v, count=n) for v, n in _facets(cursor, "state_prov")],
        collectors=[Facet(value=v, count=n) for v, n in collectors],
        relations=sorted(
            (
                Relation(value=_text(row, "id_references"), description=_text(row, "description"))
                for row in _load(directory, "ctid_references")
                if _text(row, "id_references")
            ),
            key=lambda r: r.value.lower(),
        ),
        guid_prefixes=[
            GuidPrefix(value=v, count=n, institution=institution, collection_cde=label)
            for v, n in _facets(cursor, "guid_prefix")
            for institution, label in [_collection_label(v, labels)]
        ],
        # The one order the search pages in.
        sorts=[Sort(id="guid_asc", label="Catalog number")],
        limits=Limits(
            page_size=PER_PAGE,
            max_per_page=PER_PAGE,
            max_result_window=MAX_RESULT_WINDOW,
            max_export_rows=MAX_EXPORT_ROWS,
        ),
        nonpublic_types_dropped=[_text(t, "attribute_type") for t in types if not _is_public(t)],
    )
