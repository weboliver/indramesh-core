"""Config-schema helpers for plugin config (D-09/D-10/D-12).

D-09: a plugin declares its config as inline JSON Schema (Draft 2020-12) in
``plugin.toml``'s ``[config_schema]`` table; payloads are validated with the
``jsonschema`` library (the one new dependency this phase adds).

D-10: ``"format": "password"`` is the single secret marker — the core derives
both encryption (``crypto.py``) and masking from it.

D-12: schema-declared defaults apply at READ time only. ``apply_schema_defaults``
therefore returns a new dict and never writes a default back into stored config.
"""

import copy
from typing import Any

import jsonschema


def secret_field_names(config_schema: dict) -> set[str]:
    """Return dotted paths of every ``"format": "password"`` field (D-10).

    Recurses through object ``properties`` at any depth, so a nested
    ``credentials.properties.token = {"format": "password"}`` is discovered as
    ``"credentials.token"``. A root-level password field is its bare name, which
    keeps the flat-schema contract bit-for-bit compatible.

    Array ``items`` subschemas are deliberately NOT traversed here — Task 2's
    ``unsupported_secret_field_paths`` rejects them before persistence instead of
    silently storing them as plaintext.

    A property whose OWN name contains a literal ``"."`` is also excluded here
    (CR-02): the dotted-path representation ``crypto.py``'s ``_get_path``/
    ``_set_path``/``_delete_path`` use to address a nested secret cannot tell
    such a name apart from nesting, so treating it as protected would silently
    leave it unmasked/unencrypted. ``unsupported_secret_field_paths()`` flags it
    instead so the schema is rejected fail-closed.
    """
    names: set[str] = set()

    def walk(properties: dict, prefix: str) -> None:
        for name, spec in properties.items():
            if not isinstance(spec, dict) or "." in name:
                continue
            path = f"{prefix}{name}"
            if spec.get("format") == "password":
                names.add(path)
            nested = spec.get("properties")
            if isinstance(nested, dict):
                walk(nested, f"{path}.")

    walk(config_schema.get("properties", {}) or {}, "")
    return names


def unsupported_secret_field_paths(config_schema: dict) -> set[str]:
    """Return descriptive paths of ``format: password`` declarations that
    ``secret_field_names()``'s plain-``properties`` traversal cannot protect.

    Covers an array ``items`` subschema (no single leaf path to encrypt), a
    property name containing a literal ``"."`` (CR-02 — ambiguous against the
    dotted path representation), and any JSON-Schema composition/alternate
    keyword (``$ref``/``$defs``, ``allOf``, ``anyOf``, ``oneOf``,
    ``patternProperties``, ``additionalProperties``, ``if``/``then``/``else``
    — CR-01). ``$ref`` is never resolved: a schema using it is rejected
    rather than silently trusted. Such schemas are fail-closed: recorded here
    so ``validate_config_payload()`` can reject them before persistence
    rather than silently storing the value as plaintext.
    """
    paths: set[str] = set()

    def mark_all(node: object, path: str) -> None:
        """Record every ``format: password`` marker anywhere under ``node``."""
        if isinstance(node, dict):
            if node.get("format") == "password":
                paths.add(path)
            for key, value in node.items():
                mark_all(value, f"{path}<{key}>")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                mark_all(value, f"{path}[{index}]")

    def walk(spec: dict, path: str) -> None:
        nested = spec.get("properties")
        if isinstance(nested, dict):
            for name, child in nested.items():
                if not isinstance(child, dict):
                    continue
                child_path = f"{path}{name}"
                if "." in name:
                    # Ambiguous against the dotted-path representation even
                    # though it's a literal `properties` entry — fail closed
                    # rather than silently mishandling it (CR-02).
                    mark_all(child, f"{child_path}.")
                else:
                    walk(child, f"{child_path}.")

        items = spec.get("items")
        if isinstance(items, dict):
            mark_all(items, f"{path}[]")

        for keyword in ("patternProperties", "$defs", "definitions"):
            child = spec.get(keyword)
            if isinstance(child, dict):
                for name, sub in child.items():
                    mark_all(sub, f"{path}<{keyword}:{name}>")

        additional = spec.get("additionalProperties")
        if isinstance(additional, dict):
            mark_all(additional, f"{path}<additionalProperties>")

        for keyword in ("allOf", "anyOf", "oneOf"):
            children = spec.get(keyword)
            if isinstance(children, list):
                for index, child in enumerate(children):
                    if isinstance(child, dict):
                        mark_all(child, f"{path}<{keyword}[{index}]>")

        for keyword in ("if", "then", "else"):
            child = spec.get(keyword)
            if isinstance(child, dict):
                mark_all(child, f"{path}<{keyword}>")

        if "$ref" in spec:
            paths.add(f"{path}<$ref>")

    walk(config_schema, "")
    return paths


def validate_config_payload(config_schema: dict, payload: dict) -> None:
    """Validate ``payload`` against ``config_schema`` (D-09).

    Raises ``jsonschema.exceptions.SchemaError`` when the schema declares a
    password field in a location the core cannot protect (see
    ``unsupported_secret_field_paths()``) — the config API (Plan 282-07) maps
    ``SchemaError`` to a 422, so such a value can never be silently persisted
    as plaintext. Otherwise lets ``jsonschema.exceptions.ValidationError``
    propagate to the caller unchanged.
    """
    unsupported = unsupported_secret_field_paths(config_schema)
    if unsupported:
        raise jsonschema.exceptions.SchemaError(
            "format:password is not supported at: " + ", ".join(sorted(unsupported))
        )
    jsonschema.validate(instance=payload, schema=config_schema)


def apply_schema_defaults(config_schema: dict, stored_config: dict) -> dict:
    """Return ``stored_config`` plus any schema default for an absent key (D-12).

    Never mutates ``stored_config``: defaults are applied only when a config is
    read, never persisted into the ``plugins.config`` row. An object-typed
    property whose resolved value is a ``dict`` recurses into its nested
    ``properties``; an absent object key is NOT synthesized just to apply nested
    defaults.

    A schema-declared default is deep-copied before being handed out (WR-01):
    a list/dict default object would otherwise be shared, by reference,
    between the manifest's ``config_schema`` and every ``ctx.config`` built
    for the plugin — a plugin mutating its own ``ctx.config`` would then
    permanently corrupt the schema default for every other read/context.
    """
    resolved = dict(stored_config)
    properties = config_schema.get("properties", {}) or {}
    for key, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        if key not in resolved and "default" in spec:
            resolved[key] = copy.deepcopy(spec["default"])
        nested = spec.get("properties")
        value = resolved.get(key)
        if isinstance(nested, dict) and isinstance(value, dict):
            resolved[key] = apply_schema_defaults(spec, value)
    return resolved


__all__ = [
    "secret_field_names",
    "unsupported_secret_field_paths",
    "validate_config_payload",
    "apply_schema_defaults",
]
