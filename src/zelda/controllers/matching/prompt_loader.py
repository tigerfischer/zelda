"""Jinja2 template loader for matching prompts.

Templates live in `src/zelda/prompts/matching/`. This module resolves
that path relative to the package root so it works regardless of the
working directory the CLI is invoked from.

Usage:
    from zelda.controllers.matching.prompt_loader import render_prompt

    text = render_prompt("matching/proposer.j2", row_a=row_a, row_b=row_b, ...)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Resolve the prompts directory relative to this file so the loader
# works from any working directory.
_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    trim_blocks=True,      # strip newline after block tags
    lstrip_blocks=True,    # strip leading whitespace before block tags
    undefined=StrictUndefined,  # raise on missing variables — catch bugs early
    keep_trailing_newline=True,
)


def render_prompt(template_name: str, **kwargs: Any) -> str:
    """Render a prompt template by name with the given variables.

    Args:
        template_name: path relative to the prompts directory,
                       e.g. "matching/proposer.j2"
        **kwargs: template variables

    Raises:
        jinja2.TemplateNotFound: if the template file doesn't exist
        jinja2.UndefinedError: if a variable referenced in the template
                               is not provided
    """
    return _env.get_template(template_name).render(**kwargs)


__all__ = ["render_prompt"]
