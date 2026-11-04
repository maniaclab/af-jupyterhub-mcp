"""Tests for Settings and its JUPYTERHUB_MCP_* environment parsing."""

from __future__ import annotations

import pytest

from af_jupyterhub_mcp.config import Settings

_CPU_LATEST = "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest"
_GPU_LATEST = "hub.opensciencegrid.org/usatlas/ml-platform-gpu:latest"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an environment with no JUPYTERHUB_MCP_* variables."""
    for name in (
        "JUPYTERHUB_MCP_HUB_URL",
        "JUPYTERHUB_MCP_PUBLIC_HUB_URL",
        "JUPYTERHUB_MCP_CPU_IMAGES",
        "JUPYTERHUB_MCP_GPU_IMAGES",
        "JUPYTERHUB_MCP_IMAGE_TOOL_OVERRIDES",
        "JUPYTERHUB_MCP_SITES",
        "JUPYTERHUB_MCP_LAUNCH_TIMEOUT_SECONDS",
        "JUPYTERHUB_MCP_AUTH_CACHE_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_hub_url_defaults_to_the_af_hub(self) -> None:
        assert Settings().hub_url == "https://jupyterhub.af.uchicago.edu"

    def test_image_allowlists_default_to_latest_ml_platform(self) -> None:
        settings = Settings()
        assert settings.cpu_images == (_CPU_LATEST,)
        assert settings.gpu_images == (_GPU_LATEST,)

    def test_sites_default_to_local(self) -> None:
        assert Settings().sites == ("local",)

    def test_timeouts_and_ttls(self) -> None:
        settings = Settings()
        assert settings.launch_timeout_seconds == 900
        assert settings.auth_cache_ttl_seconds == 60

    def test_from_env_with_nothing_set_matches_defaults(self) -> None:
        assert Settings.from_env() == Settings()


class TestFromEnv:
    def test_hub_url_trailing_slash_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JUPYTERHUB_MCP_HUB_URL", "https://hub.example.com/")
        assert Settings.from_env().hub_url == "https://hub.example.com"

    def test_image_lists_are_comma_separated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JUPYTERHUB_MCP_CPU_IMAGES", "a:1, b:2 ,")
        monkeypatch.setenv("JUPYTERHUB_MCP_GPU_IMAGES", "g:1")
        settings = Settings.from_env()
        assert settings.cpu_images == ("a:1", "b:2")
        assert settings.gpu_images == ("g:1",)

    def test_sites_are_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JUPYTERHUB_MCP_SITES", "local,ssl,nrp")
        assert Settings.from_env().sites == ("local", "ssl", "nrp")

    def test_integer_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JUPYTERHUB_MCP_LAUNCH_TIMEOUT_SECONDS", "120")
        monkeypatch.setenv("JUPYTERHUB_MCP_AUTH_CACHE_TTL_SECONDS", "5")
        settings = Settings.from_env()
        assert settings.launch_timeout_seconds == 120
        assert settings.auth_cache_ttl_seconds == 5

    @pytest.mark.parametrize(
        ("name", "raw"),
        [
            pytest.param("JUPYTERHUB_MCP_LAUNCH_TIMEOUT_SECONDS", "soon", id="nan"),
            pytest.param("JUPYTERHUB_MCP_LAUNCH_TIMEOUT_SECONDS", "0", id="zero"),
            pytest.param("JUPYTERHUB_MCP_AUTH_CACHE_TTL_SECONDS", "-1", id="neg"),
        ],
    )
    def test_bad_integer_knob_raises(
        self, monkeypatch: pytest.MonkeyPatch, name: str, raw: str
    ) -> None:
        monkeypatch.setenv(name, raw)
        with pytest.raises(ValueError, match=name):
            Settings.from_env()


class TestPublicHubUrl:
    def test_defaults_to_hub_url(self) -> None:
        assert Settings().public_url == "https://jupyterhub.af.uchicago.edu"
        assert Settings(hub_url="http://proxy-public").public_url == (
            "http://proxy-public"
        )

    def test_explicit_public_url_wins_for_links(self) -> None:
        settings = Settings(
            hub_url="http://proxy-public.binderhub.svc.cluster.local",
            public_hub_url="https://jupyterhub.af.uchicago.edu",
        )
        assert settings.public_url == "https://jupyterhub.af.uchicago.edu"
        assert settings.hub_url == "http://proxy-public.binderhub.svc.cluster.local"

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "JUPYTERHUB_MCP_HUB_URL", "http://proxy-public.binderhub.svc.cluster.local"
        )
        monkeypatch.setenv(
            "JUPYTERHUB_MCP_PUBLIC_HUB_URL", "https://jupyterhub.af.uchicago.edu/"
        )
        settings = Settings.from_env()
        assert settings.hub_url == "http://proxy-public.binderhub.svc.cluster.local"
        assert settings.public_url == "https://jupyterhub.af.uchicago.edu"


class TestAllImages:
    def test_union_is_deduplicated_and_ordered(self) -> None:
        settings = Settings(cpu_images=("a", "b"), gpu_images=("b", "c"))
        assert settings.all_images == ("a", "b", "c")


class TestImageToolOverrides:
    _ENV = "JUPYTERHUB_MCP_IMAGE_TOOL_OVERRIDES"

    def test_defaults_to_no_overrides(self) -> None:
        """No overrides means every image is assumed to support every tool."""
        assert dict(Settings().image_tool_overrides) == {}

    def test_absent_from_env_gives_no_overrides(self) -> None:
        assert dict(Settings.from_env().image_tool_overrides) == {}

    def test_parses_json_object_into_frozensets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            self._ENV,
            '{"img:old": ["read_cell", "notebook_run-all-cells"], "img:ancient": []}',
        )
        overrides = Settings.from_env().image_tool_overrides
        assert overrides == {
            "img:old": frozenset({"read_cell", "notebook_run-all-cells"}),
            "img:ancient": frozenset(),
        }

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("not json", id="bad-json"),
            pytest.param('["read_cell"]', id="not-an-object"),
            pytest.param('{"img:old": "read_cell"}', id="value-not-a-list"),
            pytest.param('{"img:old": ["read_cell", 3]}', id="non-string-id"),
        ],
    )
    def test_malformed_value_raises(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(self._ENV, raw)
        with pytest.raises(ValueError, match=self._ENV):
            Settings.from_env()
