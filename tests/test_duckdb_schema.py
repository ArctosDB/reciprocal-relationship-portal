"""The schema of the six-record database of `conftest.py`."""

from vulpes.engines.duckdb import DuckDbQueryEngine
from vulpes.models.query import ArctosQuery


def test_code_table_lists_are_complete_and_ordered(engine: DuckDbQueryEngine) -> None:
    schema = engine.schema()
    # A non-public type never ships, and neither does its code table.
    assert [(t.id, t.vocabulary, t.units_table) for t in schema.attribute_types] == [
        ("detected", "ctexamined_detected", None),
        ("sex", "ctsex_cde", None),
        ("weight", None, "ctweight_units"),
    ]
    assert schema.nonpublic_types_dropped == ["restricted data"]
    assert [(f.id, f.label, f.vocabulary) for f in schema.part_fields] == [
        ("part_name", "part name", "ctspecimen_part_name"),
        ("disposition", "disposition", "ctdisposition"),
        ("condition", "condition", None),
        ("part_barcode", "part barcode", None),
    ]
    # Every vocabulary a type or part field names is a key, an empty table included.
    assert sorted(schema.vocabularies) == ["ctdisposition", "ctexamined_detected", "ctsex_cde", "ctspecimen_part_name"]
    assert schema.vocabularies["ctdisposition"] == []
    # A parent before its children; a url that arrives as an array is its first element.
    detected = schema.vocabularies["ctexamined_detected"]
    assert [v.value for v in detected] == ["endoparasite", "virus", "virus: Orthohantavirus"]
    assert detected[2].documentation_url == "https://example.org/h"
    # A relationship with no record still ships.
    assert [r.value for r in schema.relations] == ["ate", "host of parasite", "parasite of", "same individual as"]


def test_observed_lists_carry_counts(engine: DuckDbQueryEngine) -> None:
    schema = engine.schema()
    assert [(f.value, f.count) for f in schema.countries] == [("United States", 5), ("Canada", 1)]
    assert [(f.value, f.count) for f in schema.states][:2] == [("Alaska", 2), ("New Mexico", 2)]
    # The collector role only, by record: Cook prepared the mouse and did not collect it.
    assert [(f.value, f.count) for f in schema.collectors] == [("Jane Doe", 2), ("Joseph A. Cook", 2)]
    assert [(p.value, p.count, p.institution, p.collection_cde) for p in schema.guid_prefixes] == [
        ("MSB:Mamm", 2, "MSB", "Mammalogy"),
        ("MSB:Para", 2, "MSB", "Parasitology"),
        ("DMNS:Mamm", 1, "DMNS", "Mammalogy"),
        # A code the table has no label for.
        ("MSB:Host", 1, "MSB", ""),
    ]
    # No record here has a subfamily, so the rank is not offered.
    assert [r.id for r in schema.ranks] == ["scientific_name", "phylum", "class", "order", "family", "genus", "species"]
    assert schema.limits.page_size == 100
    # The day of the flat export, as the importer recorded it.
    assert schema.snapshot_date.isoformat() == "2026-07-17"


def test_the_schema_offers_what_a_search_accepts(engine: DuckDbQueryEngine) -> None:
    schema = engine.schema()
    # Every column the schema lists can be asked for, and every relationship searched.
    results = engine.search(ArctosQuery(taxon=["genus|Sorex"], cols=",".join(schema.columns)))
    assert results.column_order == schema.columns
    for relation in schema.relations:
        engine.search(ArctosQuery(taxon=[f"||{relation.value}"]))
