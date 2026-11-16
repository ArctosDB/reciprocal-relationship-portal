"""Taxon suggestions over the six records of `conftest.py`."""

from vulpes.engines.duckdb import DuckDbQueryEngine


def found(engine: DuckDbQueryEngine, q: str, rank: str | None = None, limit: int = 10) -> list[tuple[str, str, int]]:
    return [(t.rank, t.name, t.record_count) for t in engine.taxa(q, rank, limit)]


def test_a_prefix_matches_in_any_case_with_the_most_records_first(engine: DuckDbQueryEngine) -> None:
    assert found(engine, "sorex") == [
        ("genus", "Sorex", 3),
        ("scientific_name", "Sorex cinereus", 2),
        ("species", "Sorex cinereus", 2),
        ("scientific_name", "Sorex monticolus", 1),
        ("species", "Sorex monticolus", 1),
    ]
    # The start of the name, not any part of it.
    assert found(engine, "cinereus") == []
    assert found(engine, "sorex", limit=1) == [("genus", "Sorex", 3)]


def test_rank_keeps_one_rank_s_names(engine: DuckDbQueryEngine) -> None:
    assert found(engine, "So", rank="genus") == [("genus", "Sorex", 3)]
    assert found(engine, "Cestoda", rank="genus") == []
    assert found(engine, "Cestoda", rank="class") == [("class", "Cestoda", 1)]
    assert found(engine, "So", rank="nope") == []


def test_each_determination_of_a_joined_column_is_its_own_name(engine: DuckDbQueryEngine) -> None:
    assert found(engine, "ixodes", rank="species") == [
        ("species", "Ixodes pacificus", 1),
        ("species", "Ixodes scapularis", 1),
    ]
    # Both determinations name the same genus, on one record.
    assert found(engine, "ixodes", rank="genus") == [("genus", "Ixodes", 2)]


def test_a_taxon_names_its_parent_in_the_rank_chain(engine: DuckDbQueryEngine) -> None:
    (genus,) = engine.taxa("Peromyscus", "genus", 10)
    # There is no subfamily here, so the rank above holds no name and neither field is set.
    assert (genus.parent_rank, genus.parent_name) == (None, None)
    (order,) = engine.taxa("Rodentia", "order", 10)
    assert (order.parent_rank, order.parent_name) == ("class", "Mammalia")
    # `scientific_name` is not part of the chain, and the highest rank has nothing above it.
    assert all(t.parent_name is None for t in engine.taxa("Sorex cinereus", "scientific_name", 10))
    assert all(t.parent_name is None for t in engine.taxa("Chordata", "phylum", 10))


def test_a_query_under_two_characters_matches_nothing(engine: DuckDbQueryEngine) -> None:
    assert found(engine, "S") == []
    assert found(engine, "  s ") == []
