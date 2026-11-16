import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from vulpes.engines.duckdb import DuckDbQueryEngine
from vulpes.models.search import BadQueryError
from vulpes.routes.router import make_router

app = FastAPI(
    title="Arctos Reciprocal Relationship Portal",
    description="""
Search portal for querying nested Arctos parasite/pathogen attributes, parts, and relations
""",
)


@app.exception_handler(BadQueryError)
def bad_query(_request: Request, error: BadQueryError) -> JSONResponse:
    """A row outside the grammar is a 400 in the envelope the portal reads: it
    shows `message` where the failed call's result would have gone."""
    return JSONResponse({"error": "Bad Request", "message": str(error)}, status_code=400)


router = APIRouter()


@router.get("/")
def index() -> str:
    return "Hello, world!"


app.include_router(router, prefix="/api")
# Without CODETABLES_PATH the engine looks for `code-tables` beside the database.
code_tables = os.environ.get("CODETABLES_PATH")
app.include_router(
    make_router(
        DuckDbQueryEngine(
            os.environ.get("DUCKDB_PATH") or "../shared/data/arctos.duckdb",
            Path(code_tables) if code_tables else None,
        )
    ),
    prefix="/api/duckdb",
    tags=["duckdb"],
)
app.add_middleware(GZipMiddleware, compresslevel=6)
# The portal is served from another origin, and everything here is a public read.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])
