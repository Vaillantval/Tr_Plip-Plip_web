"""Politique de securite du contenu (CSP) du site client.

La CSP n'autorise que les fichiers servis par le site : un seul script ou
gestionnaire ecrit dans un gabarit cesserait de fonctionner en silence.
Ces tests gardent donc les deux bouts : l'en-tete, et des gabarits sans
script, sans gestionnaire on* et sans attribut style.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.web.middleware import CONTENT_SECURITY_POLICY

TEMPLATES = Path(__file__).resolve().parent.parent / "apps" / "web" / "templates"


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["web:home", "web:login"])
def test_client_pages_carry_the_policy(client, name):
    response = client.get(reverse(name))
    policy = response.headers["Content-Security-Policy"]
    assert policy == CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert "frame-ancestors 'none'" in policy and "form-action 'self'" in policy


@pytest.mark.django_db
def test_console_is_not_subject_to_the_client_policy(client):
    response = client.get(reverse("console:login"))
    assert "Content-Security-Policy" not in response.headers


def test_client_templates_have_no_inline_script_handler_or_style():
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for pattern in (r"<script(?![^>]*\bsrc=)[^>]*>", r"\son[a-z]+\s*=", r"\sstyle\s*=", r"\shx-on"):
            for match in re.finditer(pattern, text):
                offenders.append(f"{path.relative_to(TEMPLATES)}: {match.group(0).strip()}")
    assert offenders == []


@pytest.mark.django_db
def test_pages_load_the_shared_script(client):
    html = client.get(reverse("web:home")).content.decode()
    assert "/static/web/app.js" in html
    assert '"allowEval": false' in html
