from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Response
from opentelemetry import trace

from vulpes.engines.abc import CSVStreamingResponse, QueryEngine
from vulpes.models.query import ArctosQuery
from vulpes.models.responses import Schema, SearchResults, SearchSummary, TaxaResults

tracer = trace.get_tracer(__name__)

MAPPER_CONFIG = (Path(__file__).parent.parent / "config" / "berkeleymapper.xml").read_bytes()

# Nothing an engine answers changes until the service restarts on a new
# snapshot, so a browser may reuse an answer instead of asking again. The
# portal asks for the same search again on every tab switch.
SEARCH_CACHE = "public, max-age=300"
SCHEMA_CACHE = "public, max-age=900"


def make_router(engine: QueryEngine) -> APIRouter:
    """
    Factory for making a router that relies on one search engine
    """
    router = APIRouter()

    @router.get("/download", response_class=CSVStreamingResponse)
    def download(query: Annotated[ArctosQuery, Query()]) -> CSVStreamingResponse:
        with tracer.start_as_current_span(f"{engine.name}.download"):
            return engine.download(query)

    @router.get("/berkeleymapper.xml")
    def berkeleymapper() -> Response:
        """The BerkeleyMapper config for a `cols=map` download."""
        return Response(MAPPER_CONFIG, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})

    @router.get("/relations")
    def relations(response: Response, query: Annotated[ArctosQuery, Query()]) -> SearchResults:
        response.headers["Cache-Control"] = SEARCH_CACHE
        with tracer.start_as_current_span(f"{engine.name}.relations"):
            return engine.relations(query)

    @router.get("/schema")
    def schema(response: Response) -> Schema:
        response.headers["Cache-Control"] = SCHEMA_CACHE
        with tracer.start_as_current_span(f"{engine.name}.schema"):
            return engine.schema()

    @router.get("/search")
    def search(response: Response, query: Annotated[ArctosQuery, Query()]) -> SearchResults:
        response.headers["Cache-Control"] = SEARCH_CACHE
        # To record a request detail on the trace, set an attribute on
        # opentelemetry's current span here, for example `arctos.page`.
        with tracer.start_as_current_span(f"{engine.name}.search"):
            return engine.search(query)

    @router.get("/summary")
    def summary(response: Response, query: Annotated[ArctosQuery, Query()]) -> SearchSummary:
        response.headers["Cache-Control"] = SEARCH_CACHE
        with tracer.start_as_current_span(f"{engine.name}.summary"):
            return engine.summary(query)

    @router.get("/taxa")
    def taxa(
        response: Response,
        q: Annotated[str, Query(description="The start of a taxon name, at least two characters.")],
        rank: Annotated[str | None, Query(description="A rank id of the schema, to keep that rank's names.")] = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
    ) -> TaxaResults:
        # The names change only with the snapshot, and the form asks on every keystroke.
        response.headers["Cache-Control"] = "public, max-age=3600"
        with tracer.start_as_current_span(f"{engine.name}.taxa"):
            return TaxaResults(matches=engine.taxa(q, rank, limit))

    return router
