"""Every error the API sends has one shape, `{"error": <HTTP phrase>, "message": ...}`,
and `message` is written for the portal to show as it is.

Each one is logged: a 4xx with its message and the request, a 500 with its
traceback and an id. The 500's message carries the same id, so a user's report
leads to the exact log line.
"""

import logging
import secrets
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vulpes.models.search import BadQueryError

logger = logging.getLogger("vulpes.errors")


def _body(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": HTTPStatus(status).phrase, "message": message}, status_code=status)


def _target(method: str, path: str, query: str) -> str:
    return f"{method} {path}" + (f"?{unquote(query)}" if query else "")


def _client_error(request: Request, status: int, message: str) -> JSONResponse:
    logger.warning("%d %s: %s", status, _target(request.method, request.url.path, request.url.query), message)
    return _body(status, message)


_SENTENCES = {
    "extra_forbidden": "{name} is not a parameter",
    "missing": "{name} is required",
    "greater_than_equal": "{name} must be at least {ge}",
    "less_than_equal": "{name} must be at most {le}",
    "int_parsing": "{name} must be a whole number",
    "int_from_float": "{name} must be a whole number",
    "literal_error": "{name} must be one of {expected}",
}
"""Pydantic error types as sentences; `ctx` fills the placeholders after `name`."""


def _readable(error: dict[str, Any]) -> str:
    """One pydantic error as a sentence about the parameter it names."""
    loc = error.get("loc", ())
    name = f"`{loc[-1]}`" if len(loc) > 1 else "the request"
    kind: str = error.get("type", "")
    if kind.startswith("date"):
        return f"{name} must be a date, YYYY-MM-DD"
    if kind in _SENTENCES:
        return _SENTENCES[kind].format(name=name, **(error.get("ctx") or {}))
    return f"{name}: {error.get('msg', 'is not valid')}"


def _bad_query(request: Request, error: BadQueryError) -> JSONResponse:
    """A row outside the grammar."""
    return _client_error(request, 400, str(error))


def _invalid(request: Request, error: RequestValidationError) -> JSONResponse:
    """A parameter of the wrong type or out of range, or one the API does not take."""
    return _client_error(request, 422, "; ".join(_readable(e) for e in error.errors()))


def _http(request: Request, error: HTTPException) -> JSONResponse:
    """A route that does not exist, or a method it does not take."""
    missing = error.status_code == HTTPStatus.NOT_FOUND
    message = f"nothing at {request.url.path}" if missing else str(error.detail)
    return _client_error(request, error.status_code, message)


class _Unexpected:
    """Turns any other exception into a logged 500 with an id. It sits inside
    the CORS middleware so the portal can read the reply; Starlette's own
    handler sits outside it, where the reply has no CORS headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            if started:
                # Part of the reply is already sent, so there is no error body to give.
                raise
            error_id = secrets.token_hex(4)
            query = scope.get("query_string", b"").decode("latin-1")
            logger.exception("500 [%s] %s", error_id, _target(scope["method"], scope["path"], query))
            await _body(500, f"Internal error. Quote id {error_id} when reporting it.")(scope, receive, send)


def install(app: FastAPI) -> None:
    """Call before adding the CORS middleware, so it wraps the 500 handler."""
    app.exception_handler(BadQueryError)(_bad_query)
    app.exception_handler(RequestValidationError)(_invalid)
    app.exception_handler(HTTPException)(_http)
    app.add_middleware(_Unexpected)
