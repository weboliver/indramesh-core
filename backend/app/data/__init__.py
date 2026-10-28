import json
import os

_STARTER_TEMPLATES_PATH = os.path.join(os.path.dirname(__file__), "starter_templates.json")
_UI_STRINGS_PATH = os.path.join(os.path.dirname(__file__), "ui_strings.json")


def load_starter_templates() -> list[dict]:
    with open(_STARTER_TEMPLATES_PATH, "r") as f:
        return json.load(f)


def load_ui_strings() -> list[dict]:
    with open(_UI_STRINGS_PATH, "r") as f:
        return json.load(f)


def load_ui_translations_locale(code: str) -> dict:
    """Load a committed backend-local per-locale systemui translation catalog
    (flat {key: value} dict, dotted keys). Mirrors load_ui_strings's exact
    open/json.load shape. An uncommitted/unknown locale code simply has no
    matching file — return {} (never an error), so
    seed_ui_translations_locales() can skip it silently."""
    path = os.path.join(os.path.dirname(__file__), f"ui_translations_{code}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)
