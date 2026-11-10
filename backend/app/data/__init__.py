import json
import os

_STARTER_TEMPLATES_PATH = os.path.join(os.path.dirname(__file__), "starter_templates.json")
_UI_STRINGS_PATH = os.path.join(os.path.dirname(__file__), "ui_strings.json")

# The single source of truth for "bundled with the application": these language
# codes ship a committed catalog inside this repository and therefore cannot be
# deleted by an administrator. The map is kept as the one maintained list and the
# tuple is derived from it, so codes and display names cannot drift apart.
# Display names are English because they appear identically in every language
# picker (D-06). "it" is listed although its seeded row only arrives with the
# Italian catalogue — the code is bundled either way.
BUNDLED_LANGUAGE_NAMES: dict[str, str] = {
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "pt": "Portuguese",
    "it": "Italian",
}
BUNDLED_LANGUAGE_CODES: tuple[str, ...] = tuple(BUNDLED_LANGUAGE_NAMES)


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
