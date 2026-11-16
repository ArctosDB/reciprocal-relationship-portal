"""The related-records table and the CSV export, over the six records of
`conftest.py` and through the routes the portal calls."""

import csv
import io
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vulpes.engines.duckdb import DuckDbQueryEngine
from vulpes.models.query import ArctosQuery
from vulpes.models.responses import SearchResults
from vulpes.models.search import BadQueryError
from vulpes.routes.router import make_router

SHREW, SHREW_AGAIN, MOUSE, CESTODE, TICK = "MSB:Mamm:1", "MSB:Host:1", "MSB:Mamm:2", "MSB:Para:1", "MSB:Para:2"

MAP_HEADER = [
    "guid",
    "related_guid",
    "related_scientific_name",
    "scientific_name",
    "country",
    "state_prov",
    "dec_lat",
    "dec_long",
    "coordinateuncertaintyinmeters",
]


@pytest.fixture
def client(engine: DuckDbQueryEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Exports are written here, so a test can see that none is left behind.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    app = FastAPI()
    app.include_router(make_router(engine))
    return TestClient(app)


def related(engine: DuckDbQueryEngine, **params: Any) -> SearchResults:  # noqa: ANN401
    return engine.relations(ArctosQuery.model_validate(params))


def reached(engine: DuckDbQueryEngine, **params: Any) -> list[tuple[str, list[str]]]:  # noqa: ANN401
    return [(r["guid"], r["related_guid"]) for r in related(engine, **params).records]


def table(client: TestClient, path: str = "/download", **params: str | list[str]) -> list[list[str]]:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return list(csv.reader(io.StringIO(response.text)))


# ---- /relations -----------------------------------------------------------


def test_a_tab_lists_each_reached_record_once_with_the_records_it_was_reached_from(engine: DuckDbQueryEngine) -> None:
    results = related(engine, taxon=["genus|Sorex|host of parasite"], tab="host of parasite")
    assert results.column_order[:3] == ["guid", "related_guid", "related_scientific_name"]
    # Two shrew records point at one cestode lot. The parasite outside the database has no record to list.
    assert [(r["guid"], r["related_guid"]) for r in results.records] == [(CESTODE, [SHREW_AGAIN, SHREW])]
    # What it is a parasite of, at a glance: both records are the one species.
    assert results.records[0]["related_scientific_name"] == ["Sorex cinereus"]
    # `context` is the records the form matched, `matched` the distinct records they reach.
    assert (results.summary.context, results.summary.matched, results.summary.taxon) == (2, 1, None)
    assert results.total.value == 1
    # It is a record like any other, with its own nested fields.
    assert results.records[0]["scientific_name"] == "Cestoda"
    assert {link["relationship"] for link in results.records[0]["relations"]} == {"parasite of"}


def test_a_tab_shows_one_relationship_of_those_the_search_asked_for(engine: DuckDbQueryEngine) -> None:
    both = ["genus|Sorex|host of parasite; same individual as"]
    assert reached(engine, taxon=both, tab="same individual as") == [(SHREW_AGAIN, [SHREW])]
    assert reached(engine, taxon=both, tab="host of parasite") == [(CESTODE, [SHREW_AGAIN, SHREW])]
    # No tab is every relationship at once, a record reached by two of them listed once.
    assert reached(engine, taxon=both) == [(SHREW_AGAIN, [SHREW]), (CESTODE, [SHREW_AGAIN, SHREW])]
    # A relationship no row asked for holds nothing, though the shrew does have such a link.
    assert reached(engine, taxon=["genus|Sorex|host of parasite"], tab="same individual as") == []


def test_the_form_s_filters_reach_the_matched_records_and_not_the_related_ones(engine: DuckDbQueryEngine) -> None:
    # The cestode was collected in New Mexico; the Colorado filter is on the shrew.
    assert reached(engine, taxon=["genus|Sorex|host of parasite"], state=["Colorado"], tab="host of parasite") == [
        (CESTODE, [SHREW_AGAIN])
    ]
    # A Related taxon narrows which links are followed.
    hosts = ["class|Mammalia|host of parasite|genus|Ixodes"]
    assert reached(engine, taxon=hosts, tab="host of parasite") == [(TICK, [MOUSE])]


def test_a_search_that_asks_for_no_relation_reaches_nothing(engine: DuckDbQueryEngine) -> None:
    results = related(engine, taxon=["genus|Sorex"], tab="host of parasite")
    assert results.records == []
    assert (results.summary.context, results.summary.matched) == (3, 0)
    # An empty table still declares its columns.
    assert results.column_order[:3] == ["guid", "related_guid", "related_scientific_name"]


# ---- /download ------------------------------------------------------------


def test_the_map_export_of_the_matched_records(client: TestClient, tmp_path: Path) -> None:
    rows = table(client, taxon="genus|Sorex|host of parasite", cols="map")
    assert rows == [
        MAP_HEADER,
        [SHREW_AGAIN, CESTODE, "Cestoda", "Sorex cinereus", "United States", "Colorado", "", "", ""],
        # The second parasite is outside the database: no guid, only the name Arctos cached.
        [
            SHREW,
            CESTODE,
            "Cestoda; Hymenolepis diminuta",
            "Sorex cinereus",
            "United States",
            "New Mexico",
            "35.2",
            "-106.4",
            "100",
        ],
    ]
    # The file DuckDB wrote is gone once it has been sent.
    assert list(tmp_path.iterdir()) == []


def test_a_tab_exports_the_related_records(client: TestClient) -> None:
    rows = table(client, taxon="genus|Sorex|host of parasite", tab="host of parasite", cols="map")
    # Several guids share the one cell. Both shrew records carry one name, listed once.
    assert rows == [
        MAP_HEADER,
        [
            CESTODE,
            f"{SHREW_AGAIN}; {SHREW}",
            "Sorex cinereus",
            "Cestoda",
            "United States",
            "New Mexico",
            "35.2",
            "-106.4",
            "100",
        ],
    ]


def test_the_default_export_carries_nested_columns_as_json(client: TestClient) -> None:
    header, row = table(client, taxon="genus|Peromyscus")
    # No relation was asked for, so there is no `related_guid` column.
    assert header == [
        "guid",
        "scientific_name",
        "country",
        "state_prov",
        "dec_lat",
        "dec_long",
        "attributedetail",
        "relations",
    ]
    assert row[:6] == [MOUSE, "Peromyscus maniculatus", "United States", "Alaska", "64.8", "-147.7"]
    assert {a["attribute_type"] for a in json.loads(row[6])} == {"detected", "sex", "weight"}
    assert {link["relationship"] for link in json.loads(row[7])} == {"host of parasite", "ate"}


def test_cols_names_the_columns_of_the_export(client: TestClient) -> None:
    params = {"prefix": "MSB:Para", "cols": "guid,genus,spec_locality"}
    rows = table(client, **params)
    assert rows == [
        ["guid", "genus", "spec_locality"],
        [CESTODE, "", "Sandia Mountains, Cibola National Forest"],
        [TICK, "Ixodes; Ixodes", "Fairbanks"],
    ]

    response = client.get("/download", params={"taxon": "genus|Sorex"})
    assert response.headers["content-type"].startswith("text/csv")
    assert re.fullmatch(r'attachment; filename="arctos_\d{8}T\d{6}Z\.csv"', response.headers["content-disposition"])


def test_an_export_past_the_cap_is_refused_whole(
    engine: DuckDbQueryEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(engine.schema().limits, "max_export_rows", 2)
    with pytest.raises(BadQueryError, match="3 rows"):
        engine.download(ArctosQuery(taxon=["genus|Sorex"]))
    # The cap is on the rows of the table exported, here one cestode.
    engine.download(ArctosQuery(taxon=["genus|Sorex|host of parasite"], tab="host of parasite"))
    with pytest.raises(BadQueryError, match="nope"):
        engine.download(ArctosQuery(cols="guid,nope"))


# ---- caching --------------------------------------------------------------


def test_answers_are_cacheable_until_the_next_snapshot(client: TestClient) -> None:
    assert client.get("/schema").headers["cache-control"] == "public, max-age=900"
    assert client.get("/taxa", params={"q": "so"}).headers["cache-control"] == "public, max-age=3600"
    assert client.get("/berkeleymapper.xml").headers["cache-control"] == "public, max-age=3600"
    # The portal asks for the same search again on every tab switch.
    for path in ("/search", "/summary", "/relations"):
        assert client.get(path, params={"taxon": "genus|Sorex"}).headers["cache-control"] == "public, max-age=300"
    # An error is not an answer worth keeping.
    assert "cache-control" not in client.get("/search", params={"page": "0"}).headers


def test_the_schema_is_the_same_document_compressed_or_not(client: TestClient, engine: DuckDbQueryEngine) -> None:
    expected = json.loads(engine.schema().model_dump_json())
    packed = client.get("/schema", headers={"Accept-Encoding": "gzip"})
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.json() == expected
    plain = client.get("/schema", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.json() == expected
    assert packed.headers["vary"] == plain.headers["vary"] == "Accept-Encoding"
    # The route still documents the shape it returns.
    assert "Schema" in client.get("/openapi.json").text


# ---- /berkeleymapper.xml --------------------------------------------------


def test_the_mapper_config_names_every_column_of_the_map_export(client: TestClient) -> None:
    response = client.get("/berkeleymapper.xml")
    assert response.headers["content-type"].startswith("application/xml")
    # BerkeleyMapper binds a column to a concept by its header, and by position
    # when no concept has that alias, which puts values under the wrong label.
    aliases = set(re.findall(r'<concept [^>]*alias="([^"]+)"', response.text))
    for query in ({"taxon": "genus|Sorex"}, {"taxon": "genus|Sorex|host of parasite", "tab": "host of parasite"}):
        header = table(client, cols="map", **query)[0]
        assert set(header) <= aliases
    # The two concepts that put a record on the map.
    assert 'datatype="darwin:decimallatitude" alias="dec_lat"' in response.text
    assert 'datatype="darwin:decimallongitude" alias="dec_long"' in response.text
