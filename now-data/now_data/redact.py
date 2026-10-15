"""Secret redaction for exported `sys_properties` values.

A port of servicenow-coder's `instance-config/scripts/redact.js`. The two exporters
must agree, so the regexes are kept character-identical to that file. A property's
name, type and description are kept for grounding; a secret-looking VALUE becomes
`[REDACTED]`. Three independent triggers:

1. The `type` is `password` or `password2`. ServiceNow's own signal, checked first,
   because its `{{gpaes}}` encrypted blobs slipped past the shape heuristics.
2. The value starts with a well-known credential prefix, or contains a PEM private
   key header. Unambiguous whatever the name.
3. The NAME looks secret and the value is an opaque token: 16+ characters, no
   whitespace, not purely numeric. No charset allow-list: every one so far had a gap
   a real secret slipped through (coder #265).
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

SECRET_NAME_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_.\-]?key|apikey|credential|private[_.\-]?key|client[_.\-]?secret|keystore)",
    re.IGNORECASE,
)
SECRET_PREFIX_RE = re.compile(r"^(AQ\.|AIza|sk-|ghp_|ghs_|gho_|ya29\.|glpat-|xox[baprs]-)")
# JavaScript's \s, which is also what String.prototype.trim() strips. Python's \s and
# str.strip() differ: they add \x1c-\x1f and drop \ufeff, so a BOM-prefixed token would
# slip past the anchored prefix check here and not in redact.js.
_JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_TRIM_RE = re.compile(f"^[{_JS_WS}]+|[{_JS_WS}]+$")
OPAQUE_TOKEN_RE = re.compile(f"^[^{_JS_WS}]{{16,}}$")
PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_DIGITS_RE = re.compile(r"^\d+$", re.ASCII)  # JS \d is ASCII-only
SECRET_TYPES = frozenset({"password", "password2"})


def redact_secret_value(name: str, value, *types: str):
    """Return `value`, or `[REDACTED]` if it looks like a secret.

    `types` are the property's type as the caller has it (the raw value, the display
    label, or both); any one of them being a password type redacts.
    """
    if not isinstance(value, str) or not value:
        return value
    if any(t in SECRET_TYPES for t in types if t):
        return REDACTED
    # The shape triggers are anchored, so test the trimmed value: a pasted token with a
    # stray space or trailing newline would otherwise slip past them.
    trimmed = _JS_TRIM_RE.sub("", value)
    if SECRET_PREFIX_RE.search(trimmed):
        return REDACTED
    if PEM_PRIVATE_KEY_RE.search(value):
        return REDACTED
    # fullmatch, not search: Python's `$` also matches before a trailing newline.
    if SECRET_NAME_RE.search(name) and OPAQUE_TOKEN_RE.fullmatch(trimmed) and not _DIGITS_RE.fullmatch(trimmed):
        return REDACTED
    return value
