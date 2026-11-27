"""Frontend `api*.ts` <-> FastAPI OpenAPI schema parity guard (`D-3`, `A-16`, `D-26`, `T-21`).

Globs every non-test `frontend/src/api*.ts` contract file and verifies two-way parity
against `/v1/openapi.json`:
1. Every exported object type maps to an OpenAPI component schema and declares zero phantom fields.
2. Nullability (`| null`) and closed string-literal enums match between OpenAPI and TypeScript.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

from fleetforge.api.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
GENERATOR_SCRIPT = REPO_ROOT / "scripts" / "generate_openapi_ts.py"

_TS_OBJECT_OPEN_RE = re.compile(
    r"^export\s+type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{",
    re.MULTILINE,
)
_TS_UNION_ALIAS_RE = re.compile(
    r"^export\s+type\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*((?:\|?\s*['\"][^'\"]+['\"]\s*)+(?:\|\s*string)?)\s*$",
    re.MULTILINE,
)
_TS_STRING_LIT_RE = re.compile(r"^(?:'([^']*)'|\"([^\"]*)\")$")

# Explicit name aliases where `apiTypes.ts` uses a shorter local alias for a FastAPI schema.
# Unlisted `export type X = { ... }` names must match `components.schemas[X]` 1:1.
_OPENAPI_SCHEMA_ALIASES: dict[str, str] = {
    "Me": "MeResponse",
    "LoginResult": "LoginResponse",
}

# `/v1/healthz` returns `{"status": "ok", **build_info().as_dict()}` directly as a dict.
_INLINE_ENDPOINT_FIELDS: dict[str, set[str]] = {
    "Health": {"status", "version", "commit", "built_at"},
}


def _api_ts_paths() -> list[Path]:
    return [
        p
        for p in sorted(FRONTEND_SRC.glob("api*.ts"))
        if not p.name.endswith((".test.ts", ".spec.ts"))
    ]


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


def _split_top_level(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth_angle = 0
    depth_brace = 0
    depth_bracket = 0
    depth_paren = 0
    buf: list[str] = []
    for ch in text:
        if ch == "<":
            depth_angle += 1
        elif ch == ">":
            depth_angle = max(0, depth_angle - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        if (
            ch == sep
            and depth_angle == 0
            and depth_brace == 0
            and depth_bracket == 0
            and depth_paren == 0
        ):
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_ts_union_aliases(cleaned: str) -> dict[str, set[str] | None]:
    aliases: dict[str, set[str] | None] = {}
    for match in _TS_UNION_ALIAS_RE.finditer(cleaned):
        name = match.group(1)
        rhs = match.group(2).strip()
        arms = [a.strip() for a in rhs.split("|") if a.strip()]
        lits: set[str] = set()
        open_union = False
        for arm in arms:
            m = _TS_STRING_LIT_RE.match(arm)
            if m is None:
                open_union = True
                break
            lits.add(m.group(1) if m.group(1) is not None else m.group(2))
        aliases[name] = None if open_union or not lits else lits
    return aliases


def _parse_ts_object_bodies(cleaned: str) -> dict[str, dict[str, str]]:
    """Return `{TypeName: {field_name: raw_ts_type_expr}}`."""
    types: dict[str, dict[str, str]] = {}
    for match in _TS_OBJECT_OPEN_RE.finditer(cleaned):
        type_name = match.group(1)
        pos = match.end()
        depth = 1
        chars: list[str] = []
        while pos < len(cleaned) and depth > 0:
            ch = cleaned[pos]
            if ch == "{":
                depth += 1
                chars.append(ch)
            elif ch == "}":
                depth -= 1
                if depth > 0:
                    chars.append(ch)
            else:
                chars.append(ch)
            pos += 1
        body = "".join(chars)
        fields: dict[str, str] = {}
        # Split object members on top-level newlines / semicolons.
        raw_members: list[str] = []
        for chunk in _split_top_level(body, ";"):
            raw_members.extend(_split_top_level(chunk, "\n"))
        for raw in raw_members:
            entry = raw.strip().rstrip(",").strip()
            if not entry or ":" not in entry:
                continue
            lhs, rhs = entry.split(":", 1)
            field_name = lhs.strip().rstrip("?").strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
                fields[field_name] = rhs.strip()
        types[type_name] = fields
    return types


def _ts_field_nullability_and_enum(
    type_expr: str,
    union_aliases: dict[str, set[str] | None],
) -> tuple[bool, set[str] | None]:
    arms = [a.strip() for a in _split_top_level(type_expr, "|") if a.strip()]
    nullable = "null" in arms
    non_null = [a for a in arms if a not in ("null", "undefined")]
    if not non_null:
        return nullable, None
    lits: set[str] = set()
    for arm in non_null:
        m = _TS_STRING_LIT_RE.match(arm)
        if m is not None:
            lits.add(m.group(1) if m.group(1) is not None else m.group(2))
        elif arm in union_aliases:
            alias_lits = union_aliases[arm]
            if alias_lits is None:
                return nullable, None
            lits |= alias_lits
        else:
            return nullable, None
    return nullable, lits or None


def _schema_property_map(components: dict[str, Any], schema_name: str) -> dict[str, dict[str, Any]]:
    schema = components[schema_name]
    props: dict[str, dict[str, Any]] = dict(schema.get("properties", {}))
    for item in schema.get("allOf", ()):
        if "$ref" in item:
            ref_name = str(item["$ref"]).rsplit("/", 1)[-1]
            props.update(_schema_property_map(components, ref_name))
        elif "properties" in item:
            props.update(item["properties"])
    return props


def _openapi_nullability_and_enum(
    prop: dict[str, Any],
    components: dict[str, Any],
) -> tuple[bool, set[str] | None]:
    if "$ref" in prop:
        ref_name = str(prop["$ref"]).rsplit("/", 1)[-1]
        return _openapi_nullability_and_enum(components[ref_name], components)
    if "anyOf" in prop:
        nullable = False
        non_null_branches: list[dict[str, Any]] = []
        for branch in prop["anyOf"]:
            if branch.get("type") == "null":
                nullable = True
            else:
                non_null_branches.append(branch)
        if not non_null_branches:
            return nullable, None
        lits: set[str] = set()
        for branch in non_null_branches:
            b_null, b_enum = _openapi_nullability_and_enum(branch, components)
            nullable = nullable or b_null
            if b_enum is None:
                return nullable, None
            lits |= b_enum
        return nullable, lits or None
    if "allOf" in prop and len(prop["allOf"]) == 1:
        return _openapi_nullability_and_enum(prop["allOf"][0], components)
    if "enum" in prop and all(isinstance(v, str) for v in prop["enum"]):
        return prop.get("type") == "null", set(prop["enum"])
    if "const" in prop and isinstance(prop["const"], str):
        return False, {prop["const"]}
    return prop.get("type") == "null", None


def _load_ts_contracts() -> tuple[dict[str, dict[str, str]], dict[str, set[str] | None]]:
    ts_types: dict[str, dict[str, str]] = {}
    union_aliases: dict[str, set[str] | None] = {}
    paths = _api_ts_paths()
    assert paths, "expected frontend/src/api*.ts files"
    for ts_path in paths:
        cleaned = _strip_ts_comments(ts_path.read_text(encoding="utf-8"))
        union_aliases.update(_parse_ts_union_aliases(cleaned))
        ts_types.update(_parse_ts_object_bodies(cleaned))
    return ts_types, union_aliases


def test_api_ts_fields_exist_in_openapi() -> None:
    """Every field of every exported object type in `frontend/src/api*.ts` exists in OpenAPI."""
    openapi = create_app().openapi()
    components: dict[str, Any] = openapi.get("components", {}).get("schemas", {})
    ts_types, _ = _load_ts_contracts()
    assert ts_types, "failed to parse any exported object types from frontend/src/api*.ts"

    missing_types: list[str] = []
    phantom_fields: dict[str, list[str]] = {}

    for ts_name, field_map in sorted(ts_types.items()):
        ts_fields = set(field_map)
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

        server_fields = set(_schema_property_map(components, schema_name))
        extra = sorted(ts_fields - server_fields)
        if extra:
            phantom_fields[f"{ts_name} ({schema_name})"] = extra

    assert not missing_types, f"api*.ts object types missing from OpenAPI schemas: {missing_types}"
    assert not phantom_fields, (
        f"api*.ts declares fields absent from FastAPI OpenAPI schemas: {phantom_fields}"
    )


def test_nullability_and_enums_agree_with_openapi() -> None:
    """D-26: nullability and closed string-literal enums match two-way between OpenAPI and `api*.ts`."""
    openapi = create_app().openapi()
    components: dict[str, Any] = openapi.get("components", {}).get("schemas", {})
    ts_types, union_aliases = _load_ts_contracts()

    mismatches: list[str] = []
    for ts_name, field_map in sorted(ts_types.items()):
        if ts_name in _INLINE_ENDPOINT_FIELDS:
            continue
        schema_name = _OPENAPI_SCHEMA_ALIASES.get(ts_name, ts_name)
        if schema_name not in components:
            continue
        server_props = _schema_property_map(components, schema_name)
        for field_name, ts_expr in sorted(field_map.items()):
            if field_name not in server_props:
                continue
            ts_nullable, ts_enum = _ts_field_nullability_and_enum(ts_expr, union_aliases)
            oa_nullable, oa_enum = _openapi_nullability_and_enum(
                server_props[field_name], components
            )
            if ts_nullable != oa_nullable:
                mismatches.append(
                    f"{ts_name}.{field_name}: nullability drift (TS={ts_nullable} vs OpenAPI={oa_nullable}; ts={ts_expr!r})"
                )
            if ts_enum != oa_enum:
                mismatches.append(
                    f"{ts_name}.{field_name}: enum drift (TS={sorted(ts_enum) if ts_enum else None} vs OpenAPI={sorted(oa_enum) if oa_enum else None})"
                )

    assert not mismatches, "OpenAPI <-> TypeScript contract drift:\n" + "\n".join(mismatches)


def test_generated_openapi_ts_matches_fastapi_schema() -> None:
    """D-10: `scripts/generate_openapi_ts.py --check` confirms zero drift in `apiGenerated.ts`."""
    spec = importlib.util.spec_from_file_location("generate_openapi_ts", GENERATOR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0
