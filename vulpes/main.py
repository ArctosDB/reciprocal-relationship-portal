import os
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from vulpes import errors
from vulpes.engines.duckdb import DuckDbQueryEngine
from vulpes.routes.router import make_router
from vulpes.synonyms import DEFAULT_DATASET, Synonyms

app = FastAPI(
    title="Arctos Reciprocal Relationship Portal",
    description="""
Search portal for querying nested Arctos parasite/pathogen attributes, parts, and relations
""",
)


# The portal shows an error's `message` where the failed call's result would have gone.
errors.install(app)


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
            # The ChecklistBank dataset synonyms come from, a Catalogue of Life release.
            Synonyms(os.environ.get("CHECKLISTBANK_DATASET") or DEFAULT_DATASET),
        )
    ),
    prefix="/api/duckdb",
    tags=["duckdb"],
)
app.add_middleware(GZipMiddleware, compresslevel=6)
# The portal is served from another origin, and everything here is a public read.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])
