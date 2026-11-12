import pytest

pytestmark = pytest.mark.duckdb


def fetch(cursor, query, params=None):
    """Run `query` and return rows as a list of dicts keyed by column name."""
    cursor.execute(query, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]


def has_attribute(cursor, ids, attribute_type, attribute_value):
    """Return the subset of `ids` that have the given attribute in the attributes table."""
    rows = cursor.execute(
        """
        SELECT DISTINCT id FROM attributes
        WHERE id IN (SELECT unnest($ids))
            AND attribute_type = $attribute_type
            AND attribute_value = $attribute_value
        """,
        {"ids": list(ids), "attribute_type": attribute_type, "attribute_value": attribute_value},
    ).fetchall()
    return {r[0] for r in rows}


HOSTS_QUERY = """
    WITH edges AS (
        SELECT src_id, string_agg(dst_guid, '; ' ORDER BY dst_guid) AS parasite_guids
        FROM related_records
        WHERE relationship = 'host of parasite'
            AND src_prefix = 'MSB:Mamm'
            AND dst_id IS NOT NULL
        GROUP BY src_id
    )
    SELECT e.parasite_guids, h.*
    FROM edges e
    JOIN records h ON h.collection_object_id = e.src_id
    -- records is sorted by guid_prefix: this lets the scan skip other collections
    WHERE h.guid_prefix = 'MSB:Mamm'
"""


def test_hosts(cursor):
    rows = fetch(cursor, HOSTS_QUERY + " LIMIT 100")

    assert rows
    assert len({r["collection_object_id"] for r in rows}) == len(rows), "one row per host"
    for r in rows:
        assert r["guid_prefix"] == "MSB:Mamm"
        assert r["guid"].startswith("MSB:Mamm:")
        assert r["parasite_guids"]

    # the aggregated guid list should match the raw edges for each host
    edges = cursor.execute(
        """
        SELECT src_id, list_sort(list(dst_guid))
        FROM related_records
        WHERE relationship = 'host of parasite'
            AND dst_id IS NOT NULL
            AND src_id IN (SELECT unnest($ids))
        GROUP BY src_id
        """,
        {"ids": [r["collection_object_id"] for r in rows]},
    ).fetchall()
    expected = {src_id: guids for src_id, guids in edges}
    for r in rows:
        assert r["parasite_guids"].split("; ") == expected[r["collection_object_id"]]


def test_parasites(cursor):
    # the parasite records themselves, from any collection
    rows = fetch(
        cursor,
        """
        SELECT r.src_guid AS host_guid, r.dst_guid, r.dst_prefix, p.*
        FROM related_records r
        -- joining on the prefix too lets the scan skip collections with no matching edges
        JOIN records p ON p.guid_prefix = r.dst_prefix AND p.collection_object_id = r.dst_id
        WHERE r.relationship = 'host of parasite'
            AND r.src_prefix = 'MSB:Mamm'
        LIMIT 100
        """,
    )

    assert rows
    for r in rows:
        assert r["host_guid"].startswith("MSB:Mamm:")
        # the joined record is the one the edge points at
        assert r["guid"] == r["dst_guid"]
        assert r["guid_prefix"] == r["dst_prefix"]


def test_attribute(cursor):
    rows = fetch(
        cursor,
        """
        SELECT r.*
        FROM records r
        SEMI JOIN attributes a ON a.id = r.collection_object_id
            AND a.attribute_type = 'examined for'
            AND a.attribute_value = 'virus: Orthohantavirus'
        WHERE r.guid_prefix = 'MSB:Mamm'
        """,
    )

    assert rows
    ids = [r["collection_object_id"] for r in rows]
    assert len(set(ids)) == len(ids), "SEMI JOIN should not duplicate records"
    assert all(r["guid_prefix"] == "MSB:Mamm" for r in rows)
    assert has_attribute(cursor, ids, "examined for", "virus: Orthohantavirus") == set(ids)


def test_part_attribute(cursor):
    rows = fetch(
        cursor,
        """
        SELECT p.id AS part_owner_id, p.part_name, p.part_id, pa.attribute_type, pa.attribute_value, r.*
        FROM part_attributes pa
        JOIN parts p ON p.part_id = pa.part_id
        JOIN records r ON r.collection_object_id = pa.id
        WHERE pa.attribute_type = 'part modifier'
            AND pa.attribute_value = 'postcranial'
            AND r.guid_prefix = 'MSB:Mamm'
        """,
    )

    assert rows
    for r in rows:
        assert r["attribute_type"] == "part modifier"
        assert r["attribute_value"] == "postcranial"
        assert r["guid_prefix"] == "MSB:Mamm"
        assert r["part_name"]
        # the part belongs to the record it was joined to
        assert r["part_owner_id"] == r["collection_object_id"]


def test_complex_query(cursor):
    # hosts in MSB:Mamm that have an endoparasite detected and have a related parasite record
    rows = fetch(
        cursor,
        """
        WITH edges AS (
            SELECT src_id, string_agg(dst_guid, '; ' ORDER BY dst_guid) AS parasite_guids
            FROM related_records
            WHERE relationship = 'host of parasite'
                AND src_prefix = 'MSB:Mamm'
                AND dst_id IS NOT NULL
            GROUP BY src_id
        )
        SELECT e.parasite_guids, h.*
        FROM edges e
        JOIN records h ON h.collection_object_id = e.src_id
        SEMI JOIN attributes a ON a.id = h.collection_object_id
            AND a.attribute_type = 'detected'
            AND a.attribute_value = 'endoparasite'
        WHERE h.guid_prefix = 'MSB:Mamm'
        """,
    )

    assert rows
    ids = {r["collection_object_id"] for r in rows}
    assert len(ids) == len(rows)
    assert all(r["guid_prefix"] == "MSB:Mamm" and r["parasite_guids"] for r in rows)
    assert has_attribute(cursor, ids, "detected", "endoparasite") == ids

    # result should be exactly the hosts query filtered to records with the attribute
    all_hosts = {r["collection_object_id"] for r in fetch(cursor, HOSTS_QUERY)}
    assert ids <= all_hosts
    assert has_attribute(cursor, all_hosts, "detected", "endoparasite") == ids
