"""Tests for server naming, request validation, user_options, and summaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from af_jupyterhub_mcp.config import Settings
from af_jupyterhub_mcp.hub.errors import (
    GuardrailError,
    HubAPIError,
    ImageNotAllowedError,
    SpawnFailedError,
    SpawnTimeoutError,
)
from af_jupyterhub_mcp.hub.servers import (
    build_user_options,
    check_server_name,
    find_server,
    follow_spawn,
    generate_server_name,
    server_status,
    summarize_server,
    validate_create_request,
    validate_new_server_name,
)
from tests.fakes import ALICE_TOKEN, HUB_URL, FakeHub, server_model

if TYPE_CHECKING:
    from af_jupyterhub_mcp.hub.client import HubClient

_CPU = "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest"
_GPU = "hub.opensciencegrid.org/usatlas/ml-platform-gpu:latest"


@pytest.fixture
def settings() -> Settings:
    return Settings(cpu_images=(_CPU,), gpu_images=(_GPU,), sites=("local", "ssl"))


class TestServerNames:
    @pytest.mark.parametrize("name", ["notebook-1", "a", "my-analysis-2", "x" * 63])
    def test_valid_new_names(self, name: str) -> None:
        validate_new_server_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "-lead",
            "trail-",
            "Upper",
            "has space",
            "sl/ash",
            "under_score",
            "x" * 64,
        ],
    )
    def test_invalid_new_names(self, name: str) -> None:
        with pytest.raises(GuardrailError, match="server name"):
            validate_new_server_name(name)

    @pytest.mark.parametrize("name", ["maniaclab-ml_platform-ab12cd34", "Mixed.Case"])
    def test_lookup_accepts_names_other_clients_created(self, name: str) -> None:
        check_server_name(name)

    @pytest.mark.parametrize("name", ["", "a/b"])
    def test_lookup_rejects_names_the_hub_cannot_address(self, name: str) -> None:
        with pytest.raises(GuardrailError, match="server name"):
            check_server_name(name)

    def test_generate_picks_the_first_free_slot(self) -> None:
        assert generate_server_name({"notebook-1", "notebook-3"}) == "notebook-2"

    def test_generate_starts_at_one(self) -> None:
        assert generate_server_name(set()) == "notebook-1"

    def test_generate_gives_up_when_every_slot_is_taken(self) -> None:
        taken = {f"notebook-{i}" for i in range(1, 200)}
        assert generate_server_name(taken) is None


class TestValidateCreateRequest:
    def _validate(self, settings: Settings, **overrides: Any) -> None:
        kwargs: dict[str, Any] = {
            "image": _CPU,
            "site": "local",
            "cpu_cores": 1,
            "memory_gb": 1,
            "gpus": 0,
            "gpu_product": None,
        }
        kwargs.update(overrides)
        validate_create_request(settings=settings, **kwargs)

    def test_defaults_are_valid(self, settings: Settings) -> None:
        self._validate(settings)

    def test_gpu_request_with_gpu_image_is_valid(self, settings: Settings) -> None:
        self._validate(settings, image=_GPU, gpus=1, gpu_product="NVIDIA-A100")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("cpu_cores", 0),
            ("cpu_cores", 17),
            ("memory_gb", 0),
            ("memory_gb", 257),
            ("gpus", -1),
        ],
    )
    def test_out_of_range_values(
        self, settings: Settings, field: str, value: int
    ) -> None:
        with pytest.raises(GuardrailError, match=field):
            self._validate(settings, **{field: value})

    def test_gpu_product_without_gpus(self, settings: Settings) -> None:
        with pytest.raises(GuardrailError, match="gpu_product"):
            self._validate(settings, gpu_product="NVIDIA-A100")

    def test_unknown_site(self, settings: Settings) -> None:
        with pytest.raises(GuardrailError, match="site 'mars'"):
            self._validate(settings, site="mars")

    def test_image_not_on_any_list(self, settings: Settings) -> None:
        with pytest.raises(ImageNotAllowedError, match="list_supported_images"):
            self._validate(settings, image="docker.io/evil:1")

    def test_cpu_image_with_gpus_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(ImageNotAllowedError, match="GPU image"):
            self._validate(settings, gpus=1)

    def test_gpu_image_without_gpus_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(ImageNotAllowedError, match="CPU image"):
            self._validate(settings, image=_GPU)


class TestBuildUserOptions:
    def test_cpu_request_matches_the_spawner_form(self) -> None:
        options = build_user_options(
            image=_CPU,
            site="local",
            cpu_cores=2,
            memory_gb=8,
            guaranteed_qos=False,
            gpus=0,
            gpu_product=None,
        )
        assert options == {
            "image": _CPU,
            "resource_requests": {
                "site": "local",
                "cpu": "2",
                "memory": "8",
                "qos": "Burstable",
                "gpuCount": "0",
            },
        }

    def test_guaranteed_gpu_request_names_the_product(self) -> None:
        options = build_user_options(
            image=_GPU,
            site="ssl",
            cpu_cores=4,
            memory_gb=16,
            guaranteed_qos=True,
            gpus=2,
            gpu_product="NVIDIA-A100",
        )
        assert options["resource_requests"] == {
            "site": "ssl",
            "cpu": "4",
            "memory": "16",
            "qos": "Guaranteed",
            "gpuCount": "2",
            "gpuModel": "NVIDIA-A100",
        }


class TestSummaries:
    def test_status(self) -> None:
        assert server_status(server_model("a", "s")) == "ready"
        assert (
            server_status(server_model("a", "s", ready=False, pending="spawn"))
            == "pending spawn"
        )
        assert server_status(server_model("a", "s", ready=False)) == "stopped"

    def test_summary_of_a_server_this_backend_launched(self) -> None:
        options = build_user_options(
            image=_GPU,
            site="ssl",
            cpu_cores=4,
            memory_gb=16,
            guaranteed_qos=True,
            gpus=1,
            gpu_product="NVIDIA-A100",
        )
        summary = summarize_server(
            server_model("alice", "nb-1", user_options=options), HUB_URL
        )
        assert summary == {
            "name": "nb-1",
            "status": "ready",
            "image": _GPU,
            "site": "ssl",
            "cpu": "4",
            "memory_gb": "16",
            "qos": "Guaranteed",
            "gpus": "1",
            "gpu_product": "NVIDIA-A100",
            "url": f"{HUB_URL}/user/alice/nb-1/",
            "started": "2026-11-04T00:00:00.000000Z",
            "last_activity": "2026-11-04T01:00:00.000000Z",
        }

    def test_binderhub_ui_server_never_leaks_its_token(self) -> None:
        options = {
            "token": "SECRET-binder-token",
            "image": "harbor.af.uchicago.edu/binderhub/ml-platform:abc",
            "binder_request": "v2/gh/maniaclab/ml_platform/main",
            "binder_ref_url": "https://github.com/maniaclab/ml_platform/tree/abc",
            "resource_requests": {"site": "UC AF", "cpu": "1", "memory": "1"},
        }
        summary = summarize_server(
            server_model("alice", "maniaclab-ml_platform-x", user_options=options),
            HUB_URL,
        )
        assert "SECRET-binder-token" not in repr(summary)
        assert summary["binder_request"] == "v2/gh/maniaclab/ml_platform/main"
        assert summary["image"] == options["image"]
        assert summary["site"] == "UC AF"

    def test_summary_of_a_server_with_no_user_options(self) -> None:
        model = server_model("alice", "bare")
        model["user_options"] = None
        summary = summarize_server(model, HUB_URL)
        assert summary["image"] is None
        assert summary["cpu"] is None

    def test_find_server(self) -> None:
        nb1 = server_model("alice", "nb-1")
        user: dict[str, Any] = {"name": "alice", "servers": {"nb-1": nb1}}
        assert find_server(user, "nb-1") is nb1
        assert find_server(user, "nb-2") is None
        assert find_server({"name": "alice"}, "nb-1") is None


class TestFollowSpawn:
    async def test_reports_every_event_and_returns_ready(self, hub: HubClient) -> None:
        await hub.start_server(ALICE_TOKEN, "alice", "nb-1", {})
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        ready = await follow_spawn(
            hub, ALICE_TOKEN, "alice", "nb-1", timeout=5, on_event=on_event
        )
        assert ready["ready"] is True
        assert [e["progress"] for e in seen] == [10, 50, 100]

    async def test_failed_event_without_message_still_fails(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.progress_events = [{"progress": 100, "failed": True}]
        await hub.start_server(ALICE_TOKEN, "alice", "nb-1", {})
        with pytest.raises(SpawnFailedError, match="Spawn failed"):
            await follow_spawn(hub, ALICE_TOKEN, "alice", "nb-1", timeout=5)

    async def test_failed_event_raises_with_the_hub_message(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.progress_events = [
            {"progress": 100, "failed": True, "message": "Spawn failed: no nodes"}
        ]
        await hub.start_server(ALICE_TOKEN, "alice", "nb-1", {})
        with pytest.raises(SpawnFailedError, match="no nodes"):
            await follow_spawn(hub, ALICE_TOKEN, "alice", "nb-1", timeout=5)

    async def test_stream_ending_without_ready_is_a_failure(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.progress_ends_early = True
        await hub.start_server(ALICE_TOKEN, "alice", "nb-1", {})
        with pytest.raises(SpawnFailedError, match="failed to start"):
            await follow_spawn(hub, ALICE_TOKEN, "alice", "nb-1", timeout=5)

    async def test_timeout_raises_and_leaves_the_server_pending(
        self, hub: HubClient, fake_hub: FakeHub
    ) -> None:
        fake_hub.progress_hangs = True
        await hub.start_server(ALICE_TOKEN, "alice", "nb-1", {})
        with pytest.raises(SpawnTimeoutError, match=r"0\.2 seconds"):
            await follow_spawn(hub, ALICE_TOKEN, "alice", "nb-1", timeout=0.2)
        assert fake_hub.servers["alice"]["nb-1"]["pending"] == "spawn"

    async def test_hub_error_propagates(self, hub: HubClient) -> None:
        with pytest.raises(HubAPIError):
            await follow_spawn(hub, ALICE_TOKEN, "alice", "missing", timeout=5)
