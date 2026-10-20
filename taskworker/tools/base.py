"""Tool abstraction shared by the worker and the verifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..policy import Assessment
    from ..runtime import RunContext


class ToolError(Exception):
    """An expected failure the model should see and recover from (bad ref, file not found...)."""


@dataclass
class ToolResult:
    content: str | list[dict]
    is_error: bool = False
    terminal: bool = False  # ends the run (finish / submit_verdict)
    data: dict = field(default_factory=dict)  # structured payload for the trace / caller

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(b.get("text", "[image]") for b in self.content)


Handler = Callable[[dict, "RunContext"], ToolResult]
Assessor = Callable[[dict, "RunContext"], "Assessment"]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    assess: Assessor | None = None  # None means the tool has no external side effects

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or list(properties),
            "additionalProperties": False}


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate_input(schema: dict, args: Any, path: str = "") -> str | None:
    """Schema check (required keys, types, enums, nested objects and array items).

    Returns an error message naming the offending field, or None. Weaker models often send
    e.g. a list of strings where objects are expected; a precise error lets them correct it.
    """
    where = path or "tool input"
    expected = _TYPES.get(schema.get("type", ""))
    if expected and (not isinstance(args, expected) or (expected is int and isinstance(args, bool))):
        return f"{where} must be of type {schema['type']}"
    if "enum" in schema and args not in schema["enum"]:
        return f"{where} must be one of {schema['enum']}"
    if isinstance(args, dict) and schema.get("type") == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in args:
                return f"missing required field '{path + '.' if path else ''}{key}'"
        for key, value in args.items():
            if key not in props:
                return f"unknown field '{path + '.' if path else ''}{key}'"
            if problem := validate_input(props[key], value, f"{path + '.' if path else ''}{key}"):
                return problem
    if isinstance(args, list) and "items" in schema:
        for i, item in enumerate(args):
            if problem := validate_input(schema["items"], item, f"{where}[{i}]"):
                return problem
    return None
