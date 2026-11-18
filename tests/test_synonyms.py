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
from vulpes.models.responses import SearchSummary
from vulpes.synonyms import Synonyms, SynonymsUnavailableError

SHREW, SHREW_AGAIN, MOUSE, TICK, OTHER_SHREW = "MSB:Mamm:1", "MSB:Host:1", "MSB:Mamm:2", "MSB:Para:2", "DMNS:Mamm:1"

MATCHES = {
    "mammal sorex": {"id": "S", "status": "accepted"},
    "insect sorex": {"id": "B", "status": "synonym", "parentId": "I"},
    "peromyscus": {"id": "P", "status": "synonym", "parentId": "S"},
}

INFO = {
    "S": {
        "usage": {
            "id": "S",
            "sectorKey": 1,
            "referenceIds": ["r1", "gone"],
            "name": {"scientificName": "Sorex", "authorship": "Linnaeus, 1758", "link": "https://mammals.example/sorex"},
        },
        "references": {"r1": {"citation": "Smith, A. (2005). Shrews of the world."}},
        "synonyms": {
            "heterotypic": [
                {"sectorKey": 2, "name": {"scientificName": "Peromyscus", "authorship": "Gloger, 1841"}},
                {"name": {"scientificName": "Neosorex"}},
            ],
            "misapplied": [{"name": {"scientificName": "Misused"}}],
        },
    },
    "I": {
        "usage": {"id": "I", "name": {"scientificName": "Ixodes"}},
        "synonyms": {"heterotypic": [{"name": {"scientificName": "Sorex"}}]},
    },
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
        return MATCHES["mammal sorex" if hints.get("class") == "Mammalia" else "insect sorex"]
    return MATCHES.get(name)


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
        if path[-1] == "info":
            return INFO[path[-2]]
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


def summary(engine: DuckDbQueryEngine, **params: Any) -> SearchSummary:  # noqa: ANN401
    return engine.summary(ArctosQuery.model_validate(params))


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


def test_an_accepted_name_lists_its_synonyms(expanding: DuckDbQueryEngine) -> None:
    assert summary(expanding, taxon=["genus|Sorex"], synonyms=True).description == "Genus Sorex (also Peromyscus, Neosorex)."


def test_a_synonym_reaches_and_names_its_accepted_name(expanding: DuckDbQueryEngine) -> None:
    assert guids(expanding, taxon=["genus|peromyscus"], synonyms=True) == [OTHER_SHREW, SHREW_AGAIN, SHREW, MOUSE]
    found = summary(expanding, taxon=["genus|Peromyscus"], synonyms=True)
    assert found.description == "Genus Peromyscus (accepted as Sorex; also Neosorex)."


def test_the_summary_says_who_accepts_what_and_cites_them(expanding: DuckDbQueryEngine) -> None:
    synonymy = summary(expanding, taxon=["genus|Peromyscus"], synonyms=True).synonyms
    assert synonymy is not None
    assert synonymy.model_dump() == {
        "release": {
            "key": 316691,
            "alias": "COL26.10 XR",
            "title": "Catalogue of Life",
            "version": None,
            "doi": None,
            "citation": "COL & friends",
        },
        "taxa": [
            {
                "rank": "genus",
                "name": "Peromyscus",
                "status": "synonym",
                # A reference id the release does not resolve is left out; misapplied names are not synonyms.
                "accepted": {
                    "name": "Sorex",
                    "authorship": "Linnaeus, 1758",
                    "link": "https://mammals.example/sorex",
                    "source": 10,
                    "references": ["Smith, A. (2005). Shrews of the world."],
                },
                "synonyms": [
                    {"name": "Peromyscus", "authorship": "Gloger, 1841", "link": None, "source": 20},
                    {"name": "Neosorex", "authorship": None, "link": None, "source": None},
                ],
            }
        ],
        "sources": [
            {
                "key": 10,
                "alias": "Mammals",
                "title": "Mammals of the world",
                "version": None,
                "doi": "10.1/m",
                "citation": None,
            },
            {"key": 20, "alias": "Paper2020", "title": "A paper", "version": None, "doi": None, "citation": None},
        ],
    }


def test_a_name_the_matcher_cannot_place_matches_alone(expanding: DuckDbQueryEngine) -> None:
    assert guids(expanding, taxon=["genus|Ixodes"], synonyms=True) == [TICK]
    found = summary(expanding, taxon=["genus|Ixodes"], synonyms=True)
    assert found.description == "Genus Ixodes."
    assert found.synonyms is not None
    assert [(t.status, t.accepted) for t in found.synonyms.taxa] == [("unmatched", None)]
    assert found.synonyms.release is None


def test_a_lookup_is_cached(expanding: DuckDbQueryEngine, fake: Fake) -> None:
    guids(expanding, taxon=["genus|Sorex"], synonyms=True)
    guids(expanding, taxon=["genus|SOREX"], synonyms=True)
    assert len(fake.calls) == 1


def test_an_unreachable_catalogue_searches_the_name_alone_and_says_so(expanding: DuckDbQueryEngine, fake: Fake) -> None:
    fake.down = True
    assert guids(expanding, taxon=["genus|Peromyscus"], synonyms=True) == [MOUSE]
    found = summary(expanding, taxon=["genus|Peromyscus"], synonyms=True)
    assert found.description == "Genus Peromyscus (synonyms unavailable)."
    assert found.synonyms is not None
    assert [t.status for t in found.synonyms.taxa] == ["unavailable"]


def test_no_synonyms_asked_none_reported(expanding: DuckDbQueryEngine) -> None:
    assert summary(expanding, taxon=["genus|Peromyscus"]).synonyms is None


def test_a_name_filed_under_two_classifications_takes_the_synonyms_of_both(fake: Fake) -> None:
    mammal, insect = (("class", "Mammalia"),), (("class", "Insecta"),)
    expansion = Synonyms(fetch=fake).expand([("genus", "Sorex")], lambda _rank, _name: (mammal, insect))
    assert expansion.names("genus", "Sorex") == ("Peromyscus", "Neosorex", "Ixodes")
    assert [(t.status, t.accepted and t.accepted.name) for t in expansion.taxa] == [
        ("accepted", "Sorex"),
        ("synonym", "Ixodes"),
    ]
    assert [s.key for s in expansion.sources] == [10, 20]
