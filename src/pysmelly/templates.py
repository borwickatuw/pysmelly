"""What a template reads, by a static scan of its text.

Django and Jinja templates read an object's attributes with a dot:
``{{ cut.unread_pages }}``, ``{% if floor.surrogate %}``,
``{% for page in cut.pages %}``. Both engines try the name as a mapping key
too, so a dot also reads a dict's (or a TypedDict's) key. Python code never
names those reads, so a check that asks what production code reads of a
record has to look at the templates as well.

The scan is lexical and never renders, imports or executes anything. Inside
each ``{{ ... }}`` and ``{% ... %}`` block, quoted strings are blanked (so
``{% static "css/app.css" %}`` reads no ``css``), and every identifier that
follows a dot after a name, a ``)`` or a ``]`` is an attribute read.
Comments (``{# ... #}``) are not blocks. It over-reads rather than
under-reads (a ``{% comment %}`` block's dotted names count), which can only
keep a field alive, never report one.
"""

from __future__ import annotations

import re

_BLOCK = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.DOTALL)
_STRING = re.compile(r"\"[^\"]*\"|'[^']*'")
_DOTTED = re.compile(r"(?<=[\w)\]])\.([A-Za-z_]\w*)")


def attribute_names(text: str) -> set[str]:
    """Every name a template's text reads after a dot (``{{ x.name }}``)."""
    names: set[str] = set()
    for block in _BLOCK.finditer(text):
        code = _STRING.sub('""', block.group(1) or block.group(2))
        names.update(_DOTTED.findall(code))
    return names
