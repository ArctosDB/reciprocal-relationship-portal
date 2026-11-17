"""Every error reply has the shape `{"error", "message"}`, and is logged."""

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from vulpes import errors
from vulpes.engines.duckdb import DuckDbQueryEngine
from vulpes.routes.router import make_router


@pytest.fixture(scope="module")
def client(engine: DuckDbQueryEngine) -> TestClient:
    # The middleware in the order `vulpes/main.py` adds it.
    app = FastAPI()
    errors.install(app)
    app.include_router(make_router(engine), prefix="/api/duckdb")

    @app.get("/api/boom")
    def boom() -> None:
        msg = "the database went away"
        raise RuntimeError(msg)

    app.add_middleware(GZipMiddleware, compresslevel=6)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("url", "status", "message"),
    [
        ("/api/duckdb/search?attr=null|x", 400, "`attr` row `null` takes no values"),
        ("/api/duckdb/search?page=0", 422, "`page` must be at least 1"),
        ("/api/duckdb/search?page=two", 422, "`page` must be a whole number"),
        ("/api/duckdb/search?from=2020-13-01", 422, "`from` must be a date, YYYY-MM-DD"),
        ("/api/duckdb/search?attr_op=xor", 422, "`attr_op` must be one of 'and' or 'or'"),
        ("/api/duckdb/search?bogus=1&page=0", 422, "`page` must be at least 1; `bogus` is not a parameter"),
        ("/api/duckdb/taxa", 422, "`q` is required"),
        ("/api/duckdb/taxa?q=So&limit=50", 422, "`limit` must be at most 20"),
        ("/api/nowhere", 404, "nothing at /api/nowhere"),
    ],
)
def test_a_client_error_says_what_was_wrong_and_is_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture, url: str, status: int, message: str
) -> None:
    with caplog.at_level(logging.WARNING, logger="vulpes.errors"):
        reply = client.get(url, headers={"Origin": "https://portal.example"})
    assert reply.status_code == status
    assert reply.json() == {"error": reply.reason_phrase, "message": message}
    assert reply.headers["access-control-allow-origin"] == "*"
    path, _, query = url.partition("?")
    assert caplog.messages == [f"{status} GET {path}" + (f"?{query}" if query else "") + f": {message}"]


def test_an_unexpected_error_is_a_500_whose_id_is_in_the_log(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="vulpes.errors"):
        reply = client.get("/api/boom?x=1", headers={"Origin": "https://portal.example"})
    assert reply.status_code == 500
    body = reply.json()
    assert body["error"] == "Internal Server Error"
    match = re.fullmatch(r"Internal error\. Quote id ([0-9a-f]{8}) when reporting it\.", body["message"])
    assert match
    # Readable by the portal, which is on another origin.
    assert reply.headers["access-control-allow-origin"] == "*"

    [record] = caplog.records
    assert record.getMessage() == f"500 [{match[1]}] GET /api/boom?x=1"
    assert record.exc_info is not None
    assert "the database went away" in str(record.exc_info[1])
