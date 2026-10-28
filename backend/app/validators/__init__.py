"""Input-side HTML sanitization via Pydantic validators.

Replaces the output-side HTMLSanitizationMiddleware. Escaping happens at
ingestion time (POST/PATCH), eliminating the double-escaping bug where
both middleware AND React JSX would escape the same text.
"""

from html import escape as _html_escape
import re

import bleach
from pydantic import BeforeValidator
from typing import Annotated


def _escape_str(value: object) -> object:
    """Apply html.escape() to a string value. Used as BeforeValidator so
    Field(max_length=N) is enforced on the escaped output, not the raw
    input — html.escape() can expand length up to 6x (e.g. ' -> &#x27;),
    which would otherwise let oversized-after-escape strings slip past
    max_length and hit the DB column's String(N) limit.

    Skips escaping if the value already contains HTML entities — this
    prevents double-escaping when response models are constructed from
    database records that were already escaped at input time.

    Non-str values are returned unchanged so Pydantic's own type
    validation (which runs after this BeforeValidator) reports a clean
    "Input should be a valid string" error instead of crashing here.
    """
    if not isinstance(value, str):
        return value
    # If the value already contains common HTML entities, assume it was
    # already escaped (either at input time or via pre-existing data).
    if '&lt;' in value or '&gt;' in value or '&amp;' in value or '&quot;' in value or '&#' in value:
        return value
    return _html_escape(value)


# EscapedStr: apply html.escape() on input. Use for all string fields
# that are NOT user-authored rich text via textarea.
EscapedStr = Annotated[str, BeforeValidator(_escape_str)]

# PlainStr: no escaping. Use for description fields and value_simple
# where the frontend uses <textarea> and HTML input is intentional.
PlainStr = Annotated[str, ...]


# D-08 (Reversibility: costly): preserve this write-time layer alongside the
# frontend DOMPurify render-time layer; either layer alone is a security regression.
# If URI-bearing attributes such as href or src are allowed later, configure
# bleach's protocols= allow-list at the same time to reject unsafe URI schemes.
TEXTAREA_HTML_ALLOWED_TAGS = ["p", "br", "b", "strong", "u", "h1", "h2", "h3"]


def sanitize_textarea_html(raw_html: str) -> str:
    # bleach strips disallowed tags but retains their text content. Script
    # bodies have no display value, so remove complete script elements first.
    without_script_elements = re.sub(
        r"(?is)<script\b[^>]*>.*?</script\s*>", "", raw_html
    )
    return bleach.clean(
        without_script_elements,
        tags=TEXTAREA_HTML_ALLOWED_TAGS,
        attributes={},
        strip=True,
        strip_comments=True,
    )
