"""Tests for the af-jupyterhub-mcp CLI argument wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from af_jupyterhub_mcp.cli import main

if TYPE_CHECKING:
    import pytest


class TestCliServe:
    def test_serve_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JUPYTERHUB_MCP_RESOURCE_URL", raising=False)
        with (
            patch("af_jupyterhub_mcp.cli.serve") as mock_serve,
            patch("sys.argv", ["af-jupyterhub-mcp", "serve"]),
        ):
            main()
        _args, kwargs = mock_serve.call_args
        assert kwargs == {
            "host": "0.0.0.0",
            "port": 8000,
            "resource_url": None,
            "forwarded_allow_ips": "127.0.0.1",
            "log_level": "info",
        }

    def test_resource_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JUPYTERHUB_MCP_RESOURCE_URL", "https://mcp.invalid")
        with (
            patch("af_jupyterhub_mcp.cli.serve") as mock_serve,
            patch("sys.argv", ["af-jupyterhub-mcp", "serve"]),
        ):
            main()
        assert mock_serve.call_args.kwargs["resource_url"] == "https://mcp.invalid"

    def test_serve_forwards_all_overrides(self) -> None:
        with (
            patch("af_jupyterhub_mcp.cli.serve") as mock_serve,
            patch(
                "sys.argv",
                [
                    "af-jupyterhub-mcp",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9000",
                    "--resource-url",
                    "https://af-jupyterhub-mcp.invalid",
                    "--forwarded-allow-ips",
                    "*",
                    "--log-level",
                    "debug",
                ],
            ),
        ):
            main()
        assert mock_serve.call_args.kwargs == {
            "host": "127.0.0.1",
            "port": 9000,
            "resource_url": "https://af-jupyterhub-mcp.invalid",
            "forwarded_allow_ips": "*",
            "log_level": "debug",
        }

    def test_no_command_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("sys.argv", ["af-jupyterhub-mcp"]):
            main()
        assert "serve" in capsys.readouterr().out
