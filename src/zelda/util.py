"""Small shared utilities used across controllers and gateways."""

import re


def slugify(s: str) -> str:
    """Lowercase a string and replace runs of unsafe characters with a
    single dash. Suitable for path segments and Drive folder names.

    Falls back to "unknown" if the result is empty.
    """
    s = s.strip().lower()
    s = re.sub(r"[^\w-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unknown"
