"""Command line entry point: `init`, `serve`, and `doctor`."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from .errors import WorkspaceError
from .workspace import Workspace

DEFAULT_PORT = 8765
HOST = "127.0.0.1"  # Loopback only; remote use goes through an SSH tunnel.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-workspace",
        description=(
            "A local research workspace: import papers, read cited summaries, write "
            "sourced Markdown notes, and chat with a coding agent."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Log debug output.")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a new project directory.")
    init.add_argument("path", type=Path, help="Where to create the project.")
    init.add_argument("--name", help="Project name (defaults to the directory name).")

    serve = sub.add_parser("serve", help="Serve an existing project in the browser.")
    serve.add_argument("path", type=Path, nargs="?", default=Path.cwd())
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--open", action="store_true", help="Open a browser window.")
    serve.add_argument("--reload", action="store_true", help="Reload on code changes (dev).")
    serve.add_argument(
        "--backend",
        choices=("claude", "fake"),
        help="Agent backend. 'fake' is for tests and offline UI work.",
    )
    serve.add_argument(
        "--dev-origin",
        help="Additionally accept this origin, for the Vite dev server (dev only).",
    )

    doctor = sub.add_parser("doctor", help="Check the workspace and agent setup.")
    doctor.add_argument("path", type=Path, nargs="?", default=Path.cwd())

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "doctor":
            return cmd_doctor(args)
    except WorkspaceError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


def cmd_init(args: argparse.Namespace) -> int:
    workspace = Workspace.initialize(args.path, name=args.name)
    print(f"Created project at {workspace.root}")
    print(f"  {workspace.notes_dir.relative_to(workspace.root)}/welcome.md")
    print()
    print("Next:")
    print(f"  research-workspace serve {args.path} --open")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app, static_root
    from .services import open_services

    services = open_services(args.path, backend_name=args.backend)
    app = create_app(services, dev_origin=args.dev_origin)

    url = f"http://{HOST}:{args.port}/"
    availability = services.agent_availability()

    print(f"Research Workspace — {services.workspace.root}")
    print(f"  {url}")
    if static_root() is None:
        print("  warning: browser assets are not built; run `npm --prefix frontend run build`")
    if availability.ready:
        print(f"  agent: {availability.backend} ready")
    else:
        print(f"  agent: not configured — {availability.message}")
        print("  notes and PDFs work regardless; summaries and chat need an agent.")
    print("  Ctrl-C to stop.")

    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        app,
        host=HOST,
        port=args.port,
        log_level="debug" if args.verbose else "warning",
        access_log=False,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .api.app import static_root
    from .services import open_services

    print(f"python: {sys.version.split()[0]}")

    assets = static_root()
    print(f"browser assets: {'found at ' + str(assets) if assets else 'MISSING (not built)'}")

    workspace = Workspace(args.path)
    if not workspace.exists():
        print(f"project: not a project directory ({workspace.root})")
        print("  run: research-workspace init <path>")
        return 1
    print(f"project: {workspace.root}")

    services = open_services(args.path)
    try:
        documents = services.documents.list()
        pending = [
            document.document_id
            for document in documents
            if document.extraction.get("status") not in ("ok", "failed")
        ]
        failed = [
            document.document_id
            for document in documents
            if document.extraction.get("status") == "failed"
        ]
        print(f"documents: {len(documents)} imported, {len(pending)} awaiting extraction")
        if failed:
            print(f"  extraction failed: {', '.join(failed)}")
        print(f"notes: {len(services.workspace.list_notes())}")
        print(f"references: {len(services.references.all())}")

        availability = services.agent_availability()
        print(f"agent backend: {availability.backend}")
        print(f"  installed: {'yes' if availability.installed else 'no'}")
        print(f"  authenticated: {'yes' if availability.authenticated else 'no'}")
        print(f"  {availability.message}")
        if availability.detail:
            print(f"  {availability.detail}")
        # Credentials are never printed, only reported as present or absent.
        return 0 if availability.ready and assets else 1
    finally:
        services.db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
