import os
from typing import Any

from fastapi import FastAPI, APIRouter

app = FastAPI(
    title="Arctos Reciprocal Relationship Portal",
    description="""
Search portal for querying nested Arctos parasite/pathogen attributes, parts, and relations
""",
    root_path=os.environ.get("ROOT_PATH", ""),
)

router = APIRouter()


@router.get("/")
def index() -> Any:
    return "Hello, world!"


app.include_router(router, prefix="/api")
