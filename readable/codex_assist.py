"""Codex subscription CLI adapter for glossary and failed-paragraph assistance."""
from __future__ import annotations

from copy import deepcopy

from .codex_provider import call_json
from .config import Config
from .structure import ClaudeError


def register() -> None:
    """Initialize optional providers at CLI/Web startup without circular imports."""
    from .assist import ASSIST_PROVIDERS
    ASSIST_PROVIDERS.setdefault("codex", call_assist)


def _strict_schema(schema: dict) -> dict:
    """Codex structured output requires closed objects and all fields required."""
    result = deepcopy(schema)
    if result.get("type") == "object":
        props = result.get("properties", {})
        result["properties"] = {key: _strict_schema(value) for key, value in props.items()}
        result["required"] = list(props)
        result["additionalProperties"] = False
    elif result.get("type") == "array":
        result["items"] = _strict_schema(result["items"])
    return result


def _validate(value, schema: dict) -> None:
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        if not isinstance(value, dict) or set(value) != set(props):
            raise ValueError("unexpected object fields")
        for key, field in props.items():
            _validate(value[key], field)
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError("expected array")
        for item in value:
            _validate(item, schema["items"])
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError("expected string")
    else:
        raise ValueError("unsupported schema type")


def call_assist(system_prompt: str, user_text: str, cfg: Config, schema: dict,
                cwd=None, runner=None) -> tuple[dict, dict]:
    """Return validated assistance JSON; never expose the PDF directory to CLI."""
    keys = set(schema.get("properties", {}))
    if keys not in ({"fixes", "remove"}, {"units"}):
        raise ClaudeError("Codex翻訳補助の応答スキーマが不正です")
    strict = _strict_schema(schema)

    def normalize(data):
        _validate(data, strict)
        field, identifier = ("units", "id") if "units" in keys else ("fixes", "en")
        seen = set()
        for item in data[field]:
            key = item[identifier].strip()
            if not key or key.lower() in seen:
                raise ValueError("empty or duplicate identifier")
            seen.add(key.lower())
        return data

    return call_json(system_prompt, user_text, cfg, strict, normalize, cwd=cwd, runner=runner,
                     transport_hint="This is translation assistance, not PDF structure analysis. Empty results use arrays. ")
