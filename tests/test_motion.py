"""Mouvement sur le site client : jamais d'animation decorative sans fin.

WCAG 2.2.2 (niveau A) : un mouvement automatique de plus de 5 s doit
pouvoir etre arrete. Les animations d'accroche jouent donc trois fois au
plus ; seule la roue d'attente d'un envoi (.is-loading) tourne sans fin.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Customer
from apps.providers.plopplop.client import PaymentIntent
from apps.transactions import services

CSS = Path(__file__).resolve().parent.parent / "apps" / "web" / "static" / "web" / "app.css"
PHONE = "50937123456"


def _rules(css: str):
    """(selecteur, corps) des regles simples, @media compris a plat."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        yield selector.strip(), body


def test_only_the_loading_spinner_loops_forever():
    endless = [sel for sel, body in _rules(CSS.read_text(encoding="utf-8")) if re.search(r"animation[^;]*\binfinite\b", body)]
    assert endless == ["button.is-loading::before"]


@pytest.mark.django_db
def test_pay_button_halo_is_not_restarted_by_polling(client):
    cache.clear()
    client.cookies["django_language"] = "fr"
    with mock.patch("apps.accounts.otp.generate_code", return_value="123456"):
        client.post(reverse("web:login"), {"phone": PHONE})
    client.post(reverse("web:login_code"), {"code": "123456"})
    customer = Customer.objects.get(phone=PHONE)
    from apps.api import services as api_services

    with mock.patch("apps.transactions.services.get_client") as get_client:
        get_client.return_value.create_payment.return_value = PaymentIntent(
            transaction_id="PAY-1", reference="PP", redirect_url="https://pay.example/abc"
        )
        quote = services.quote_transfer(source_wallet="moncash", destination_wallet="natcash", net_amount=Decimal("1000"))
        txn = api_services.create_transfer(
            customer=customer, idempotency_key="cle-motion", source_wallet="moncash", destination_wallet="natcash",
            recipient_phone="50932123456", net_amount=Decimal("1000"), expected_total=quote.total_charged,
        ).transaction

    page = client.get(reverse("web:transfer_detail", args=[txn.reference])).content.decode()
    polled = client.get(reverse("web:transfer_status", args=[txn.reference])).content.decode()

    assert 'class="button block cta"' in page
    assert 'href="https://pay.example/abc"' in polled and " cta" not in polled
