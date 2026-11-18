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
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from vulpes.models.responses import AcceptedName, Checklist, Synonymy, TaxonName, TaxonSynonymy

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

Classify = Callable[[str, str], tuple[Classification, ...]]
"""The higher classifications a `(rank, lowercased name)` is filed under,
most common first. A name filed under several, a homonym in the records
themselves, is matched once under each."""

Concept = tuple[AcceptedName, tuple[TaxonName, ...]]
"""One taxon as the release has it: its accepted name, then its synonyms."""


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
        self._lookup = lru_cache(maxsize=CACHE_SIZE)(self._lookup_uncached)
        self._dataset = lru_cache(maxsize=CACHE_SIZE)(self._dataset_uncached)
        self._sector = lru_cache(maxsize=CACHE_SIZE)(self._sector_uncached)

    def expand(self, taxa: Iterable[tuple[str, str]], classify: Classify) -> Synonymy:
        """What each `(rank, name)`, a rank id or `scientific_name`, is in the
        release, and the datasets its names came from."""
        found = [t for taxon in dict.fromkeys(taxa) for t in self._match(*taxon, classify)]
        matched = [t for t in found if t.accepted is not None]
        if not matched:
            return Synonymy(release=None, taxa=found, sources=[])
        names = (n for t in matched for n in (t.accepted, *t.synonyms) if n is not None)
        keys = dict.fromkeys(n.source for n in names if n.source is not None)
        # Every one was fetched by the lookups, so these are cached.
        return Synonymy(release=self._dataset(None), taxa=found, sources=[self._dataset(k) for k in keys])

    def _match(self, rank: str, name: str, classify: Classify) -> list[TaxonSynonymy]:
        """One entry per taxon `name` matches under the classifications the
        records file it under."""
        key = " ".join(name.lower().split())
        classifications = classify(rank, key) or ((),)
        matcher_rank = None if rank == "scientific_name" else rank
        try:
            with ThreadPoolExecutor(max_workers=len(classifications)) as pool:
                found = list(pool.map(lambda c: self._lookup(matcher_rank, key, c), classifications))
        except SynonymsUnavailableError as e:
            logger.warning("synonyms of %s %r unavailable: %s", rank, name, e)
            return [TaxonSynonymy(rank=rank, name=name, status="unavailable", accepted=None, synonyms=[])]
        # Two classifications usually lead to the same taxon.
        concepts = {c[0].name: c for c in found if c is not None}.values()
        if not concepts:
            return [TaxonSynonymy(rank=rank, name=name, status="unmatched", accepted=None, synonyms=[])]
        return [
            TaxonSynonymy(
                rank=rank,
                name=name,
                status="accepted" if accepted.name.lower() == key else "synonym",
                accepted=accepted,
                synonyms=list(synonyms),
            )
            for accepted, synonyms in concepts
        ]

    def _dataset_uncached(self, key: int | None) -> Checklist:
        """Dataset `key`, or the release for `None`."""
        d = self._fetch(self._base if key is None else f"{API}/dataset/{key}")
        citation = d.get("citation")
        if citation:
            citation = html.unescape(re.sub(r"<[^>]+>", "", citation)).strip()
        return Checklist(
            key=d["key"],
            alias=d.get("alias"),
            title=d.get("title"),
            version=d.get("version"),
            doi=d.get("doi"),
            citation=citation,
        )

    def _sector_uncached(self, sector: int) -> int:
        """The dataset a sector of the release copies from, its metadata fetched
        into the cache."""
        key = int(self._fetch(f"{self._base}/sector/{sector}")["subjectDatasetKey"])
        self._dataset(key)
        return key

    def _lookup_uncached(self, rank: str | None, name: str, classification: Classification = ()) -> Concept | None:
        """The taxon `name` matches, or `None` when the matcher finds no single usage."""
        params = {"scientificName": name, **dict(classification)}
        if rank:
            params["rank"] = rank
        found = self._fetch(f"{self._base}/match/nameusage?{urlencode(params)}")
        usage = found.get("usage")
        if not found.get("match") or not usage:
            return None

        # A synonym's parent is its accepted taxon.
        accepted_id = usage["id"] if usage.get("status") in {"accepted", "provisionally accepted"} else usage.get("parentId")
        if not accepted_id:
            return None
        info = self._fetch(f"{self._base}/taxon/{quote(accepted_id, safe='')}/info")
        accepted = info["usage"]
        references = info.get("references") or {}
        cited = [c for r in accepted.get("referenceIds") or () if (c := (references.get(r) or {}).get("citation"))]
        usages = [u for group in _SYNONYM_GROUPS for u in (info.get("synonyms") or {}).get(group) or ()]

        # Each source is two requests, and none waits on another. The release
        # is fetched here, where failing fails the lookup, so a search that
        # found a taxon always has it to cite.
        sectors = list(dict.fromkeys(u["sectorKey"] for u in (accepted, *usages) if u.get("sectorKey") is not None))
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            release = pool.submit(self._dataset, None)
            keys = dict(zip(sectors, pool.map(self._sector, sectors), strict=True))
            release.result()

        def named(u: dict[str, Any]) -> dict[str, Any]:
            n = u.get("name") or {}
            link = n.get("link") or u.get("link")
            return {
                "name": n["scientificName"],
                "authorship": n.get("authorship"),
                "link": link,
                "source": keys.get(u.get("sectorKey")),
            }

        synonyms = {s["name"]: TaxonName(**s) for s in map(named, usages)}
        return AcceptedName(**named(accepted), references=cited), tuple(synonyms.values())
