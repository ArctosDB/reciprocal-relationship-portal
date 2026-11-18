"""A six-record database in the layout `importer/build_duckdb.py` writes, small
enough to read every expected answer off this file.

    1  MSB:Mamm:1   Sorex cinereus          a shrew, hosting the cestode and one parasite outside the database
    2  MSB:Host:1   Sorex cinereus          the same animal catalogued a second time
    3  MSB:Mamm:2   Peromyscus maniculatus  a mouse with no event, only the dates on the record
    4  MSB:Para:1   Cestoda                 the tapeworm lot both shrew records point at
    5  MSB:Para:2   Ixodes (two names)      a tick lot with two determinations, on the mouse
    6  DMNS:Mamm:1  Sorex monticolus        a shrew with nothing else recorded
"""

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING

import duckdb
import pytest

from vulpes.engines.duckdb import DuckDbQueryEngine

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = """
CREATE TABLE snapshot AS SELECT DATE '2026-07-17' AS snapshot_date;

CREATE TABLE records (
    collection_object_id BIGINT, guid VARCHAR, guid_prefix VARCHAR, scientific_name VARCHAR,
    phylum VARCHAR, phylclass VARCHAR, phylorder VARCHAR, family VARCHAR, subfamily VARCHAR,
    genus VARCHAR, species VARCHAR, country VARCHAR, state_prov VARCHAR, locality_id BIGINT,
    began_date VARCHAR, ended_date VARCHAR
);
INSERT INTO records VALUES
    (1, 'MSB:Mamm:1', 'MSB:Mamm', 'Sorex cinereus', 'Chordata', 'Mammalia', 'Eulipotyphla', 'Soricidae', NULL,
        'Sorex', 'Sorex cinereus', 'United States', 'New Mexico', 10, '1990-06-01', '1990-06-02'),
    (2, 'MSB:Host:1', 'MSB:Host', 'Sorex cinereus', 'Chordata', 'Mammalia', 'Eulipotyphla', 'Soricidae', NULL,
        'Sorex', 'Sorex cinereus', 'United States', 'Colorado', 11, '1990', '1990'),
    (3, 'MSB:Mamm:2', 'MSB:Mamm', 'Peromyscus maniculatus', 'Chordata', 'Mammalia', 'Rodentia', 'Cricetidae', NULL,
        'Peromyscus', 'Peromyscus maniculatus', 'United States', 'Alaska', 12, '2005-07-04', '2005-07-04'),
    (4, 'MSB:Para:1', 'MSB:Para', 'Cestoda', 'Platyhelminthes', 'Cestoda', NULL, NULL, NULL,
        NULL, NULL, 'United States', 'New Mexico', 10, '1990-06-01', '1990-06-01'),
    (5, 'MSB:Para:2', 'MSB:Para', 'Ixodes scapularis or Ixodes pacificus', 'Arthropoda', 'Arachnida', 'Ixodida',
        'Ixodidae; Ixodidae', NULL, 'Ixodes; Ixodes', 'Ixodes scapularis; Ixodes pacificus',
        'United States', 'Alaska', 12, '2005-07-04', '2005-07-04'),
    (6, 'DMNS:Mamm:1', 'DMNS:Mamm', 'Sorex monticolus', 'Chordata', 'Mammalia', 'Eulipotyphla', 'Soricidae', NULL,
        'Sorex', 'Sorex monticolus', 'Canada', 'British Columbia', NULL, NULL, NULL);

CREATE TABLE localities (
    locality_id BIGINT, spec_locality VARCHAR, higher_geog VARCHAR, dec_lat DOUBLE, dec_long DOUBLE,
    coordinateuncertaintyinmeters BIGINT
);
INSERT INTO localities VALUES
    (10, 'Sandia Mountains, Cibola National Forest', 'North America, United States, New Mexico', 35.2, -106.4, 100),
    (11, 'Mountains of Sandia', 'North America, United States, Colorado', NULL, NULL, NULL),
    (12, 'Fairbanks', 'North America, United States, Alaska', 64.8, -147.7, NULL);

CREATE TABLE specimen_events (
    id BIGINT, specimen_event_id BIGINT, locality_id BIGINT, specimen_event_type VARCHAR, order_by_vs BIGINT,
    began_date VARCHAR, ended_date VARCHAR, verbatim_date VARCHAR, collecting_method VARCHAR,
    collecting_source VARCHAR, habitat VARCHAR, verificationstatus VARCHAR
);
INSERT INTO specimen_events (id, specimen_event_id, locality_id, specimen_event_type, began_date, ended_date) VALUES
    (1, 1001, 10, 'collection', '1990-06-01', '1990-06-02'),
    -- the shrew was handled again eleven years on, somewhere else
    (1, 1002, 12, 'encounter', '2001-01-01', '2001-01-01'),
    (2, 1003, 11, 'collection', '1990', '1990'),
    (4, 1004, 10, 'collection', '1990-06-01', '1990-06-01'),
    (5, 1005, 12, 'collection', '2005-07-04', '2005-07-04');

CREATE TABLE attributes (
    id BIGINT, attribute_type VARCHAR, attribute_value VARCHAR, attribute_units VARCHAR, attribute_date VARCHAR,
    attribute_method VARCHAR, attribute_remark VARCHAR, determiner_agent_id BIGINT, determiner_name VARCHAR
);
INSERT INTO attributes (id, attribute_type, attribute_value, attribute_units, attribute_method) VALUES
    (1, 'detected', 'endoparasite', NULL, 'necropsy'),
    (1, 'examined for', 'endoparasite', NULL, 'necropsy'),
    (1, 'sex', 'male', NULL, NULL),
    (2, 'sex', 'female', NULL, NULL),
    (3, 'detected', 'virus: Orthohantavirus', NULL, 'Immunoblot assay'),
    (3, 'sex', 'male', NULL, NULL),
    (3, 'weight', '12', 'g', NULL),
    (6, 'not detected', 'endoparasite', NULL, NULL);

CREATE TABLE parts (
    id BIGINT, part_id BIGINT, parent_part_id BIGINT, part_name VARCHAR, condition VARCHAR, part_count BIGINT,
    disposition VARCHAR, part_remark VARCHAR, part_barcode VARCHAR, container_path VARCHAR
);
INSERT INTO parts (id, part_id, part_name, condition, part_count, disposition, part_barcode) VALUES
    (1, 501, 'liver', 'good', 1, 'in collection', 'A100'),
    (3, 502, 'liver', 'good', 1, 'used up', NULL),
    (3, 503, 'skull', 'good', 1, 'in collection', NULL);

CREATE TABLE part_attributes (
    id BIGINT, part_id BIGINT, part_name VARCHAR, attribute_type VARCHAR, attribute_value VARCHAR,
    attribute_units VARCHAR, attribute_date VARCHAR, attribute_method VARCHAR, attribute_remark VARCHAR,
    determiner_agent_id BIGINT, determiner_name VARCHAR
);
INSERT INTO part_attributes (id, part_id, part_name, attribute_type, attribute_value) VALUES
    (1, 501, 'liver', 'preservation', 'frozen');

CREATE TABLE agents (agent_id BIGINT, agent_name VARCHAR);
INSERT INTO agents VALUES (100, 'Joseph A. Cook'), (101, 'Jane Doe');

CREATE TABLE record_agents (id BIGINT, agent_id BIGINT, agent_role VARCHAR, agent_order BIGINT, agent_remark VARCHAR);
INSERT INTO record_agents (id, agent_id, agent_role) VALUES
    (1, 100, 'collector'), (2, 100, 'collector'), (2, 101, 'collector'), (3, 101, 'collector'),
    (3, 100, 'preparator');

CREATE TABLE related_records (
    src_id BIGINT, src_guid VARCHAR, src_prefix VARCHAR, relationship VARCHAR, identifier_type VARCHAR,
    dst_value VARCHAR, remarks VARCHAR, issued_by_agent_id BIGINT, issued_by_name VARCHAR,
    dst_identification VARCHAR, dst_family VARCHAR, dst_geography VARCHAR, dst_locality VARCHAR,
    dst_guid VARCHAR, dst_prefix VARCHAR, dst_id BIGINT
);
INSERT INTO related_records (src_id, relationship, dst_value, dst_identification, dst_family, dst_guid, dst_id) VALUES
    (1, 'host of parasite', 'https://arctos.database.museum/guid/MSB:Para:1', 'Cestoda', NULL, 'MSB:Para:1', 4),
    (2, 'host of parasite', 'https://arctos.database.museum/guid/MSB:Para:1', 'Cestoda', NULL, 'MSB:Para:1', 4),
    (4, 'parasite of', 'https://arctos.database.museum/guid/MSB:Mamm:1', 'Sorex cinereus', 'Soricidae', 'MSB:Mamm:1', 1),
    (4, 'parasite of', 'https://arctos.database.museum/guid/MSB:Host:1', 'Sorex cinereus', 'Soricidae', 'MSB:Host:1', 2),
    (3, 'host of parasite', 'https://arctos.database.museum/guid/MSB:Para:2', 'Ixodes', 'Ixodidae', 'MSB:Para:2', 5),
    -- a parasite in another museum: no record here, only what Arctos cached about it
    (1, 'host of parasite', 'HWML 48061', 'Hymenolepis diminuta', 'Hymenolepididae', NULL, NULL),
    (1, 'same individual as', 'https://arctos.database.museum/guid/MSB:Host:1', 'Sorex cinereus', 'Soricidae',
        'MSB:Host:1', 2),
    (3, 'ate', 'a beetle in the stomach', 'Coleoptera', NULL, NULL, NULL);
"""

# Arctos ships a table as `{"data": [...]}` or as a bare array; both are here.
CODE_TABLES: dict[str, object] = {
    "ctattribute_type": {
        "data": [
            {"attribute_type": "weight", "description": "Mass.", "value_code_table": "", "unit_code_table": "ctweight_units"},
            {"attribute_type": "sex", "description": "Sex.", "value_code_table": "ctsex_cde", "unit_code_table": ""},
            {"attribute_type": "detected", "value_code_table": "ctexamined_detected", "public": 1},
            {"attribute_type": "restricted data", "value_code_table": "ctyes_no", "public": 0},
        ]
    },
    "ctsex_cde": [{"sex_cde": "male", "description": "Male."}, {"sex_cde": "female", "description": "Female."}],
    "ctexamined_detected": {
        "data": [
            {"examined_detected": "virus: Orthohantavirus", "description": "", "documentation_url": ["https://example.org/h"]},
            {"examined_detected": "virus", "description": "Any virus."},
            {"examined_detected": "endoparasite", "description": ""},
        ]
    },
    "ctspecimen_part_name": [{"part_name": "liver", "description": ""}, {"part_name": "skull", "description": ""}],
    "ctdisposition": [],
    "ctcollection_cde": [{"collection_cde": "Mammalogy"}, {"collection_cde": "Parasitology"}],
    "ctid_references": [
        {"id_references": "same individual as", "description": ""},
        {"id_references": "parasite of", "description": "The record is a parasite of the related one."},
        {"id_references": "host of parasite", "description": ""},
        {"id_references": "ate", "description": ""},
    ],
}


@pytest.fixture(scope="session")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DuckDbQueryEngine]:
    root: Path = tmp_path_factory.mktemp("arctos")
    with duckdb.connect(str(root / "arctos.duckdb")) as db:
        db.execute(SCHEMA)
    (root / "code-tables").mkdir()
    for table, rows in CODE_TABLES.items():
        (root / "code-tables" / f"{table}.json").write_text(json.dumps(rows), encoding="utf-8")

    engine = DuckDbQueryEngine(str(root / "arctos.duckdb"))
    yield engine
    engine.close()
