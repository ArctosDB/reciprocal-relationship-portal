from typing import Any

from fastapi import FastAPI, APIRouter

app = FastAPI(
    title="Arctos Reciprocal Relationship Portal",
    description="""
Search portal for querying nested Arctos parasite/pathogen attributes, parts, and relations
"""
)

router = APIRouter()


@router.get("/")
def index() -> Any:
    return "Hello, world!"


app.include_router(router, prefix="/api")
