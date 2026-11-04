"""Tests for the af_jupyterhub_mcp package metadata."""

from __future__ import annotations

import af_jupyterhub_mcp


def test_version_is_set() -> None:
    assert af_jupyterhub_mcp.__version__
