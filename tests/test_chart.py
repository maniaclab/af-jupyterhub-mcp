"""Render the Helm chart and check the numeric env values the server parses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from af_jupyterhub_mcp.config import Settings

_CHART = Path(__file__).resolve().parent.parent / "charts" / "af-jupyterhub-mcp"

# env var name -> (values.yaml path, Settings attribute)
_NUMERIC_ENV = {
    "JUPYTERHUB_MCP_LAUNCH_TIMEOUT_SECONDS": (
        ("notebook", "launchTimeoutSeconds"),
        "launch_timeout_seconds",
    ),
    "JUPYTERHUB_MCP_IMAGE_OUTPUT_MAX_BYTES": (
        ("notebook", "imageOutput", "maxBytes"),
        "image_output_max_bytes",
    ),
    "JUPYTERHUB_MCP_IMAGE_OUTPUT_MAX_TOTAL_BYTES": (
        ("notebook", "imageOutput", "maxTotalBytes"),
        "image_output_max_total_bytes",
    ),
    "JUPYTERHUB_MCP_AUTH_CACHE_TTL_SECONDS": (
        ("auth", "cacheTtlSeconds"),
        "auth_cache_ttl_seconds",
    ),
}

# Values at or above 1e6 are where float64 formatting switches to exponent notation.
_LARGE = {
    "launchTimeoutSeconds": 2000000,
    "maxBytes": 1048576,
    "maxTotalBytes": 4194304,
    "cacheTtlSeconds": 3000000,
}


def _nested(path: tuple[str, ...], value: object) -> dict[str, object]:
    for key in reversed(path):
        value = {key: value}
    return value  # type: ignore[return-value]


def _merge(into: dict[str, object], other: dict[str, object]) -> None:
    for key, value in other.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)  # type: ignore[arg-type]
        else:
            into[key] = value


def _values(*, as_strings: bool) -> dict[str, object]:
    values: dict[str, object] = {}
    for path, _ in _NUMERIC_ENV.values():
        number = _LARGE[path[-1]]
        _merge(values, _nested(path, str(number) if as_strings else number))
    return values


def _render_env(tmp_path: Path, values: dict[str, object] | None) -> dict[str, str]:
    """Return the server container's env from `helm template`."""
    command = ["helm", "template", "shared", str(_CHART)]
    if values is not None:
        # A values file is parsed as YAML, so integers reach the template as float64,
        # as they do under Flux and `helm install -f`.
        values_file = tmp_path / "values.yaml"
        values_file.write_text(json.dumps(values))
        command += ["-f", str(values_file)]
    rendered = subprocess.run(
        command, check=True, capture_output=True, text=True
    ).stdout
    deployments = [
        doc
        for doc in yaml.safe_load_all(rendered)
        if doc and doc["kind"] == "Deployment"
    ]
    assert len(deployments) == 1
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return {item["name"]: item["value"] for item in containers[0]["env"]}


class TestChartNumericEnv:
    """Numeric env values render as plain decimal integers the server accepts."""

    @pytest.mark.parametrize("variant", ["defaults", "integers", "strings"])
    def test_numeric_env_is_plain_integer(
        self, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        values = {
            "defaults": None,
            "integers": _values(as_strings=False),
            "strings": _values(as_strings=True),
        }[variant]
        env = _render_env(tmp_path, values)

        for name, (path, _) in _NUMERIC_ENV.items():
            assert env[name].isdecimal(), f"{name}={env[name]!r}"
            if values is not None:
                assert env[name] == str(_LARGE[path[-1]])
            monkeypatch.setenv(name, env[name])

        settings = Settings.from_env()
        for name, (_, attribute) in _NUMERIC_ENV.items():
            assert getattr(settings, attribute) == int(env[name])
