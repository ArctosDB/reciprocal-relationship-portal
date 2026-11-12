# pyright: ignore
# pylint: disable=all

import duckdb

from build_parquet import run


def test_relations(prefix, cursor):
    # hosts in `prefix`, each with the list of its parasites' guids
    query_hosts = """
        COPY (
            WITH edges AS (
                SELECT src_id, string_agg(dst_guid, '; ' ORDER BY dst_guid) AS parasite_guids
                FROM related_records
                WHERE relationship = 'host of parasite'
                    AND src_prefix = $prefix
                    AND dst_id IS NOT NULL
                GROUP BY src_id
            )
            SELECT e.parasite_guids, h.*
            FROM edges e
            JOIN records h ON h.collection_object_id = e.src_id
            -- records is sorted by guid_prefix: this lets the scan skip other collections
            WHERE h.guid_prefix = $prefix
            LIMIT 100
        )
        TO 'out-hosts.csv' (FORMAT csv, HEADER)
    """

    # the parasite records themselves, from any collection
    query_paras = """
        COPY (
            SELECT r.src_guid AS host_guid, p.*
            FROM related_records r
            -- joining on the prefix too lets the scan skip collections with no matching edges
            JOIN records p ON p.guid_prefix = r.dst_prefix AND p.collection_object_id = r.dst_id
            WHERE r.relationship = 'host of parasite'
                AND r.src_prefix = $prefix
            LIMIT 100
        )
        TO 'out-paras.csv' (FORMAT csv, HEADER)
    """

    run("query_hosts", cursor, query_hosts, {"prefix": prefix})
    run("query_paras", cursor, query_paras, {"prefix": prefix})


def test_attribute(prefix, attribute_type, attribute_value, cursor):
    query = """
        COPY (
            SELECT r.*
            FROM records r
            SEMI JOIN attributes a ON a.id = r.collection_object_id
                AND a.attribute_type = $attribute_type
                AND a.attribute_value = $attribute_value
            WHERE r.guid_prefix = $prefix
        )
        TO 'out-attributes.csv' (FORMAT csv, HEADER)
    """

    run("query_attribute", cursor, query, {
        "prefix": prefix, "attribute_type": attribute_type, "attribute_value": attribute_value})


def test_part_attribute(prefix, attribute_type, attribute_value, cursor):
    query = """
        COPY (
            SELECT p.part_name, p.part_id, pa.attribute_type, pa.attribute_value, r.*
            FROM part_attributes pa
            JOIN parts p ON p.part_id = pa.part_id
            JOIN records r ON r.collection_object_id = pa.id
            WHERE pa.attribute_type = $attribute_type
                AND pa.attribute_value = $attribute_value
                AND r.guid_prefix = $prefix
        )
        TO 'out-part-attributes.csv' (FORMAT csv, HEADER)
    """

    run("query_part_attribute", cursor, query, {
        "prefix": prefix, "attribute_type": attribute_type, "attribute_value": attribute_value})


def test_complex_query(prefix, cursor):
    # hosts in `prefix` that have an endoparasite detected, with their parasites
    query = """
        COPY (
            WITH edges AS (
                SELECT src_id, string_agg(dst_guid, '; ' ORDER BY dst_guid) AS parasite_guids
                FROM related_records
                WHERE relationship = 'host of parasite'
                    AND src_prefix = $prefix
                    AND dst_id IS NOT NULL
                GROUP BY src_id
            )
            SELECT e.parasite_guids, h.*
            FROM edges e
            JOIN records h ON h.collection_object_id = e.src_id
            SEMI JOIN attributes a ON a.id = h.collection_object_id
                AND a.attribute_type = 'detected'
                AND a.attribute_value = 'endoparasite'
            WHERE h.guid_prefix = $prefix
        )
        TO 'out-complex.csv' (FORMAT csv, HEADER)
    """

    run("query_complex", cursor, query, {"prefix": prefix})


if __name__ == "__main__":
    d = duckdb.connect("arctos.duckdb", config={
        "memory_limit": "12GB",
        "preserve_insertion_order": False,
    })

    with d.cursor() as cursor:
        test_relations("MSB:Mamm", d.cursor())
        test_attribute("MSB:Mamm", "examined for", "virus: Orthohantavirus", d.cursor())
        test_part_attribute("MSB:Mamm", "part modifier", "postcranial", d.cursor())
        test_complex_query("MSB:Mamm", d.cursor())
