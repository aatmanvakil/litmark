"""The tool schemas and the dispatchable methods must agree.

`dispatch` resolves by getattr, so a method with no schema is callable but
never offered to the model, and a schema with no method answers
`unknown_tool`. Neither mismatch surfaces at runtime, so it is pinned here.
"""

from __future__ import annotations

import inspect

from litmark.agent.claude_sdk import ALLOWED_TOOLS
from litmark.agent.tools import INTERNAL_METHODS, TOOL_SCHEMAS, ProjectTools


def _public_methods() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(ProjectTools, inspect.isfunction)
        if not name.startswith("_")
    }


def _schema_names() -> set[str]:
    return {schema["name"] for schema in TOOL_SCHEMAS}


def test_every_schema_has_a_dispatchable_method():
    missing = _schema_names() - _public_methods()

    assert not missing, f"schemas with no ProjectTools method: {sorted(missing)}"


def test_every_public_method_is_a_tool_or_declared_internal():
    unclassified = _public_methods() - _schema_names() - INTERNAL_METHODS

    assert not unclassified, (
        "these ProjectTools methods are dispatchable but the model is never told "
        f"they exist: {sorted(unclassified)}. Add a TOOL_SCHEMAS entry, or name "
        "them in INTERNAL_METHODS."
    )


def test_list_notes_is_offered_to_the_model():
    # Required by spec.md 7: the agent finds a note the user named, not by ID.
    assert "list_notes" in _schema_names()
    assert any(name.endswith("__list_notes") for name in ALLOWED_TOOLS)


def test_internal_methods_are_not_also_schemas():
    assert not (INTERNAL_METHODS & _schema_names())


def test_every_schema_declares_an_object_input():
    for schema in TOOL_SCHEMAS:
        assert schema["input_schema"]["type"] == "object", schema["name"]
