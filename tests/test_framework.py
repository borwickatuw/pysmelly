"""Tests for content-based Django module recognition in checks/framework.py."""

from __future__ import annotations

import ast

import pytest

from pysmelly.checks.framework import is_django_settings_module, is_django_urlconf

URLCONF_SOURCES = {
    "plain": "urlpatterns = [path('', view)]\n",
    "annotated": "urlpatterns: list = []\n",
    "extended under DEBUG": "if DEBUG:\n    urlpatterns += static()\n",
    "inside try": "try:\n    urlpatterns = []\nexcept ImportError:\n    pass\n",
}

NOT_URLCONF_SOURCES = {
    "no assignment": "from django.urls import path\n",
    "only read": "print(urlpatterns)\n",
    "function local": "def build():\n    urlpatterns = []\n    return urlpatterns\n",
    "class attribute": "class Router:\n    urlpatterns = []\n",
    "attribute target": "module.urlpatterns = []\n",
}


@pytest.mark.parametrize("source", URLCONF_SOURCES.values(), ids=URLCONF_SOURCES.keys())
def test_urlconf_recognised(source):
    assert is_django_urlconf(ast.parse(source))


@pytest.mark.parametrize("source", NOT_URLCONF_SOURCES.values(), ids=NOT_URLCONF_SOURCES.keys())
def test_not_urlconf(source):
    assert not is_django_urlconf(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "INSTALLED_APPS = ['django.contrib.admin']\n",
        "from .base import *\nINSTALLED_APPS += ['debug_toolbar']\n",
    ],
    ids=["base settings", "split settings extending base"],
)
def test_settings_module_recognised(source):
    assert is_django_settings_module(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "DEBUG = True\nSECRET_KEY = 'x'\n",
        "from django.conf import settings\nprint(settings.INSTALLED_APPS)\n",
        "def apps():\n    INSTALLED_APPS = []\n    return INSTALLED_APPS\n",
    ],
    ids=["settings without apps", "reads settings", "function local"],
)
def test_not_settings_module(source):
    assert not is_django_settings_module(ast.parse(source))
