"""Searches against the six records of `conftest.py`, sent as the portal sends
them. Read the expected guids off that file's header."""

from typing import Any

import pytest

from vulpes.engines.duckdb import DuckDbQueryEngine
from vulpes.models.query import ArctosQuery
from vulpes.models.responses import SearchResults
from vulpes.models.search import BadQueryError

SHREW, SHREW_AGAIN, MOUSE, CESTODE, TICK, OTHER_SHREW = (
    "MSB:Mamm:1",
    "MSB:Host:1",
    "MSB:Mamm:2",
    "MSB:Para:1",
    "MSB:Para:2",
    "DMNS:Mamm:1",
)


def run(engine: DuckDbQueryEngine, **params: Any) -> SearchResults:  # noqa: ANN401
    return engine.search(ArctosQuery.model_validate(params))


def guids(engine: DuckDbQueryEngine, **params: Any) -> list[str]:  # noqa: ANN401
    return [record["guid"] for record in run(engine, **params).records]


# ---- taxon ----------------------------------------------------------------


def test_a_rank_matches_its_own_column_in_any_case_and_pages_in_guid_order(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, taxon=["genus|sorex"]) == [OTHER_SHREW, SHREW_AGAIN, SHREW]
    # Cestoda is a class here, and a genus nowhere.
    assert guids(engine, taxon=["class|Cestoda"]) == [CESTODE]
    assert guids(engine, taxon=["genus|Cestoda"]) == []


def test_a_rank_column_holding_several_determinations_matches_on_any_one(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, taxon=["genus|Ixodes"]) == [TICK]
    assert guids(engine, taxon=["species|ixodes pacificus"]) == [TICK]
    assert guids(engine, taxon=["species|Ixodes"]) == []


def test_scientific_name_is_a_phrase_of_whole_words(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, taxon=["scientific_name|sorex"]) == [OTHER_SHREW, SHREW_AGAIN, SHREW]
    assert guids(engine, taxon=["scientific_name|Sorex cinereus"]) == [SHREW_AGAIN, SHREW]
    assert guids(engine, taxon=["scientific_name|cinereus Sorex"]) == []
    assert guids(engine, taxon=["scientific_name|Sor"]) == []


def test_taxon_rows_or_together(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, taxon=["genus|Peromyscus", "class|Cestoda"]) == [MOUSE, CESTODE]


# ---- relationships --------------------------------------------------------


def test_a_pairwise_search_selects_the_searched_side_and_names_what_it_reaches(engine: DuckDbQueryEngine) -> None:
    results = run(engine, taxon=["genus|Sorex|host of parasite|class|Cestoda"])
    assert results.column_order[:3] == ["guid", "related_guid", "related_scientific_name"]
    assert [r["related_scientific_name"] for r in results.records] == [["Cestoda"], ["Cestoda"]]
    assert [(r["guid"], r["related_guid"]) for r in results.records] == [
        (SHREW_AGAIN, [CESTODE]),
        (SHREW, [CESTODE]),
    ]
    # The record still carries every link it has; `related_guid` is the subset asked for.
    assert {link["relationship"] for link in results.records[1]["relations"]} == {"host of parasite", "same individual as"}

    # The same link from the other side is a different search.
    assert guids(engine, taxon=["class|Cestoda|parasite of|genus|Sorex"]) == [CESTODE]


def test_a_row_may_name_only_the_relation(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, taxon=["||host of parasite"]) == [SHREW_AGAIN, SHREW, MOUSE]
    assert guids(engine, taxon=["||parasite of|genus|Sorex"]) == [CESTODE]
    # Related taxa of one row OR together.
    assert guids(engine, taxon=["||host of parasite|class|Cestoda|genus|Ixodes"]) == [SHREW_AGAIN, SHREW, MOUSE]


def test_the_relationship_and_the_related_taxon_must_be_the_same_link(engine: DuckDbQueryEngine) -> None:
    # The shrew is `same individual as` a Sorex and hosts a cestode, but hosts no Sorex.
    assert guids(engine, taxon=["||host of parasite|genus|Sorex"]) == []
    assert guids(engine, taxon=["||same individual as|genus|Sorex"]) == [SHREW]


def test_a_link_leaving_the_database_matches_on_what_arctos_cached(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, taxon=["||host of parasite|family|Hymenolepididae"]) == [SHREW]
    results = run(engine, taxon=["||host of parasite|scientific_name|Hymenolepis"])
    # It names no record, so there is no guid to list, but Arctos cached what it is.
    assert [(r["guid"], r.get("related_guid")) for r in results.records] == [(SHREW, None)]
    assert results.records[0]["related_scientific_name"] == ["Hymenolepis diminuta"]
    # Only the family and the identification are cached, never the other ranks.
    assert guids(engine, taxon=["||host of parasite|genus|Hymenolepis"]) == []


def test_a_row_reaches_only_through_the_records_its_own_taxon_selects(engine: DuckDbQueryEngine) -> None:
    results = run(engine, taxon=["genus|Sorex|host of parasite", "genus|Peromyscus|same individual as"])
    # `same individual as` was asked of the mouse, so the shrew's own such link is not followed.
    assert [(r["guid"], r["related_guid"]) for r in results.records] == [(SHREW_AGAIN, [CESTODE]), (SHREW, [CESTODE])]


def test_a_null_relationship_takes_records_related_to_nothing(engine: DuckDbQueryEngine) -> None:
    # The tick has no link of its own, but the mouse's points at it.
    assert guids(engine, taxon=["||null"]) == [OTHER_SHREW]
    assert guids(engine, taxon=["genus|Sorex|null"]) == [OTHER_SHREW]
    assert guids(engine, taxon=["genus|Peromyscus|null"]) == []
    # It ORs with the row's other relationships, and only those reach anything.
    results = run(engine, taxon=["genus|Sorex|null; host of parasite"])
    assert [(r["guid"], r.get("related_guid")) for r in results.records] == [
        (OTHER_SHREW, None),
        (SHREW_AGAIN, [CESTODE]),
        (SHREW, [CESTODE]),
    ]
    # Nothing a record is related to can be a related taxon.
    with pytest.raises(BadQueryError):
        run(engine, taxon=["genus|Sorex|null|class|Cestoda"])
    # `null` is no relationship to view.
    assert run(engine, taxon=["genus|Sorex|null"]).column_order[:2] == ["guid", "scientific_name"]
    summary = engine.summary(ArctosQuery(taxon=["genus|Sorex|null; host of parasite"]))
    assert summary.description == "Genus Sorex with relationship host of parasite, or with no relationships."
    assert [block.relation for block in summary.related] == ["host of parasite"]


# ---- attributes and parts -------------------------------------------------


def test_attribute_rows_and_by_default_and_report_a_funnel(engine: DuckDbQueryEngine) -> None:
    results = run(engine, taxon=["class|Mammalia"], attr=["sex|male", "detected|endoparasite"], prefix=["MSB:Mamm"])
    assert [r["guid"] for r in results.records] == [SHREW]
    summary = results.summary
    assert (summary.taxon, summary.context, summary.matched) == (4, 2, 1)
    # Each row is counted against the context, not against the other row.
    assert [(a.attr, a.count) for a in summary.attrs] == [("sex|male", 2), ("detected|endoparasite", 1)]
    assert results.total.value == 1


def test_attribute_rows_or_and_negate(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, attr=["sex|female", "detected|virus: orthohantavirus"], attr_op="or") == [SHREW_AGAIN, MOUSE]
    assert guids(engine, attr=["sex", "!sex|male"]) == [SHREW_AGAIN]
    # Negating a negative type is its own search: records with no such row at all.
    assert guids(engine, taxon=["genus|Sorex"], attr=["!not detected"]) == [SHREW_AGAIN, SHREW]
    # A negated row stays inside the OR.
    assert guids(engine, taxon=["genus|Sorex"], attr=["!sex", "detected"], attr_op="or") == [OTHER_SHREW, SHREW]
    # Several values of one row OR; a type alone asks only whether it is recorded.
    assert guids(engine, attr=["detected|endoparasite|virus: Orthohantavirus"]) == [SHREW, MOUSE]
    assert guids(engine, attr=["weight"]) == [MOUSE]


def test_method_qualifies_the_attribute_row_it_was_found_by(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, attr=["detected"], method="immunoblot") == [MOUSE]
    # The shrew has a detection and the mouse an immunoblot, but not for an endoparasite.
    assert guids(engine, attr=["detected|endoparasite"], method="immunoblot") == []
    assert guids(engine, method="necropsy") == [SHREW]
    # The form drops the method when a row is negated, and so does the service.
    assert guids(engine, attr=["!detected|endoparasite", "sex|male"], method="immunoblot") == [MOUSE]


def test_part_rows_and_together(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, part=["part_name|Liver"]) == [SHREW, MOUSE]
    assert guids(engine, part=["part_name|liver", "!disposition|used up"]) == [SHREW]
    assert guids(engine, part=["part_barcode"]) == [SHREW]
    assert guids(engine, part=["part_barcode|A100"]) == [SHREW]


def test_null_rows_ask_whether_a_record_has_any_attribute_or_part(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, attr=["null"]) == [CESTODE, TICK]
    assert guids(engine, attr=["!null"]) == [OTHER_SHREW, SHREW_AGAIN, SHREW, MOUSE]
    assert guids(engine, part=["null"]) == [OTHER_SHREW, SHREW_AGAIN, CESTODE, TICK]
    assert guids(engine, part=["!null"]) == [SHREW, MOUSE]
    # They combine like any other row.
    assert guids(engine, attr=["!null"], part=["null"]) == [OTHER_SHREW, SHREW_AGAIN]
    assert guids(engine, attr=["null", "weight"], attr_op="or") == [MOUSE, CESTODE, TICK]
    # `!null` with a method: any attribute determined that way.
    assert guids(engine, attr=["!null"], method="necropsy") == [SHREW]
    summary = engine.summary(ArctosQuery(attr=["null"], part=["!null"]))
    assert summary.description == "With no attributes with any parts."
    with pytest.raises(BadQueryError):
        run(engine, attr=["null|x"])


# ---- scope ----------------------------------------------------------------


def test_collection_place_and_collector(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, prefix=["MSB:Para", "DMNS:Mamm"]) == [OTHER_SHREW, CESTODE, TICK]
    assert guids(engine, country=["canada"]) == [OTHER_SHREW]
    assert guids(engine, state=["alaska", "Colorado"]) == [SHREW_AGAIN, MOUSE, TICK]
    # The collector role only: Cook prepared the mouse and did not collect it.
    assert guids(engine, collector="Joseph A. Cook") == [SHREW_AGAIN, SHREW]


def test_locality_is_a_phrase_on_the_record_s_own_locality(engine: DuckDbQueryEngine) -> None:
    assert guids(engine, locality="sandia mountains") == [SHREW, CESTODE]
    assert guids(engine, locality="Mountains of Sandia") == [SHREW_AGAIN]


def test_both_ends_of_a_date_range_hold_for_one_event(engine: DuckDbQueryEngine) -> None:
    # A year-only date reads as its first day; the cestode shares the shrew's event date.
    assert guids(engine, **{"from": "1990-01-01", "to": "1990-12-31"}) == [SHREW_AGAIN, SHREW, CESTODE]
    assert guids(engine, **{"from": "1990-01-02", "to": "1990-12-31"}) == [SHREW, CESTODE]
    # The shrew has events in 1990 and 2001, and none between.
    assert guids(engine, **{"from": "1995-01-01", "to": "1999-12-31"}) == []
    assert guids(engine, **{"from": "2001-01-01"}) == [SHREW, MOUSE, TICK]
    # The mouse has no event at all, only the dates on the record.
    assert guids(engine, taxon=["genus|Peromyscus"], to="2005-07-04") == [MOUSE]
    # An undated record matches no range.
    assert OTHER_SHREW not in guids(engine, to="2030-01-01")


# ---- the page -------------------------------------------------------------


def test_a_record_carries_its_nested_fields_without_the_empty_ones(engine: DuckDbQueryEngine) -> None:
    results = run(engine, taxon=["genus|Sorex"], prefix=["MSB:Mamm"])
    assert results.column_order == [
        "guid",
        "scientific_name",
        "relations",
        "country",
        "state_prov",
        "spec_locality",
        "events",
        "attributedetail",
        "partdetail",
    ]
    (record,) = results.records
    assert list(record) == results.column_order
    assert record["spec_locality"] == "Sandia Mountains, Cibola National Forest"
    assert [(e["began_date"], e["spec_locality"]) for e in record["events"]] == [
        ("1990-06-01", "Sandia Mountains, Cibola National Forest"),
        ("2001-01-01", "Fairbanks"),
    ]
    assert {"attribute_type": "sex", "attribute_value": "male"} in record["attributedetail"]
    assert record["partdetail"] == [
        {
            "part_id": 501,
            "part_name": "liver",
            "condition": "good",
            "part_count": 1,
            "disposition": "in collection",
            "part_barcode": "A100",
            "part_attributes": [{"attribute_type": "preservation", "attribute_value": "frozen"}],
        }
    ]
    # The link that leaves the database has an identifier and no guid.
    outside = next(link for link in record["relations"] if link["related_identifier"] == "HWML 48061")
    assert "related_guid" not in outside


def test_cols_picks_the_columns_and_their_order(engine: DuckDbQueryEngine) -> None:
    results = run(engine, taxon=["genus|Peromyscus|host of parasite"], cols="guid,dec_lat,genus,related_guid")
    assert results.column_order == ["guid", "dec_lat", "genus", "related_guid"]
    assert results.records == [{"guid": MOUSE, "dec_lat": 64.8, "genus": "Peromyscus", "related_guid": [TICK]}]

    with pytest.raises(BadQueryError, match="nope"):
        run(engine, cols="guid,nope")


def test_a_page_past_the_end_is_empty_and_still_counted(engine: DuckDbQueryEngine) -> None:
    results = run(engine, taxon=["genus|Sorex"], page=2)
    assert results.records == []
    assert results.summary.matched == 3


def test_a_row_outside_the_grammar_is_refused(engine: DuckDbQueryEngine) -> None:
    for taxon in ("tribe|Sorex", "genus|Sorex|eats", "genus|Sorex|host of parasite|class", "genus|"):
        with pytest.raises(BadQueryError):
            run(engine, taxon=[taxon])
    with pytest.raises(BadQueryError):
        run(engine, part=["part_remark|x"])


# ---- summary --------------------------------------------------------------


def test_the_summary_describes_the_whole_matched_set(engine: DuckDbQueryEngine) -> None:
    summary = engine.summary(ArctosQuery(taxon=["class|Mammalia"], prefix=["MSB:Mamm", "MSB:Host"]))
    records = summary.records
    assert records.matched == 3
    # Two names, three localities across four events, two collectors.
    assert records.distinct.model_dump() == {"taxa": 2, "localities": 3, "collectors": 2}
    # 2005 comes from the mouse, which has no event.
    assert records.years is not None
    assert (records.years.min, records.years.max) == (1990, 2005)

    facets = records.facets
    assert [(f.value, f.count) for f in facets.taxa] == [("Sorex cinereus", 2), ("Peromyscus maniculatus", 1)]
    assert [(f.value, f.count) for f in facets.states] == [("Alaska", 1), ("Colorado", 1), ("New Mexico", 1)]
    assert [(f.value, f.count) for f in facets.collections] == [("MSB:Mamm", 2), ("MSB:Host", 1)]
    # The collector role only, a record counting once under each of its collectors:
    # Cook also prepared the mouse, and Doe co-collected the second shrew.
    assert [(f.value, f.count) for f in facets.collectors] == [("Jane Doe", 2), ("Joseph A. Cook", 2)]
    # Each name is what `collector` matches: picking it keeps exactly its records.
    for f in facets.collectors:
        picked = run(engine, taxon=["class|Mammalia"], prefix=["MSB:Mamm", "MSB:Host"], collector=f.value)
        assert picked.total.value == f.count
    assert {(f.type, f.value, f.count) for f in facets.detections} == {
        ("detected", "endoparasite", 1),
        ("detected", "virus: Orthohantavirus", 1),
        ("examined for", "endoparasite", 1),
    }
    # `weight` has no code table, so its values are not a facet.
    assert [(f.type, f.value, f.count) for f in facets.attributes] == [("sex", "male", 2), ("sex", "female", 1)]
    # Counted by record: the mouse's two parts in good condition would otherwise count twice.
    assert [(f.type, f.value, f.count) for f in facets.parts] == [
        ("disposition", "in collection", 2),
        ("part_name", "liver", 2),
        ("disposition", "used up", 1),
        ("part_name", "skull", 1),
    ]
    assert summary.related == []
    assert summary.description == "Class Mammalia in MSB:Mamm, MSB:Host."


def test_the_summary_has_one_block_per_relationship(engine: DuckDbQueryEngine) -> None:
    summary = engine.summary(ArctosQuery(taxon=["genus|Sorex|same individual as; host of parasite; ate"]))
    assert summary.records.matched == 2
    # In the order the query names them, with zeros for one that reaches nothing.
    assert [(r.relation, r.pairings, r.matched) for r in summary.related] == [
        ("same individual as", 1, 1),
        # Two shrew records point at one cestode lot: two links, one record.
        ("host of parasite", 2, 1),
        ("ate", 0, 0),
    ]
    cestodes = summary.related[1]
    assert [(f.value, f.count) for f in cestodes.facets.taxa] == [("Cestoda", 1)]
    assert cestodes.years is not None
    assert cestodes.distinct.localities == 1
    assert summary.related[2].years is None


def test_a_related_taxon_row_names_the_relationship_it_reached(engine: DuckDbQueryEngine) -> None:
    summary = engine.summary(ArctosQuery(taxon=["||ate|genus|Sorex"]))
    # The cestode is a parasite of a Sorex, not something that ate one.
    assert summary.records.matched == 0
    assert [(r.relation, r.matched) for r in summary.related] == [("ate", 0)]

    summary = engine.summary(ArctosQuery(taxon=["class|Cestoda||genus|Sorex"]))
    assert [(r.relation, r.pairings, r.matched) for r in summary.related] == [("parasite of", 2, 2)]


def test_an_empty_form_matches_everything_and_describes_nothing(engine: DuckDbQueryEngine) -> None:
    assert run(engine).summary.matched == 6
    summary = engine.summary(ArctosQuery())
    assert summary.records.matched == 6
    assert summary.description is None
