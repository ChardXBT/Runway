"""No API response may return token content (spec 05 section 3).

Walks the generated OpenAPI document: every schema reachable from a
response body (through ``$ref`` chains) must carry no field whose name is
token material. Request bodies are exempt by construction - the import
route legitimately takes ``access_token`` *in*. ``token_ref`` is a lookup
handle, not a token, so it is not on the list.
"""

from __future__ import annotations

from pathlib import Path

from runway.api.app import create_app
from runway.config import Settings

# Exact field names that may never appear in a response schema.
DENIED_FIELDS = {
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "code_verifier",
    "password",
    "token",
    "token_ciphertext",
    "secret_encrypted",
    "ciphertext",
    "key_hash",
}


def _resolve(document: dict, schema: dict) -> dict:
    while "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/components/schemas/"), ref
        schema = document["components"]["schemas"][ref[len("#/components/schemas/") :]]
    return schema


def _iter_schema_fields(document: dict, schema: dict, seen: set[int]):
    schema = _resolve(document, schema)
    if id(schema) in seen:
        return
    seen.add(id(schema))
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        yield from properties
        for child in properties.values():
            yield from _iter_schema_fields(document, child, seen)
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            yield from _iter_schema_fields(document, child, seen)
    for keyword in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(keyword) or []:
            if isinstance(child, dict):
                yield from _iter_schema_fields(document, child, seen)


def test_openapi_responses_never_return_token_content(tmp_path: Path) -> None:
    application = create_app(Settings(data_dir=tmp_path / "data"))
    document = application.openapi()
    found: list[str] = []
    for path, operations in document.get("paths", {}).items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for status, response in (operation.get("responses") or {}).items():
                for media in (response.get("content") or {}).values():
                    schema = media.get("schema")
                    if not isinstance(schema, dict):
                        continue
                    for field_name in _iter_schema_fields(document, schema, set()):
                        if field_name in DENIED_FIELDS:
                            found.append(f"{method.upper()} {path} -> {status} : {field_name}")
    assert found == [], "response schemas carry token material: " + ", ".join(sorted(found))
