"""Generate `frontend/src/apiGenerated.ts` from FastAPI's OpenAPI schema (`D-10`).

Keeps frontend wire contracts in lockstep with `fleetforge.api.main:create_app().openapi()`
without hand-maintained duplicate server types. Supports `--write` and `--check`.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_OUTPUT = REPO_ROOT / "frontend" / "src" / "apiGenerated.ts"


def _load_openapi() -> dict[str, Any]:
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from fleetforge.api.main import create_app

    return dict(create_app().openapi())


def _ts_string_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _ts_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _ts_string_literal(value)
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


def _dedupe_union(arms: list[str]) -> str:
    seen: list[str] = []
    has_null = False
    for arm in arms:
        if arm == "null":
            has_null = True
            continue
        if arm not in seen:
            seen.append(arm)
    if has_null:
        seen.append("null")
    return " | ".join(seen) if seen else "unknown"


def _ts_type_expr(prop: dict[str, Any], schemas: dict[str, Any]) -> str:
    if "$ref" in prop:
        return str(prop["$ref"]).rsplit("/", 1)[-1]

    if "const" in prop:
        return _ts_literal(prop["const"])

    if "enum" in prop:
        arms = [_ts_literal(v) for v in prop["enum"]]
        if prop.get("type") == "null" and "null" not in arms:
            arms.append("null")
        return _dedupe_union(arms)

    if "anyOf" in prop or "oneOf" in prop:
        branches = prop.get("anyOf") or prop.get("oneOf") or []
        return _dedupe_union([_ts_type_expr(b, schemas) for b in branches])

    if "allOf" in prop:
        items = prop["allOf"]
        if len(items) == 1:
            return _ts_type_expr(items[0], schemas)
        return " & ".join(_ts_type_expr(item, schemas) for item in items)

    schema_type = prop.get("type")
    if isinstance(schema_type, list):
        arms = [_ts_type_expr({**prop, "type": t}, schemas) for t in schema_type]
        return _dedupe_union(arms)

    if schema_type == "string":
        return "string"
    if schema_type in ("integer", "number"):
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"
    if schema_type == "array":
        items_schema = prop.get("items")
        item_ts = (
            _ts_type_expr(items_schema, schemas) if isinstance(items_schema, dict) else "unknown"
        )
        if " | " in item_ts or " & " in item_ts:
            return f"({item_ts})[]"
        return f"{item_ts}[]"
    if schema_type == "object":
        add_props = prop.get("additionalProperties")
        if isinstance(add_props, dict):
            return f"Record<string, {_ts_type_expr(add_props, schemas)}>"
        if "properties" in prop:
            return _render_inline_object(prop, schemas)
        return "Record<string, unknown>"

    return "unknown"


def _collect_object_members(
    schema: dict[str, Any],
    schemas: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    props: dict[str, dict[str, Any]] = {}
    required: set[str] = set(schema.get("required", ()))
    for item in schema.get("allOf", ()):
        if "$ref" in item:
            ref_name = str(item["$ref"]).rsplit("/", 1)[-1]
            sub_props, sub_req = _collect_object_members(schemas[ref_name], schemas)
            props.update(sub_props)
            required |= sub_req
        elif isinstance(item, dict):
            sub_props, sub_req = _collect_object_members(item, schemas)
            props.update(sub_props)
            required |= sub_req
    props.update(schema.get("properties", {}))
    return props, required


def _render_inline_object(schema: dict[str, Any], schemas: dict[str, Any]) -> str:
    props, required = _collect_object_members(schema, schemas)
    if not props:
        return "Record<string, unknown>"
    parts: list[str] = []
    for field_name, field_schema in props.items():
        opt = "" if field_name in required else "?"
        ts_expr = _ts_type_expr(field_schema, schemas)
        parts.append(f"{field_name}{opt}: {ts_expr}")
    return "{ " + "; ".join(parts) + " }"


def _render_schema(name: str, schema: dict[str, Any], schemas: dict[str, Any]) -> str:
    if schema.get("type") == "object" or "properties" in schema or "allOf" in schema:
        props, required = _collect_object_members(schema, schemas)
        if not props:
            add_props = schema.get("additionalProperties")
            if isinstance(add_props, dict):
                return f"export type {name} = Record<string, {_ts_type_expr(add_props, schemas)}>"
            return f"export type {name} = Record<string, unknown>"
        members: list[str] = []
        for field_name, field_schema in props.items():
            opt = "" if field_name in required else "?"
            ts_expr = _ts_type_expr(field_schema, schemas)
            members.append(f"{field_name}{opt}: {ts_expr}")
        inline = f"export type {name} = {{ {'; '.join(members)} }}"
        if len(members) <= 4 and len(inline) <= 100:
            return inline
        body = "\n".join(f"  {m}" for m in members)
        return f"export type {name} = {{\n{body}\n}}"

    return f"export type {name} = {_ts_type_expr(schema, schemas)}"


def render_openapi_ts(openapi: dict[str, Any]) -> str:
    schemas: dict[str, Any] = openapi.get("components", {}).get("schemas", {})
    blocks: list[str] = [
        "// Generated from FastAPI OpenAPI (`scripts/generate_openapi_ts.py --write`).",
        "// Do not edit by hand; re-run `scripts/generate_openapi_ts.py --write` after API schema changes.",
        "export type Health = { status: string; version: string; commit: string; built_at: string }",
    ]
    for name in sorted(schemas):
        blocks.append(_render_schema(name, schemas[name], schemas))
    return "\n".join(blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Write frontend/src/apiGenerated.ts")
    mode.add_argument(
        "--check",
        action="store_true",
        help="Verify frontend/src/apiGenerated.ts matches create_app().openapi()",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Target TypeScript file path",
    )
    args = parser.parse_args(argv)

    expected = render_openapi_ts(_load_openapi())
    out_path: Path = args.output

    if args.write:
        out_path.write_text(expected, encoding="utf-8")
        return 0

    if not out_path.exists():
        sys.stderr.write(f"{out_path} does not exist; run scripts/generate_openapi_ts.py --write\n")
        return 1

    actual = out_path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=str(out_path),
                tofile="expected (from create_app().openapi())",
            )
        )
        sys.stderr.write(
            "OpenAPI TypeScript contract drift detected; run `scripts/generate_openapi_ts.py --write`:\n"
            + diff
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
