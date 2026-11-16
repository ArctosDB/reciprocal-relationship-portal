from abc import ABCMeta, abstractmethod

from fastapi.responses import StreamingResponse

from vulpes.models.query import ArctosQuery
from vulpes.models.responses import Schema, SearchResults, SearchSummary, Taxon


class CSVStreamingResponse(StreamingResponse):
    media_type = "text/csv"


class QueryEngine(metaclass=ABCMeta):
    name: str
    """Short engine id, used in span names."""

    @abstractmethod
    def download(self, query: ArctosQuery) -> CSVStreamingResponse:
        raise NotImplementedError

    @abstractmethod
    def relations(self, query: ArctosQuery) -> SearchResults:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: ArctosQuery) -> SearchResults:
        raise NotImplementedError

    @abstractmethod
    def schema(self) -> Schema:
        raise NotImplementedError

    @abstractmethod
    def summary(self, query: ArctosQuery) -> SearchSummary:
        raise NotImplementedError

    @abstractmethod
    def taxa(self, q: str, rank: str | None, limit: int) -> list[Taxon]:
        """Taxon names starting with `q`, most records first."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
