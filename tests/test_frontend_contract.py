"""Frontend `api.ts` <-> FastAPI OpenAPI schema parity guard (`D-3 / A-16 / T-6`).

Every exported object type in `frontend/src/api.ts` must map to an OpenAPI component
schema (or `/v1/healthz`'s inline dictionary), and every field declared on the
TypeScript type must exist in the server's schema so the UI never builds features
on phantom fields the API never sends.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fleetforge.api.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
API_TS_PATH = REPO_ROOT / "frontend" / "src" / "api.ts"

_TS_OBJECT_OPEN_RE = re.compile(
    r"^export\s+type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{",
    re.MULTILINE,
)
_TS_FIELD_RE = re.compile(r"(?:^|[;{,])\s*([A-Za-z_][A-Za-z0-9_]*)\??\s*:", re.MULTILINE)

# Explicit name aliases where `api.ts` uses a shorter local alias for a FastAPI schema.
# Unlisted `export type X = { ... }` names must match `components.schemas[X]` 1:1.
_OPENAPI_SCHEMA_ALIASES: dict[str, str] = {
    "Me": "MeResponse",
    "LoginResult": "LoginResponse",
}

# `/v1/healthz` returns `{"status": "ok", **build_info().as_dict()}` directly as a dict.
_INLINE_ENDPOINT_FIELDS: dict[str, set[str]] = {
    "Health": {"status", "version", "commit", "built_at"},
}


def _strip_ts_comments(source: str) -> str:
    lines: list[str] = []
    in_block = False
    for line in source.splitlines():
        i = 0
        out: list[str] = []
        while i < len(line):
            if in_block:
                end = line.find("*/", i)
                if end == -1:
                    i = len(line)
                else:
                    in_block = False
                    i = end + 2
            elif line.startswith("//", i):
                break
            elif line.startswith("/*", i):
                in_block = True
                i += 2
            else:
                out.append(line[i])
                i += 1
        lines.append("".join(out))
    return "\n".join(lines)


def _parse_api_ts_object_types(source: str) -> dict[str, set[str]]:
    cleaned = _strip_ts_comments(source)
    types: dict[str, set[str]] = {}
    for match in _TS_OBJECT_OPEN_RE.finditer(cleaned):
        type_name = match.group(1)
        pos = match.end()
        depth = 1
        top_level_chars: list[str] = []
        while pos < len(cleaned) and depth > 0:
            ch = cleaned[pos]
            if ch == "{":
                depth += 1
                if depth == 2:
                    top_level_chars.append("{}")
            elif ch == "}":
                depth -= 1
            elif depth == 1:
                top_level_chars.append(ch)
            pos += 1
        body = "".join(top_level_chars)
        fields = {m.group(1) for m in _TS_FIELD_RE.finditer(body)}
        types[type_name] = fields
    return types


def _schema_properties(components: dict[str, Any], schema_name: str) -> set[str]:
    schema = components[schema_name]
    props: set[str] = set(schema.get("properties", {}).keys())
    for item in schema.get("allOf", ()):
        if "$ref" in item:
            ref_name = str(item["$ref"]).rsplit("/", 1)[-1]
            props |= _schema_properties(components, ref_name)
        elif "properties" in item:
            props |= set(item["properties"].keys())
    return props


def test_api_ts_fields_exist_in_openapi() -> None:
    """Every field of every exported object type in `frontend/src/api.ts` exists in OpenAPI."""
    openapi = create_app().openapi()
    components: dict[str, Any] = openapi.get("components", {}).get("schemas", {})
    ts_types = _parse_api_ts_object_types(API_TS_PATH.read_text(encoding="utf-8"))
    assert ts_types, "failed to parse any exported object types from frontend/src/api.ts"

    missing_types: list[str] = []
    phantom_fields: dict[str, list[str]] = {}

    for ts_name, ts_fields in sorted(ts_types.items()):
        if ts_name in _INLINE_ENDPOINT_FIELDS:
            allowed = _INLINE_ENDPOINT_FIELDS[ts_name]
            extra = sorted(ts_fields - allowed)
            if extra:
                phantom_fields[ts_name] = extra
            continue

        schema_name = _OPENAPI_SCHEMA_ALIASES.get(ts_name, ts_name)
        if schema_name not in components:
            missing_types.append(f"{ts_name} (-> {schema_name})")
            continue

        server_fields = _schema_properties(components, schema_name)
        extra = sorted(ts_fields - server_fields)
        if extra:
            phantom_fields[f"{ts_name} ({schema_name})"] = extra

    assert not missing_types, f"api.ts object types missing from OpenAPI schemas: {missing_types}"
    assert not phantom_fields, (
        f"api.ts declares fields absent from FastAPI OpenAPI schemas: {phantom_fields}"
    )
