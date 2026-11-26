"""Adresse IP du client : jamais le premier saut de X-Forwarded-For."""

from __future__ import annotations

from django.test import RequestFactory, override_settings
from django.conf import settings

from apps.console.permissions import client_ip as console_ip
from apps.web.ratelimit import client_ip as web_ip

SPOOFED = "6.6.6.6, 10.0.0.9"


def _request(forwarded=SPOOFED, remote="10.0.0.1"):
    return RequestFactory().get("/", HTTP_X_FORWARDED_FOR=forwarded, REMOTE_ADDR=remote)


def _proxies(n):
    return override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": n})


def test_without_declared_proxy_the_header_is_ignored_everywhere():
    with _proxies(0):
        assert console_ip(_request()) == "10.0.0.1"
        assert web_ip(_request()) == "10.0.0.1"


def test_behind_one_proxy_the_hop_added_by_the_proxy_is_used_not_the_spoofed_one():
    with _proxies(1):
        assert console_ip(_request()) == "10.0.0.9"
        assert web_ip(_request()) == "10.0.0.9"
