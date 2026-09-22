"""The FastAPI application: local boundaries, static assets, and routes.

Boundaries enforced here (see spec §10): loopback binding is the caller's job,
but host/origin validation and the per-process session credential live in this
module, so an unrelated website cannot command the server.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..errors import WorkspaceError
from ..services import Services
from .routes import build_router

log = logging.getLogger(__name__)

TOKEN_HEADER = "x-research-token"
TOKEN_QUERY = "token"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def static_root() -> Path | None:
    """Locate bundled assets through package resources, not the cwd."""
    try:
        root = resources.files("research_workspace") / "static"
    except ModuleNotFoundError:  # pragma: no cover - package always importable here
        return None
    path = Path(str(root))
    return path if (path / "index.html").is_file() else None


def create_app(services: Services, *, dev_origin: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Research Workspace",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services
    app.state.dev_origin = dev_origin

    @app.on_event("startup")
    async def _startup() -> None:
        import asyncio

        services.bind_loop(asyncio.get_running_loop())
        recovered = services.recover()
        services.runner.start()
        log.info(
            "Opened project %s (requeued %s jobs, marked %s interrupted runs)",
            services.workspace.root,
            recovered["jobs"],
            recovered["runs"],
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await services.shutdown()

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[object]]
    ):  # type: ignore[no-untyped-def]
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host and host not in ALLOWED_HOSTS:
            return JSONResponse(
                {"error": {"code": "bad_host", "message": f"Unexpected Host header: {host!r}."}},
                status_code=400,
            )
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin, dev_origin):
            return JSONResponse(
                {
                    "error": {
                        "code": "bad_origin",
                        "message": "Cross-origin requests are not accepted.",
                    }
                },
                status_code=403,
            )

        path = request.url.path
        if path.startswith("/api/") and not _authorized(request, services.session_token):
            return JSONResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": (
                            "Missing or invalid session credential. Reload the page "
                            "served by this server."
                        ),
                    }
                },
                status_code=401,
            )
        response = await call_next(request)
        if origin and _origin_allowed(origin, dev_origin):
            response.headers["access-control-allow-origin"] = origin  # type: ignore[attr-defined]
            response.headers["access-control-allow-headers"] = TOKEN_HEADER  # type: ignore[attr-defined]
            response.headers["vary"] = "origin"  # type: ignore[attr-defined]
        return response

    @app.exception_handler(WorkspaceError)
    async def _domain_error(_request: Request, exc: WorkspaceError) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status_code)

    app.include_router(build_router(), prefix="/api")

    assets = static_root()
    if assets is not None:
        app.mount("/assets", StaticFiles(directory=str(assets / "assets")), name="assets")

        @app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return HTMLResponse(_render_index(assets, services.session_token))

        @app.get("/{path:path}", response_class=HTMLResponse)
        async def spa(path: str) -> HTMLResponse:
            candidate = assets / path
            if path and candidate.is_file():
                return HTMLResponse(candidate.read_text("utf-8"))
            return HTMLResponse(_render_index(assets, services.session_token))

    else:

        @app.get("/", response_class=PlainTextResponse)
        async def missing_assets() -> PlainTextResponse:
            return PlainTextResponse(
                "The browser assets are not built.\n\n"
                "This happens in a source checkout. Build them once:\n"
                "  npm --prefix frontend install\n"
                "  npm --prefix frontend run build\n\n"
                "Published wheels already contain the built assets.\n",
                status_code=503,
            )

    return app


def _render_index(assets: Path, token: str) -> str:
    """Inject the session credential into the served page.

    An unrelated website cannot read this HTML, so it cannot obtain the token.
    """
    html = (assets / "index.html").read_text("utf-8")
    marker = '<meta name="research-token" content="">'
    replacement = f'<meta name="research-token" content="{token}">'
    if marker in html:
        return html.replace(marker, replacement)
    return html.replace("<head>", f"<head>\n    {replacement}", 1)


def _authorized(request: Request, token: str) -> bool:
    supplied = request.headers.get(TOKEN_HEADER) or request.query_params.get(TOKEN_QUERY)
    if not supplied:
        return False
    import hmac

    return hmac.compare_digest(supplied, token)


def _origin_allowed(origin: str, dev_origin: str | None) -> bool:
    if dev_origin and origin == dev_origin:
        return True
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
