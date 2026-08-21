#!/usr/bin/env python3
"""Validate a staged build-output.json against
ci/schemas/build-output.schema.json (PR1 item 7).

Uses the `jsonschema` package when it's importable (the shadow workflow
installs it explicitly -- see .github/workflows/integration-shadow.yml).
When it isn't available (e.g. a minimal local checkout that only has
PyYAML, same as every other script in this repo), falls back to a small,
dependency-free structural check covering exactly the required-field/type
constraints this schema actually uses (`type`, `required`, `enum`,
`additionalProperties`, `const`) -- not a general JSON Schema
implementation, just enough to catch the mistakes emit_build_output.py
could realistically make. Every finding is returned as a human-readable
string; an empty list means valid.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = REPO_ROOT / "ci" / "schemas" / "build-output.schema.json"


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _check_type(value: Any, type_decl) -> bool:
    types = type_decl if isinstance(type_decl, list) else [type_decl]
    for t in types:
        py_type = _TYPE_MAP.get(t)
        if py_type is None:
            continue
        if t == "integer" and isinstance(value, bool):
            continue  # bool is technically an int subclass; schema means real ints
        if isinstance(value, py_type):
            return True
    return False


def _validate_node(value: Any, schema: dict, path: str, errors: List[str]) -> None:
    if "type" in schema and not _check_type(value, schema["type"]):
        errors.append(f"{path}: expected type {schema['type']!r}, got {type(value).__name__}")
        return

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}, got {value!r}")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']!r}")

    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, subvalue in value.items():
            if key in props:
                _validate_node(subvalue, props[key], f"{path}.{key}", errors)
            elif isinstance(additional, dict):
                _validate_node(subvalue, additional, f"{path}.{key}", errors)
            elif additional is False:
                errors.append(f"{path}: unexpected property {key!r}")

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate_node(item, schema["items"], f"{path}[{i}]", errors)


def _validate_fallback(doc: Any, schema: dict) -> List[str]:
    errors: List[str] = []
    _validate_node(doc, schema, "$", errors)
    return errors


def validate_document(doc: Any, schema_path: Path = DEFAULT_SCHEMA) -> List[str]:
    schema = _load_json(schema_path)
    try:
        import jsonschema

        validator = jsonschema.Draft7Validator(schema)
        return [f"{'.'.join(str(p) for p in e.path) or '$'}: {e.message}" for e in validator.iter_errors(doc)]
    except ImportError:
        return _validate_fallback(doc, schema)


def validate_file(build_output_path: Path, schema_path: Path = DEFAULT_SCHEMA) -> List[str]:
    if not build_output_path.is_file():
        return [f"{build_output_path}: file does not exist"]
    try:
        doc = _load_json(build_output_path)
    except json.JSONDecodeError as exc:
        return [f"{build_output_path}: invalid JSON: {exc}"]
    return validate_document(doc, schema_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_output", type=Path, help="path to a staged build-output.json")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args(argv)

    errors = validate_file(args.build_output, args.schema)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        print(f"INVALID: {len(errors)} violation(s)", file=sys.stderr)
        return 1
    print(f"OK: {args.build_output} matches {args.schema}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
