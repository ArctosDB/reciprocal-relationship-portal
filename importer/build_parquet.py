# pyright: ignore
# pylint: disable=all

import os
import sys
import duckdb
from datetime import datetime


PARQUET = "data/arctos.parquet"
SRC = f"read_parquet('{PARQUET}')"

JSON_COLS = [
    "partdetail",
    "previousidentifications",
    "json_locality",
    "attributedetail",
    "media",
    "identifiers",
    "related_record_cache",
    "rights",
    "citations",
    "media_tags",
    "collector_agents",
    "transactions",
]

DROPPED_COLS = [
    "cataloged_item_type",
    "encumbrances",
    "infraspecific_rank",
    "relatedinformation",
    "lastpartlocation",
    "enteredby",
    "entereddate",
    "lastdate",
    "lastuser",
    "event_count",
    "locality_count",
    "datum",
    "imageurl",
    "parts",
    "othercatalognumbers",
    "relatedcatalogeditems",
    "typestatus",
    "collectors",
    "preparators",
    "use_license_url",
    "formatted_scientific_name",
    "full_taxon_name",
    "higher_geog",
]

COLLECTION_COLS = ["institution_acronym", "collection_cde", "collection_id"]

LOCALITY_FLAT_COLS = [
    "minimum_elevation",
    "maximum_elevation",
    "orig_elev_units",
    "min_elev_in_m",
    "max_elev_in_m",
    "verbatimelevation",
    "min_depth_in_m",
    "max_depth_in_m",
    "coordinateuncertaintyinmeters",
    "georeference_source",
]
LOCALITY_JSON_COLS = [
    "spec_locality",
    "geog_auth_rec_id",
    "dec_lat",
    "dec_long",
    "locality_name",
    "locality_remarks",
    "georeference_protocol",
]

# Every table links back to records via `id` (= records.collection_object_id).
# Child tables are sorted by their main search columns so DuckDB's min/max
# zone maps skip most row groups on equality filters.
TABLES = {
    "records": f"""
        SELECT * EXCLUDE ({", ".join(JSON_COLS + DROPPED_COLS + COLLECTION_COLS + LOCALITY_FLAT_COLS + LOCALITY_JSON_COLS)})
        FROM src
        ORDER BY guid_prefix, guid
    """,

    "collections": r"""
        SELECT
            guid_prefix,
            any_value(institution_acronym) AS institution_acronym,
            any_value(collection_cde) AS collection_cde,
            any_value(collection_id) AS collection_id,
            from_json(any_value(rights), '{"terms":{"uri":"VARCHAR","display":"VARCHAR"},"license":{"uri":"VARCHAR","display":"VARCHAR"},"loan_policy":"VARCHAR"}') AS r
        FROM src
        GROUP BY guid_prefix
    """,

    "localities": r"""
        WITH ev AS (
            SELECT unnest(from_json(json_locality, '[{"locality_id":"BIGINT","spec_locality":"VARCHAR","higher_geog":"VARCHAR","geog_auth_rec_id":"BIGINT","locality_name":"VARCHAR","locality_remarks":"VARCHAR","coordinates":"VARCHAR","datum":"VARCHAR","coordinate_error":"VARCHAR","elevation":"VARCHAR","depth":"VARCHAR","georeference_protocol":"VARCHAR","source_authority":"VARCHAR","primary_spatial_data":"VARCHAR","locality_attributes":"JSON"}]')) AS e
            FROM src
            WHERE json_locality IS NOT NULL
        ),
        loc AS (
            -- a handful of locality_ids carry conflicting payloads across records; take one
            SELECT e.locality_id, any_value(e) AS e
            FROM ev
            WHERE e.locality_id IS NOT NULL
            GROUP BY 1
        ),
        flat AS (
            -- only known for each record's primary locality
            SELECT
                locality_id,
                any_value(minimum_elevation) AS minimum_elevation,
                any_value(maximum_elevation) AS maximum_elevation,
                any_value(orig_elev_units) AS orig_elev_units,
                any_value(min_elev_in_m) AS min_elev_in_m,
                any_value(max_elev_in_m) AS max_elev_in_m,
                any_value(verbatimelevation) AS verbatimelevation,
                any_value(min_depth_in_m) AS min_depth_in_m,
                any_value(max_depth_in_m) AS max_depth_in_m,
                any_value(coordinateuncertaintyinmeters) AS coordinateuncertaintyinmeters,
                any_value(georeference_source) AS georeference_source
            FROM src
            WHERE locality_id IS NOT NULL
            GROUP BY 1
        )
        SELECT
            loc.locality_id,
            e.spec_locality,
            e.higher_geog,
            e.geog_auth_rec_id,
            e.locality_name,
            e.locality_remarks,
            TRY_CAST(split_part(e.coordinates, ',', 1) AS DOUBLE) AS dec_lat,
            TRY_CAST(split_part(e.coordinates, ',', 2) AS DOUBLE) AS dec_long,
            e.datum,
            e.coordinate_error,
            e.elevation,
            e.depth,
            e.georeference_protocol,
            e.source_authority,
            e.primary_spatial_data,
            flat.* EXCLUDE (locality_id),
            e.locality_attributes
        FROM loc
        LEFT JOIN flat USING (locality_id)
        ORDER BY loc.locality_id
    """,

    "specimen_events": r"""
        SELECT
            id,
            e.specimen_event_id,
            e.collecting_event_id,
            e.locality_id,
            e.specimen_event_type,
            e.order_by_vs,
            e.began_date,
            e.ended_date,
            e.verbatim_date,
            e.verbatim_locality,
            e.collecting_method,
            e.collecting_source,
            e.collecting_event_name,
            e.coll_event_remarks,
            e.specimen_event_remark,
            e.habitat,
            e.verificationstatus,
            url_id(e.assigned_agent.agentID) AS assigned_agent_id,
            e.assigned_agent.agent_name AS assigned_agent_name,
            e.assigned_date,
            url_id(e.verified_agent.agentID) AS verified_agent_id,
            e.verified_agent.agent_name AS verified_agent_name,
            e.verified_date,
            e.event_attributes
        FROM (
            SELECT collection_object_id AS id, unnest(from_json(json_locality, '[{"specimen_event_id":"BIGINT","collecting_event_id":"BIGINT","locality_id":"BIGINT","specimen_event_type":"VARCHAR","order_by_vs":"BIGINT","began_date":"VARCHAR","ended_date":"VARCHAR","verbatim_date":"VARCHAR","verbatim_locality":"VARCHAR","collecting_method":"VARCHAR","collecting_source":"VARCHAR","collecting_event_name":"VARCHAR","coll_event_remarks":"VARCHAR","specimen_event_remark":"VARCHAR","habitat":"VARCHAR","verificationstatus":"VARCHAR","assigned_agent":{"agentID":"VARCHAR","agent_name":"VARCHAR"},"assigned_date":"VARCHAR","verified_agent":{"agentID":"VARCHAR","agent_name":"VARCHAR"},"verified_date":"VARCHAR","event_attributes":"JSON"}]')) AS e
            FROM src
            WHERE json_locality IS NOT NULL
        )
        ORDER BY id
    """,

    "identifications": r"""
        SELECT
            id,
            url_id(i.identificationID) AS identification_id,
            url_id(i.identificationID) = current_identification_id AS is_current,
            i.identification_order,
            i.scientific_name,
            i.taxa_formula,
            i.made_date,
            list_transform(i.identification_agents, a -> {
                'agent_id': url_id(coalesce(a.agent.agentID, a.agentID)),
                'agent_name': coalesce(a.agent.agent_name, a.agent_name),
                'identifier_order': a.identifier_order
            }) AS agents,
            i.short_citation,
            url_id(coalesce(i.sensu_publicationID, i.sensu_publication.publicationID)) AS sensu_publication_id,
            i.identification_remarks,
            i.identification_attributes
        FROM (
            SELECT collection_object_id AS id, identification_id AS current_identification_id,
                unnest(from_json(previousidentifications, '[{"identificationID":"VARCHAR","identification_order":"BIGINT","scientific_name":"VARCHAR","taxa_formula":"VARCHAR","made_date":"VARCHAR","identification_agents":[{"agent":{"agentID":"VARCHAR","agent_name":"VARCHAR"},"agentID":"VARCHAR","agent_name":"VARCHAR","identifier_order":"BIGINT"}],"short_citation":"VARCHAR","sensu_publicationID":"VARCHAR","sensu_publication":{"publicationID":"VARCHAR"},"identification_remarks":"VARCHAR","identification_attributes":"JSON"}]')) AS i
            FROM src
            WHERE previousidentifications IS NOT NULL
        )
        ORDER BY i.scientific_name
    """,

    "identification_taxa": r"""
        SELECT
            url_id(i.identificationID) AS identification_id,
            coalesce(t.taxon.classification_id, t.taxon_id) AS taxon_key,
            t.variable
        FROM (
            SELECT i, unnest(i.identification_taxa) AS t
            FROM (
                SELECT unnest(from_json(previousidentifications, '[{"identificationID":"VARCHAR","identification_taxa":[{"taxon_id":"VARCHAR","variable":"VARCHAR","taxon":{"classification_id":"VARCHAR"}}]}]')) AS i
                FROM src
                WHERE previousidentifications IS NOT NULL
            )
        )
        ORDER BY taxon_key
    """,

    "taxa": r"""
        SELECT
            coalesce(t.taxon.classification_id, t.taxon_id) AS taxon_key,
            any_value(t.taxon_id) AS taxon_id,
            any_value(t.taxon.name) AS name,
            any_value(t.taxon.source) AS source,
            any_value(t.taxon.ftn) AS full_taxon_name,
            any_value(t.taxon.ctrms) AS ctrms,
            any_value(t.taxon.nctrms) AS nctrms
        FROM (
            SELECT unnest(i.identification_taxa) AS t
            FROM (
                SELECT unnest(from_json(previousidentifications, '[{"identification_taxa":[{"taxon_id":"VARCHAR","taxon":{"classification_id":"VARCHAR","name":"VARCHAR","source":"VARCHAR","ftn":"VARCHAR","ctrms":[{"psn":"BIGINT","typ":"VARCHAR","term":"VARCHAR"}],"nctrms":[{"typ":"VARCHAR","term":"VARCHAR"}]}}]}]')) AS i
                FROM src
                WHERE previousidentifications IS NOT NULL
            )
        )
        GROUP BY 1
        ORDER BY name
    """,

    # Staging table so partdetail is parsed once for both parts and part_attributes.
    "_parts_raw": r"""
        SELECT collection_object_id AS id, unnest(from_json(partdetail, '[{"partID":"VARCHAR","parentPartID":"VARCHAR","part_name":"VARCHAR","condition":"VARCHAR","part_count":"BIGINT","disposition":"VARCHAR","part_remark":"VARCHAR","part_barcode":"VARCHAR","container_path":"VARCHAR","part_attributes":[{"attribute_type":"VARCHAR","attribute_value":"VARCHAR","attribute_units":"VARCHAR","attribute_date":"VARCHAR","attribute_method":"VARCHAR","attribute_remark":"VARCHAR","attribute_determiner":"JSON","attribute_determiner_id":"VARCHAR"}]}]')) AS p
        FROM src
        WHERE partdetail IS NOT NULL
    """,

    "parts": r"""
        SELECT
            id,
            url_id(p.partID) AS part_id,
            url_id(p.parentPartID) AS parent_part_id,
            p.part_name,
            p.condition,
            p.part_count,
            p.disposition,
            p.part_remark,
            p.part_barcode,
            p.container_path
        FROM _parts_raw
        ORDER BY p.part_name
    """,

    "part_attributes": r"""
        SELECT
            id,
            part_id,
            a.attribute_type,
            a.attribute_value,
            a.attribute_units,
            a.attribute_date,
            a.attribute_method,
            a.attribute_remark,
            -- determiner arrives either as a bare name string or as an agent object
            coalesce(url_id(a.attribute_determiner_id), url_id(a.attribute_determiner->>'agentID')) AS determiner_agent_id,
            CASE json_type(a.attribute_determiner)
                WHEN 'OBJECT' THEN a.attribute_determiner->>'agent_name'
                WHEN 'VARCHAR' THEN a.attribute_determiner->>'$'
            END AS determiner_name
        FROM (
            SELECT id, url_id(p.partID) AS part_id, unnest(p.part_attributes) AS a
            FROM _parts_raw
        )
        ORDER BY a.attribute_type, a.attribute_value
    """,

    "attributes": r"""
        SELECT
            id,
            a.attribute_type,
            a.attribute_value,
            a.attribute_units,
            a.attribute_date,
            a.attribute_method,
            a.attribute_remark,
            url_id(a.attribute_determiner.agentID) AS determiner_agent_id,
            a.attribute_determiner.agent_name AS determiner_name
        FROM (
            SELECT collection_object_id AS id, unnest(from_json(attributedetail, '[{"attribute_type":"VARCHAR","attribute_value":"VARCHAR","attribute_units":"VARCHAR","attribute_date":"VARCHAR","attribute_method":"VARCHAR","attribute_remark":"VARCHAR","attribute_determiner":{"agentID":"VARCHAR","agent_name":"VARCHAR"}}]')) AS a
            FROM src
            WHERE attributedetail IS NOT NULL
        )
        ORDER BY a.attribute_type, a.attribute_value
    """,

    "identifiers": r"""
        SELECT
            id,
            x.identifier_type,
            x.identifier,
            url_id(x.issued_by.agentID) AS issued_by_agent_id,
            x.issued_by.agent_name AS issued_by_name,
            url_id(x.assigned_by.agentID) AS assigned_by_agent_id,
            x.assigned_by.agent_name AS assigned_by_name,
            x.assigned_date,
            x.remarks
        FROM (
            SELECT collection_object_id AS id, unnest(from_json(identifiers, '[{"identifier_type":"VARCHAR","identifier":"VARCHAR","issued_by":{"agentID":"VARCHAR","agent_name":"VARCHAR"},"assigned_by":{"agentID":"VARCHAR","agent_name":"VARCHAR"},"assigned_date":"VARCHAR","remarks":"VARCHAR"}]')) AS x
            FROM src
            WHERE identifiers IS NOT NULL
        )
        ORDER BY x.identifier_type, x.identifier
    """,

    # media and media_tags share one table; tag rows have tag_id set
    "media": r"""
        SELECT * FROM (
            SELECT
                id,
                url_id(m.MediaID) AS media_id,
                NULL::BIGINT AS tag_id,
                m.media_type,
                m.mime_type,
                m.media_uri,
                m.thumbnail,
                NULL::VARCHAR AS preview_uri,
                m.license_url,
                m.license_text,
                m.terms_url,
                m.terms_text
            FROM (
                SELECT collection_object_id AS id, unnest(from_json(media, '[{"MediaID":"VARCHAR","media_type":"VARCHAR","mime_type":"VARCHAR","media_uri":"VARCHAR","thumbnail":"VARCHAR","license_url":"VARCHAR","license_text":"VARCHAR","terms_url":"VARCHAR","terms_text":"VARCHAR"}]')) AS m
                FROM src
                WHERE media IS NOT NULL
            )
            UNION ALL BY NAME
            SELECT
                id,
                m.media_id,
                m.tag_id,
                m.media_type,
                m.media_uri,
                m.thumbnail,
                m.preview_uri
            FROM (
                SELECT collection_object_id AS id, unnest(from_json(media_tags, '[{"media_id":"BIGINT","tag_id":"BIGINT","media_type":"VARCHAR","media_uri":"VARCHAR","thumbnail":"VARCHAR","preview_uri":"VARCHAR"}]')) AS m
                FROM src
                WHERE media_tags IS NOT NULL
            )
        )
        ORDER BY media_type, id
    """,

    "citations": r"""
        SELECT
            id,
            url_id(c.identificationID) AS identification_id,
            c.type_status,
            c.cited_name,
            url_id(coalesce(c.publicationID, c.publication.publicationID)) AS publication_id,
            coalesce(c.short_citation, c.publication.short_citation) AS short_citation,
            c.publication_doi,
            c.occurs_page_number,
            c.citation_remarks,
            c.publication_media
        FROM (
            SELECT collection_object_id AS id, unnest(from_json(citations, '[{"identificationID":"VARCHAR","type_status":"VARCHAR","cited_name":"VARCHAR","publicationID":"VARCHAR","publication":{"publicationID":"VARCHAR","short_citation":"VARCHAR"},"short_citation":"VARCHAR","publication_doi":"VARCHAR","occurs_page_number":"BIGINT","citation_remarks":"VARCHAR","publication_media":"JSON"}]')) AS c
            FROM src
            WHERE citations IS NOT NULL
        )
        ORDER BY c.type_status, c.cited_name
    """,

    # dst_id is set when the target is a record in this database;
    # external identifiers keep only dst_value.
    "related_records": r"""
        WITH e AS (
            SELECT
                id AS src_id,
                guid AS src_guid,
                guid_prefix AS src_prefix,
                r.relationship,
                r.identifier_type,
                r.record AS dst_value,
                nullif(regexp_extract(r.record, '/guid/(.+)$', 1), '') AS dst_guid,
                r.remarks,
                url_id(r.issued_by.agentID) AS issued_by_agent_id,
                r.issued_by.agent_name AS issued_by_name,
                r.identification AS dst_identification,
                r.family AS dst_family,
                r.geography AS dst_geography,
                r.locality AS dst_locality
            FROM (
                SELECT collection_object_id AS id, guid, guid_prefix, unnest(from_json(related_record_cache, '[{"relationship":"VARCHAR","identifier_type":"VARCHAR","record":"VARCHAR","remarks":"VARCHAR","issued_by":{"agentID":"VARCHAR","agent_name":"VARCHAR"},"identification":"VARCHAR","family":"VARCHAR","geography":"VARCHAR","locality":"VARCHAR"}]')) AS r
                FROM src
                WHERE related_record_cache IS NOT NULL
            )
        )
        SELECT
            e.* EXCLUDE (dst_guid),
            e.dst_guid,
            nullif(regexp_extract(e.dst_guid, '^[^:]+:[^:]+'), '') AS dst_prefix,
            d.collection_object_id AS dst_id
        FROM e
        LEFT JOIN records d ON d.guid = e.dst_guid
        ORDER BY relationship, src_prefix, src_id
    """,

    "record_agents": r"""
        SELECT
            id,
            url_id(a.agentid) AS agent_id,
            a.agent_role,
            a.agent_order,
            a.agent_remark
        FROM (
            SELECT collection_object_id AS id, unnest(from_json(collector_agents, '[{"agentid":"VARCHAR","agent_role":"VARCHAR","agent_order":"BIGINT","agent_remark":"VARCHAR","agent_name":"VARCHAR"}]')) AS a
            FROM src
            WHERE collector_agents IS NOT NULL
        )
        ORDER BY agent_id
    """,

    # collectors/preparators plus identification determiners
    "agents": r"""
        SELECT agent_id, mode(agent_name) AS agent_name
        FROM (
            SELECT url_id(a.agentid) AS agent_id, a.agent_name
            FROM (
                SELECT unnest(from_json(collector_agents, '[{"agentid":"VARCHAR","agent_name":"VARCHAR"}]')) AS a
                FROM src
                WHERE collector_agents IS NOT NULL
            )
            UNION ALL
            SELECT a.agent_id, a.agent_name
            FROM (SELECT unnest(agents) AS a FROM identifications)
        )
        WHERE agent_id IS NOT NULL
        GROUP BY agent_id
        ORDER BY agent_name
    """,

    "loans": r"""
        SELECT
            id,
            l.transaction_id,
            l.loan_number,
            l.guid_prefix,
            l.loan_type,
            l.loan_status,
            l.trans_date,
            l.closed_date,
            l.is_public_fg,
            l.projects
        FROM (
            SELECT collection_object_id AS id, unnest(from_json(transactions, '{"loans":[{"transaction_id":"BIGINT","loan_number":"VARCHAR","guid_prefix":"VARCHAR","loan_type":"VARCHAR","loan_status":"VARCHAR","trans_date":"VARCHAR","closed_date":"VARCHAR","is_public_fg":"BIGINT","projects":[{"project_id":"BIGINT","project_name":"VARCHAR"}]}]}').loans) AS l
            FROM src
            WHERE transactions IS NOT NULL
        )
        ORDER BY l.transaction_id
    """,
}


def convert_csv(csv):
    d = duckdb.connect(config={
        "threads": 2,
        "preserve_insertion_order": True,
        "memory_limit": "16GB",
    })

    with d.cursor() as cursor:
        cursor.execute(f"""
            COPY (
                SELECT * FROM read_csv(
                    '{csv}',
                    types={{'af': 'VARCHAR'}},
                    ignore_errors=true,
                    store_rejects=true)
                ORDER BY guid_prefix, guid
            )
            TO '{PARQUET}' (
                FORMAT parquet, COMPRESSION snappy
            )
        """)

        # ignore_errors drops bad rows silently; the reject table dies with this connection
        rejected = cursor.execute("SELECT count(*) FROM reject_errors").fetchall()[0][0]
        print(f"rejected rows: {rejected}")


def build_database(cursor):
    cursor.execute(f"CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM {SRC}")
    # trailing number of an Arctos URL: .../agent/123, .../IID123, .../PID123, .../media/123
    cursor.execute(r"CREATE OR REPLACE TEMP MACRO url_id(u) AS TRY_CAST(regexp_extract(u, '(\d+)$', 1) AS BIGINT)")

    for name, query in TABLES.items():
        start = datetime.now()
        temp = "TEMP " if name.startswith("_") else ""
        cursor.execute(f"CREATE OR REPLACE {temp}TABLE {name} AS {query}")
        rows = cursor.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"{name}: {rows} rows in {(datetime.now() - start).total_seconds():.0f} s", flush=True)

    cursor.execute("""
        CREATE OR REPLACE TABLE collections AS
        SELECT * EXCLUDE (r),
            r.license.uri AS license_uri, r.license.display AS license,
            r.terms.uri AS terms_uri, r.terms.display AS terms,
            r.loan_policy
        FROM collections
        ORDER BY guid_prefix
    """)


def run(name, cursor, query, params):
    start = datetime.now()
    cursor.execute(query, params)
    stop = datetime.now()

    print(cursor.execute(f"EXPLAIN ANALYZE {query}", params).fetchall()[0][1])
    print(f"{name} took {(stop - start).total_seconds() * 1000} ms")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python build_parquet.py <filtered_flat-XXXX.csv>")
    csv = sys.argv[1]
    if not os.path.exists(PARQUET) or os.path.getmtime(PARQUET) < os.path.getmtime(csv):
        print(f"converting {csv} -> {PARQUET}", flush=True)
        convert_csv(csv)

    d = duckdb.connect("arctos.duckdb", config={
        "memory_limit": "12GB",
        "preserve_insertion_order": False,
    })

    with d.cursor() as cursor:
        build_database(cursor)
