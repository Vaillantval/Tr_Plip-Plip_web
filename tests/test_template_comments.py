"""Un commentaire {# ... #} ne vaut que sur une ligne : sur deux, Django
l'affiche tel quel dans la page. Les commentaires longs passent par
{% comment %}."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPEN_COMMENT = re.compile(r"\{#(?!.*#\})")


def _templates():
    for base in (ROOT / "apps", ROOT / "templates"):
        if base.exists():
            yield from base.rglob("*.html")


def test_no_multiline_hash_comment_in_templates():
    offenders = []
    for path in _templates():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OPEN_COMMENT.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == [], "Commentaire {# #} sur plusieurs lignes : " + ", ".join(offenders)
