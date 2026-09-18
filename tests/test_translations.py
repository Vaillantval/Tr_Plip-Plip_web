"""L'extraction des catalogues est-elle a jour ?

Les garde-fous de traduction (test_web, test_claims, test_notifications)
verifient que chaque entree des catalogues .po est traduite. Ils ne
peuvent rien dire d'une chaine ajoutee au CODE et jamais extraite : elle
n'est dans aucun catalogue, donc invisible pour eux.

Le trou est reel et il a ete constate : une chaine du chemin de l'argent
a traverse une suite entierement verte avant que makemessages ne soit
lance. La regle « aucune chaine du chemin de l'argent ne peut attendre sa
traduction creole » ne garantissait donc pas ce qu'elle pretend.

Ce test rejoue l'extraction de Django -- templatize() pour les gabarits,
puis xgettext -- et compare le resultat aux catalogues. Il n'essaie pas
de deviner les chaines avec une expression reguliere : blocktranslate,
les concatenations implicites et les contextes lui echapperaient, et un
garde-fou approximatif sur le chemin de l'argent ne vaut rien.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from django.utils.translation.template import templatize

ROOT = Path(__file__).resolve().parent.parent

#: Apps qui portent un catalogue. Une app qui en gagne un doit entrer ici,
#: sinon ses chaines ne sont surveillees par rien.
CATALOGUES = ("web", "claims", "notifications")

#: Memes mots-cles que makemessages, pour que l'extraction voie ce que
#: Django voit.
KEYWORDS = (
    "--keyword=gettext_noop",
    "--keyword=gettext_lazy",
    "--keyword=ngettext_lazy:1,2",
    "--keyword=pgettext:1c,2",
    "--keyword=npgettext:1c,2,3",
    "--keyword=pgettext_lazy:1c,2",
    "--keyword=npgettext_lazy:1c,2,3",
)


def _msgids(po_text: str) -> set[str]:
    """msgid actifs d'un catalogue ou d'un POT. Les entrees obsoletes
    (« #~ ») et l'en-tete (msgid vide) sont ignorees."""
    found = set()
    for block in re.split(r"\n\s*\n", po_text):
        parts, current, obsolete = {"msgid": [], "msgstr": []}, None, False
        for line in block.splitlines():
            if line.startswith("#~"):
                obsolete = True
            elif line.startswith(("msgid ", "msgstr ")):
                current, _, rest = line.partition(" ")
                parts[current].append(ast.literal_eval(rest))
            elif line.startswith('"') and current:
                parts[current].append(ast.literal_eval(line))
        msgid = "".join(parts["msgid"])
        if msgid and not obsolete:
            found.add(msgid)
    return found


def _extract(app: str) -> set[str]:
    """Chaines traduisibles presentes dans le CODE de l'app.

    Les gabarits passent par templatize(), qui est exactement ce
    qu'emploie makemessages : blocktranslate y devient un appel gettext
    ordinaire, avec sa normalisation d'espaces.
    """
    if shutil.which("xgettext") is None:  # pragma: no cover
        pytest.skip("xgettext absent : outils gettext non installes")

    source = ROOT / "apps" / app
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sources = []
        for path in sorted(source.rglob("*")):
            if path.suffix not in (".py", ".html") or "locale" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".html":
                text = templatize(text, origin=str(path))
            target = tmp / (str(path.relative_to(source)).replace("\\", "_").replace("/", "_") + ".py")
            target.write_text(text, encoding="utf-8")
            sources.append(target.name)

        result = subprocess.run(
            ["xgettext", "--language=Python", "--from-code=UTF-8", *KEYWORDS, "--output=-", *sources],
            cwd=tmp,
            capture_output=True,
        )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return _msgids(result.stdout.decode("utf-8"))


@pytest.mark.parametrize("app", CATALOGUES)
def test_every_translatable_string_in_the_code_is_in_the_catalogue(app):
    """Une chaine ajoutee au code sans regenerer les catalogues echoue ICI.

    Sans ce test, elle passerait inapercue : les garde-fous de traduction
    ne verifient que ce qui est deja dans les .po. Sur le chemin de
    l'argent, cela veut dire une chaine rendue en francais a un client qui
    ne lit que le creole.
    """
    catalogue = _msgids((ROOT / "apps" / app / "locale" / "ht" / "LC_MESSAGES" / "django.po").read_text("utf-8"))

    missing = sorted(_extract(app) - catalogue)

    assert missing == [], (
        f"{len(missing)} chaine(s) de apps/{app} absente(s) des catalogues. "
        "Relancer makemessages, effacer les entrees devinees (fuzzy), puis compilemessages."
    )


@pytest.mark.parametrize("app", CATALOGUES)
def test_the_two_catalogues_of_an_app_hold_the_same_strings(app):
    """Anglais et creole doivent porter les memes entrees : sinon l'un des
    deux a ete regenere sans l'autre, et le garde-fou creole ne voit plus
    la moitie du travail."""
    locale = ROOT / "apps" / app / "locale"
    english = _msgids((locale / "en" / "LC_MESSAGES" / "django.po").read_text("utf-8"))
    creole = _msgids((locale / "ht" / "LC_MESSAGES" / "django.po").read_text("utf-8"))

    assert sorted(english ^ creole) == []
