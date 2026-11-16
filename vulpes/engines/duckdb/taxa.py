"""Taxon name suggestions for the form's autocomplete.

Every distinct `(rank, name)` of the snapshot is counted once at startup and
held in memory, sorted by name, so a keystroke is a binary search rather than a
scan of the records.
"""

from bisect import bisect_left
from heapq import nsmallest
from itertools import pairwise

import duckdb

from vulpes.models.responses import Taxon
from vulpes.models.search import RANKS, Rank

MIN_QUERY = 2
"""A shorter query matches nothing: it would only return the most common names
of all, which is not a suggestion."""

MAX_LIMIT = 20

# `scientific_name` is offered beside the ranks but is not part of their chain,
# so it has no parent, and neither does the highest rank.
_COLUMNS: list[tuple[str, str, Rank | None]] = [
    ("scientific_name", "scientific_name", None),
    (RANKS[0].id, RANKS[0].column, None),
    *((rank.id, rank.column, above) for above, rank in pairwise(RANKS)),
]
# Names with equal counts list in this order, whole names before ranks.
_ORDER = {rank: i for i, (rank, _, _) in enumerate(_COLUMNS)}


class TaxonIndex:
    def __init__(self, cursor: duckdb.DuckDBPyConnection) -> None:
        # A rank column joins the names of several determinations with `;`.
        # Each is counted, and takes the first name of the rank above as its parent.
        branches = [
            f"""
            SELECT '{rank}', trim(x), count(*),
                {f"'{above.id}', any_value(nullif(trim(split_part({above.column}, ';', 1)), ''))" if above else "NULL, NULL"}
            FROM (SELECT * FROM records WHERE {column} <> ''), unnest(string_split({column}, ';')) AS u(x)
            WHERE trim(x) <> '' GROUP BY ALL
            """  # noqa: S608 - the names are this module's own columns
            for rank, column, above in _COLUMNS
        ]
        rows = cursor.execute(" UNION ALL ".join(branches)).fetchall()
        taxa = [
            Taxon(
                rank=rank,
                name=name,
                record_count=n,
                parent_rank=parent_rank if parent_name else None,
                parent_name=parent_name,
            )
            for rank, name, n, parent_rank, parent_name in rows
        ]
        taxa.sort(key=lambda t: t.name.lower())
        self._taxa = taxa
        self._keys = [t.name.lower() for t in taxa]

    def matching(self, q: str, rank: str | None = None, limit: int = 10) -> list[Taxon]:
        """The names starting with `q`, in any case, with the most records
        first. `rank` keeps one rank's names."""
        prefix = q.strip().lower()
        if len(prefix) < MIN_QUERY:
            return []
        # Every key starting with the prefix sorts between it and the prefix
        # followed by the highest character.
        start = bisect_left(self._keys, prefix)
        end = bisect_left(self._keys, prefix + chr(0x10FFFF))
        found = (t for t in self._taxa[start:end] if rank is None or t.rank == rank)
        return nsmallest(
            max(0, min(limit, MAX_LIMIT)),
            found,
            key=lambda t: (-t.record_count, t.name.lower(), _ORDER[t.rank]),
        )
