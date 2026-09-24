"""Domain errors, each mapped to an explicit HTTP response by the API layer."""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base class for errors that are reported to the client as structured JSON."""

    code = "workspace_error"
    status_code = 400

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message, **self.details}}


class NotFound(WorkspaceError):
    code = "not_found"
    status_code = 404


class InvalidInput(WorkspaceError):
    code = "invalid_input"
    status_code = 422


class PathEscape(WorkspaceError):
    """A requested path resolved outside the project directory."""

    code = "path_escape"
    status_code = 400


class RevisionConflict(WorkspaceError):
    """A version-checked write was attempted against a stale base revision."""

    code = "revision_conflict"
    status_code = 409

    def __init__(
        self,
        message: str,
        *,
        expected_revision: str | None,
        actual_revision: str | None,
        current_text: str | None = None,
        proposed_text: str | None = None,
    ) -> None:
        super().__init__(
            message,
            expected_revision=expected_revision,
            actual_revision=actual_revision,
            current_text=current_text,
            proposed_text=proposed_text,
        )


class Conflict(WorkspaceError):
    """The thing was already decided, so this request came too late."""

    code = "conflict"
    status_code = 409


class AgentUnavailable(WorkspaceError):
    """No agent backend is configured, installed, or authenticated."""

    code = "agent_unavailable"
    status_code = 503


class ProjectExists(WorkspaceError):
    code = "project_exists"
    status_code = 409
