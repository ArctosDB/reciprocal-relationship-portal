"""Synonym expansion against a fake ChecklistBank, on the records of `conftest.py`.
The catalogue below is made up: it calls Peromyscus a synonym of Sorex, and has
an insect Sorex, a synonym of Ixodes, which its matcher picks unless told the
Sorex meant is a mammal. Sorex comes from a mammal checklist, Peromyscus from
a paper, and Neosorex from the release's own editors."""

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from vulpes.engines.duckdb import DuckDbQueryEngine
from vulpes.models.query import ArctosQuery
from vulpes.synonyms import Synonyms, SynonymsUnavailableError

SHREW, SHREW_AGAIN, MOUSE, TICK, OTHER_SHREW = "MSB:Mamm:1", "MSB:Host:1", "MSB:Mamm:2", "MSB:Para:2", "DMNS:Mamm:1"

MAMMAL_SOREX = {"id": "S", "name": "Sorex", "status": "accepted", "sectorKey": 1}
INSECT_SOREX = {
    "id": "B",
    "name": "Sorex",
    "status": "synonym",
    "parentId": "I",
    "classification": [{"id": "I", "name": "Ixodes"}],
}
PEROMYSCUS = {
    "id": "P",
    "name": "Peromyscus",
    "status": "synonym",
    "parentId": "S",
    "classification": [{"id": "S", "name": "Sorex", "sectorKey": 1}],
}

SYNONYMS = {
    "S": {
        "heterotypic": [{"name": {"scientificName": "Peromyscus"}, "sectorKey": 2}, {"name": {"scientificName": "Neosorex"}}]
    },
    "I": {"heterotypic": [{"name": {"scientificName": "Sorex"}}]},
}

SECTORS = {"1": 10, "2": 20}
DATASETS = {
    "COL26.10XR": {
        "key": 316691,
        "alias": "COL26.10 XR",
        "title": "Catalogue of Life",
        "citation": "<i>COL</i> &amp; friends",
    },
    "10": {"key": 10, "alias": "Mammals", "title": "Mammals of the world", "doi": "10.1/m"},
    "20": {"key": 20, "alias": "Paper2020", "title": "A paper"},
}


def match(name: str, hints: dict[str, str]) -> dict[str, Any] | None:
    if name == "sorex":
        return MAMMAL_SOREX if hints.get("class") == "Mammalia" else INSECT_SOREX
    if name == "peromyscus":
        return PEROMYSCUS
    return None


class Fake:
    def __init__(self) -> None:
        self.down = False
        self.calls: list[dict[str, str]] = []

    def __call__(self, url: str) -> Any:  # noqa: ANN401
        if self.down:
            raise SynonymsUnavailableError
        parsed = urlparse(url)
        path = parsed.path.split("/")
        if path[-2] == "sector":
            return {"subjectDatasetKey": SECTORS[path[-1]]}
        if path[-2] == "dataset":
            return DATASETS[path[-1]]
        if parsed.path.endswith("/synonyms"):
            return SYNONYMS.get(parsed.path.split("/")[-2], {})
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.calls.append(params)
        usage = match(params["scientificName"].lower(), params)
        return {"match": usage is not None, "usage": usage}


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def expanding(engine: DuckDbQueryEngine, fake: Fake) -> Iterator[DuckDbQueryEngine]:
    other = DuckDbQueryEngine(engine.path, synonyms=Synonyms(fetch=fake))
    yield other
    other.close()


def guids(engine: DuckDbQueryEngine, **params: Any) -> list[str]:  # noqa: ANN401
    return [r["guid"] for r in engine.search(ArctosQuery.model_validate(params)).records]


def describe(engine: DuckDbQueryEngine, **params: Any) -> str | None:  # noqa: ANN401
    return engine.summary(ArctosQuery.model_validate(params)).description


def test_synonyms_are_appended_only_when_asked(expanding: DuckDbQueryEngine) -> None:
    assert guids(expanding, taxon=["genus|Sorex"]) == [OTHER_SHREW, SHREW_AGAIN, SHREW]
    assert guids(expanding, taxon=["genus|Sorex"], synonyms=True) == [OTHER_SHREW, SHREW_AGAIN, SHREW, MOUSE]


def test_the_matcher_is_told_the_snapshot_s_classification(expanding: DuckDbQueryEngine, fake: Fake) -> None:
    # Told Sorex is a mammal, the matcher never picks the insect, so its Ixodes is not added.
    assert TICK not in guids(expanding, taxon=["genus|Sorex"], synonyms=True)
    assert fake.calls == [
        {
            "scientificName": "sorex",
            "rank": "genus",
            "phylum": "Chordata",
            "class": "Mammalia",
            "order": "Eulipotyphla",
            "family": "Soricidae",
        }
    ]


def test_a_synonym_reaches_its_accepted_name(expanding: DuckDbQueryEngine) -> None:
    assert guids(expanding, taxon=["genus|peromyscus"], synonyms=True) == [OTHER_SHREW, SHREW_AGAIN, SHREW, MOUSE]
    assert (
        describe(expanding, taxon=["genus|Peromyscus"], synonyms=True) == "Genus Peromyscus (or its synonyms Sorex, Neosorex)."
    )


def test_a_name_the_matcher_cannot_place_matches_alone(expanding: DuckDbQueryEngine) -> None:
    assert guids(expanding, taxon=["genus|Ixodes"], synonyms=True) == [TICK]
    assert describe(expanding, taxon=["genus|Ixodes"], synonyms=True) == "Genus Ixodes."


def test_a_lookup_is_cached(expanding: DuckDbQueryEngine, fake: Fake) -> None:
    guids(expanding, taxon=["genus|Sorex"], synonyms=True)
    guids(expanding, taxon=["genus|SOREX"], synonyms=True)
    assert len(fake.calls) == 1


def test_an_unreachable_catalogue_searches_the_name_alone_and_says_so(expanding: DuckDbQueryEngine, fake: Fake) -> None:
    fake.down = True
    assert guids(expanding, taxon=["genus|Peromyscus"], synonyms=True) == [MOUSE]
    assert describe(expanding, taxon=["genus|Peromyscus"], synonyms=True) == "Genus Peromyscus (synonyms unavailable)."


def test_the_summary_lists_the_sources_of_the_synonyms_it_added(expanding: DuckDbQueryEngine) -> None:
    summary = expanding.summary(ArctosQuery.model_validate({"taxon": ["genus|Peromyscus"], "synonyms": True}))
    assert [(s.key, s.alias, s.citation, s.names) for s in summary.sources] == [
        (316691, "COL26.10 XR", "COL & friends", ["Sorex", "Neosorex"]),
        (10, "Mammals", None, ["Sorex"]),
    ]


def test_no_synonyms_no_sources(expanding: DuckDbQueryEngine, fake: Fake) -> None:
    assert expanding.summary(ArctosQuery.model_validate({"taxon": ["genus|Peromyscus"]})).sources == []
    fake.down = True
    assert expanding.summary(ArctosQuery.model_validate({"taxon": ["genus|Peromyscus"], "synonyms": True})).sources == []


def test_a_name_filed_under_two_classifications_takes_the_synonyms_of_both(fake: Fake) -> None:
    mammal, insect = (("class", "Mammalia"),), (("class", "Insecta"),)
    expansion = Synonyms(fetch=fake).expand([("genus", "Sorex")], lambda _rank, _name: (mammal, insect))
    assert expansion.names == {("genus", "Sorex"): ("Peromyscus", "Neosorex", "Ixodes")}
    assert [(source.key, names) for source, names in expansion.sources] == [
        (316691, ("Peromyscus", "Neosorex", "Ixodes")),
        (20, ("Peromyscus",)),
    ]
