"""Taxon synonyms from a Catalogue of Life release on ChecklistBank.

ChecklistBank's name matcher picks the usage a name means, telling homonyms
apart by the higher classification it is given. The usage is followed to its
accepted taxon, and that taxon's accepted name and synonyms are the answer,
each with the dataset the release took it from.

A release never changes, so whatever a lookup found is cached for the life of
the process. A failed lookup raises `SynonymsUnavailableError` and is not cached.
"""

import html
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, NamedTuple
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_DATASET = "COL26.10XR"
"""Catalogue of Life 2026/10, extended release."""

API = "https://api.checklistbank.org"

TIMEOUT = 5.0
"""Seconds per request. A search waits on it, so it stays short."""

CACHE_SIZE = 4096

MAX_PARALLEL = 8
"""Requests in flight at once for one lookup's sources."""

# Misapplied names are left out: they are names used wrongly, not for this taxon.
_SYNONYM_GROUPS = ("homotypic", "heterotypic")

Classification = tuple[tuple[str, str], ...]
"""`(rank, name)` pairs above the name, in the matcher's rank parameters:
`kingdom`, `phylum`, `class`, `order`, `family`, `genus`…"""


class Source(NamedTuple):
    """A dataset on ChecklistBank."""

    key: int
    alias: str | None
    title: str | None
    version: str | None
    doi: str | None
    citation: str | None
    """Plain text: ChecklistBank formats it as HTML."""


class Synonym(NamedTuple):
    name: str
    source: Source | None
    """The dataset the release took the name from. `None` for a name the
    release's editors added themselves."""


class SynonymsUnavailableError(Exception):
    """ChecklistBank did not answer, or answered with something unreadable."""


Names = Mapping[tuple[str, str], tuple[str, ...] | None]
"""The other names each `(rank, name)` also matches. `None` says the lookup
failed, and the name matches alone."""


@dataclass(frozen=True)
class Expansion:
    """What the synonyms of one search's taxa came to."""

    names: Names = field(default_factory=dict)
    sources: tuple[tuple[Source, tuple[str, ...]], ...] = ()
    """The release with every name added, then each dataset the release took
    some of them from, with those names. Empty when no name was added."""


Classify = Callable[[str, str], tuple[Classification, ...]]
"""The higher classifications a `(rank, lowercased name)` is filed under,
most common first. A name filed under several, a homonym in the records
themselves, is matched once under each."""


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
        self.dataset = lru_cache(maxsize=CACHE_SIZE)(self._dataset)
        self._sector = lru_cache(maxsize=CACHE_SIZE)(self._sector_dataset)

    def expand(self, taxa: Iterable[tuple[str, str]], classify: Classify) -> Expansion:
        """The synonyms of each `(rank, name)`, a rank id or `scientific_name`,
        and the datasets they came from. A taxon whose lookup fails maps to
        `None` and adds nothing."""
        found = {taxon: self._synonyms_of(*taxon, classify) for taxon in dict.fromkeys(taxa)}
        added = [n for synonyms in found.values() for n in synonyms or ()]
        names = {taxon: None if s is None else tuple(n.name for n in s) for taxon, s in found.items()}
        if not added:
            return Expansion(names)
        by_source: dict[Source, list[str]] = {self.release(): [n.name for n in added]}
        for n in added:
            if n.source is not None:
                by_source.setdefault(n.source, []).append(n.name)
        return Expansion(names, tuple((source, tuple(dict.fromkeys(ns))) for source, ns in by_source.items()))

    def _synonyms_of(self, rank: str, name: str, classify: Classify) -> tuple[Synonym, ...] | None:
        """The other names the taxon `name` names goes by. `None` when they
        could not be looked up."""
        key = " ".join(name.lower().split())
        classifications = classify(rank, key) or ((),)
        matcher_rank = None if rank == "scientific_name" else rank
        try:
            with ThreadPoolExecutor(max_workers=len(classifications)) as pool:
                found = list(pool.map(lambda c: self.lookup(matcher_rank, key, c), classifications))
        except SynonymsUnavailableError as e:
            logger.warning("synonyms of %s %r unavailable: %s", rank, name, e)
            return None
        unique = {n.name: n for names in found for n in names if n.name.lower() != key}
        return tuple(unique.values())

    def release(self) -> Source:
        """The release synonyms are read from."""
        return self.dataset(None)

    def _dataset(self, key: int | None) -> Source:
        """Dataset `key`, or the release for `None`. Use `dataset`, which caches this."""
        d = self._fetch(self._base if key is None else f"{API}/dataset/{key}")
        citation = d.get("citation")
        if citation:
            citation = html.unescape(re.sub(r"<[^>]+>", "", citation)).strip()
        return Source(d["key"], d.get("alias"), d.get("title"), d.get("version"), d.get("doi"), citation)

    def _sector_dataset(self, sector: int) -> int:
        """The dataset a sector of the release copies from."""
        return int(self._fetch(f"{self._base}/sector/{sector}")["subjectDatasetKey"])

    def _lookup(self, rank: str | None, name: str, classification: Classification = ()) -> tuple[Synonym, ...]:
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
            accepted = usage
        else:
            accepted = next((c for c in usage.get("classification") or () if c.get("id") == usage.get("parentId")), None)
            if accepted is None:
                return ()

        synonyms = self._fetch(f"{self._base}/taxon/{quote(accepted['id'], safe='')}/synonyms")
        sectors: dict[str, int | None] = {accepted["name"]: accepted.get("sectorKey")}
        for group in _SYNONYM_GROUPS:
            for u in synonyms.get(group) or ():
                sectors.setdefault(u["name"]["scientificName"], u.get("sectorKey"))

        # Each source is two requests, and none waits on another. The release
        # is fetched here, where failing fails the lookup, so a search that
        # found synonyms always has it to cite.
        distinct = list(dict.fromkeys(k for k in sectors.values() if k is not None))
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            release = pool.submit(self.release)
            sources = dict(zip(distinct, pool.map(lambda k: self.dataset(self._sector(int(k))), distinct), strict=True))
            release.result()
        return tuple(Synonym(n, None if k is None else sources[k]) for n, k in sectors.items())
