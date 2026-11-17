"""Taxon synonyms from a Catalogue of Life release on ChecklistBank.

ChecklistBank's name matcher picks the usage a name means, telling homonyms
apart by the higher classification it is given. The usage is followed to its
accepted taxon, and that taxon's accepted name and synonyms are the answer.

A release never changes, so whatever a lookup found is cached for the life of
the process. A failed lookup raises `SynonymsUnavailableError` and is not cached.
"""

import json
from collections.abc import Callable
from functools import lru_cache
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_DATASET = "COL26.10XR"
"""Catalogue of Life 2026/10, extended release."""

API = "https://api.checklistbank.org"

TIMEOUT = 5.0
"""Seconds per request. A search waits on it, so it stays short."""

CACHE_SIZE = 4096

# Misapplied names are left out: they are names used wrongly, not for this taxon.
_SYNONYM_GROUPS = ("homotypic", "heterotypic")

Classification = tuple[tuple[str, str], ...]
"""`(rank, name)` pairs above the name, in the matcher's rank parameters:
`kingdom`, `phylum`, `class`, `order`, `family`, `genus`…"""


class SynonymsUnavailableError(Exception):
    """ChecklistBank did not answer, or answered with something unreadable."""


Fetch = Callable[[str], Any]
"""Gets a URL and returns its decoded JSON."""


def _fetch(url: str) -> Any:  # noqa: ANN401 - JSON
    # A generic client name: the API asks nothing more of it.
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "vulpes"})  # noqa: S310 - fixed https base
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.load(response)
    except (URLError, TimeoutError, OSError, ValueError) as e:
        raise SynonymsUnavailableError(str(e)) from e


class Synonyms:
    def __init__(self, dataset: str = DEFAULT_DATASET, fetch: Fetch = _fetch) -> None:
        self._base = f"{API}/dataset/{quote(dataset, safe='')}"
        self._fetch = fetch
        self.lookup = lru_cache(maxsize=CACHE_SIZE)(self._lookup)

    def _lookup(self, rank: str | None, name: str, classification: Classification = ()) -> tuple[str, ...]:
        """The accepted name of the taxon `name` matches, then its synonyms.
        Empty when the matcher finds no single usage. Use `lookup`, which
        caches this."""
        params = {"scientificName": name, **dict(classification)}
        if rank:
            params["rank"] = rank
        found = self._fetch(f"{self._base}/match/nameusage?{urlencode(params)}")
        usage = found.get("usage")
        if not found.get("match") or not usage:
            return ()

        # A synonym's parent is its accepted taxon, first in its classification.
        if usage.get("status") in {"accepted", "provisionally accepted"}:
            accepted_id, accepted_name = usage["id"], usage["name"]
        else:
            accepted = next((c for c in usage.get("classification") or () if c.get("id") == usage.get("parentId")), None)
            if accepted is None:
                return ()
            accepted_id, accepted_name = accepted["id"], accepted["name"]

        synonyms = self._fetch(f"{self._base}/taxon/{quote(accepted_id, safe='')}/synonyms")
        names = [accepted_name]
        for group in _SYNONYM_GROUPS:
            names.extend(u["name"]["scientificName"] for u in synonyms.get(group) or ())
        return tuple(dict.fromkeys(names))
