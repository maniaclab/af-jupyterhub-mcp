"""Tests for HubTokenVerifier, scope checks, and get_hub_identity."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import httpx2
import pytest

from af_jupyterhub_mcp.auth.hub import (
    REQUIRED_SCOPES,
    HubIdentity,
    HubTokenVerifier,
    MissingScopesError,
    get_hub_identity,
    missing_scopes,
)
from af_jupyterhub_mcp.hub.client import HubClient
from af_jupyterhub_mcp.hub.errors import HubUnavailableError
from tests.fakes import ALICE_TOKEN, HUB_URL, FakeHub


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def verifier(hub: HubClient, clock: _Clock) -> HubTokenVerifier:
    return HubTokenVerifier(hub, ttl_seconds=60, clock=clock)


def _whoami_count(fake_hub: FakeHub) -> int:
    return sum(r.url.path == "/hub/api/user" for r in fake_hub.requests)


class TestRequiredScopes:
    def test_required_scopes(self) -> None:
        assert REQUIRED_SCOPES == (
            "servers!user",
            "access:servers!user",
            "read:users:name!user",
        )


class TestMissingScopes:
    def test_expanded_user_filtered_scopes_satisfy(self) -> None:
        scopes = [
            "servers!user=alice",
            "access:servers!user=alice",
            "read:users:name!user=alice",
        ]
        assert missing_scopes(scopes, "alice") == []

    def test_unfiltered_scopes_satisfy(self) -> None:
        assert (
            missing_scopes(["servers", "access:servers", "read:users:name"], "a") == []
        )

    def test_literal_user_filter_satisfies(self) -> None:
        assert missing_scopes(list(REQUIRED_SCOPES), "alice") == []

    def test_another_users_filter_does_not_satisfy(self) -> None:
        scopes = [
            "servers!user=bob",
            "access:servers!user=alice",
            "read:users:name!user=alice",
        ]
        assert missing_scopes(scopes, "alice") == ["servers!user"]

    def test_server_filtered_access_does_not_satisfy(self) -> None:
        scopes = [
            "servers!user=alice",
            "access:servers!server=alice/nb-1",
            "read:users:name!user=alice",
        ]
        assert missing_scopes(scopes, "alice") == ["access:servers!user"]

    def test_reports_every_missing_scope_in_order(self) -> None:
        assert missing_scopes([], "alice") == list(REQUIRED_SCOPES)


class TestVerifyToken:
    async def test_valid_token_maps_to_hub_username(
        self, verifier: HubTokenVerifier
    ) -> None:
        access = await verifier.verify_token(ALICE_TOKEN)
        assert access is not None
        assert access.subject == "alice"
        assert access.client_id == "jupyterhub"
        assert "servers!user=alice" in access.scopes

    async def test_unknown_token_is_rejected(self, verifier: HubTokenVerifier) -> None:
        assert await verifier.verify_token("not-a-token") is None

    @pytest.mark.parametrize("status", [401, 403])
    async def test_hub_refusal_is_rejected_and_logged_without_token(
        self,
        verifier: HubTokenVerifier,
        fake_hub: FakeHub,
        caplog: pytest.LogCaptureFixture,
        status: int,
    ) -> None:
        fake_hub.fail_paths["/hub/api/user"] = status
        with caplog.at_level(logging.WARNING, logger="af_jupyterhub_mcp.auth.hub"):
            assert await verifier.verify_token(ALICE_TOKEN) is None
        assert str(status) in caplog.text
        assert ALICE_TOKEN not in caplog.text

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_hub_server_error_raises_instead_of_rejecting(
        self, verifier: HubTokenVerifier, fake_hub: FakeHub, status: int
    ) -> None:
        # Rejecting here would make the transport answer 401, and the broker
        # would mark a still-valid, non-refreshable hub token as rejected.
        fake_hub.fail_paths["/hub/api/user"] = status
        with pytest.raises(HubUnavailableError, match=str(status)) as excinfo:
            await verifier.verify_token(ALICE_TOKEN)
        assert ALICE_TOKEN not in str(excinfo.value)

    async def test_unreachable_hub_raises_instead_of_rejecting(self) -> None:
        def refuse(request: httpx2.Request) -> httpx2.Response:
            msg = "connection refused"
            raise httpx2.ConnectError(msg, request=request)

        hub = HubClient(HUB_URL, transport=httpx2.MockTransport(refuse))
        try:
            with pytest.raises(HubUnavailableError, match="could not reach"):
                await HubTokenVerifier(hub, ttl_seconds=60).verify_token(ALICE_TOKEN)
        finally:
            await hub.aclose()

    async def test_malformed_hub_answer_raises_instead_of_rejecting(self) -> None:
        def garbled(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, text="<html>proxy error page</html>")

        hub = HubClient(HUB_URL, transport=httpx2.MockTransport(garbled))
        try:
            with pytest.raises(HubUnavailableError, match="malformed"):
                await HubTokenVerifier(hub, ttl_seconds=60).verify_token(ALICE_TOKEN)
        finally:
            await hub.aclose()

    async def test_result_is_cached_within_ttl(
        self, verifier: HubTokenVerifier, fake_hub: FakeHub, clock: _Clock
    ) -> None:
        await verifier.verify_token(ALICE_TOKEN)
        clock.now += 59
        await verifier.verify_token(ALICE_TOKEN)
        assert _whoami_count(fake_hub) == 1

    async def test_cache_expires_after_ttl(
        self, verifier: HubTokenVerifier, fake_hub: FakeHub, clock: _Clock
    ) -> None:
        await verifier.verify_token(ALICE_TOKEN)
        clock.now += 61
        await verifier.verify_token(ALICE_TOKEN)
        assert _whoami_count(fake_hub) == 2

    async def test_rejections_are_not_cached(
        self, verifier: HubTokenVerifier, fake_hub: FakeHub
    ) -> None:
        fake_hub.fail_paths["/hub/api/user"] = 401
        assert await verifier.verify_token(ALICE_TOKEN) is None
        fake_hub.fail_paths.clear()
        assert await verifier.verify_token(ALICE_TOKEN) is not None

    async def test_outages_are_not_cached(
        self, verifier: HubTokenVerifier, fake_hub: FakeHub
    ) -> None:
        fake_hub.fail_paths["/hub/api/user"] = 503
        with pytest.raises(HubUnavailableError):
            await verifier.verify_token(ALICE_TOKEN)
        fake_hub.fail_paths.clear()
        assert await verifier.verify_token(ALICE_TOKEN) is not None

    async def test_non_user_token_is_rejected(
        self,
        verifier: HubTokenVerifier,
        fake_hub: FakeHub,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def service_whoami(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/hub/api/user":
                return httpx2.Response(200, json={"kind": "service", "name": "binder"})
            return await fake_hub.handle(request)

        hub = HubClient(HUB_URL, transport=httpx2.MockTransport(service_whoami))
        try:
            service_verifier = HubTokenVerifier(hub, ttl_seconds=60)
            with caplog.at_level(logging.WARNING, logger="af_jupyterhub_mcp.auth.hub"):
                assert await service_verifier.verify_token(ALICE_TOKEN) is None
        finally:
            await hub.aclose()
        assert "does not belong to a user" in caplog.text
        assert verifier._cache == {}

    async def test_cache_never_stores_the_raw_token(
        self, verifier: HubTokenVerifier
    ) -> None:
        await verifier.verify_token(ALICE_TOKEN)
        assert all(ALICE_TOKEN not in key for key in verifier._cache)

    async def test_expired_entries_are_pruned(
        self, verifier: HubTokenVerifier, clock: _Clock
    ) -> None:
        await verifier.verify_token(ALICE_TOKEN)
        clock.now += 120
        await verifier.verify_token("not-a-token")
        assert verifier._cache == {}


def _ctx(verifier: HubTokenVerifier, authorization: str | None) -> MagicMock:
    ctx = MagicMock()
    headers = {} if authorization is None else {"authorization": authorization}
    ctx.request_context.request.headers = headers
    ctx.request_context.lifespan_context = {"verifier": verifier}
    return ctx


class TestGetHubIdentity:
    async def test_returns_name_and_token(self, verifier: HubTokenVerifier) -> None:
        identity = await get_hub_identity(_ctx(verifier, f"Bearer {ALICE_TOKEN}"))
        assert identity == HubIdentity(name="alice", token=ALICE_TOKEN)

    async def test_repr_hides_the_token(self, verifier: HubTokenVerifier) -> None:
        identity = await get_hub_identity(_ctx(verifier, f"Bearer {ALICE_TOKEN}"))
        assert ALICE_TOKEN not in repr(identity)

    @pytest.mark.parametrize(
        "header", [None, "", f"token {ALICE_TOKEN}"], ids=["none", "empty", "scheme"]
    )
    async def test_missing_bearer_is_a_permission_error(
        self, verifier: HubTokenVerifier, header: str | None
    ) -> None:
        with pytest.raises(PermissionError, match="Bearer"):
            await get_hub_identity(_ctx(verifier, header))

    async def test_invalid_token_asks_to_relink(
        self, verifier: HubTokenVerifier
    ) -> None:
        with pytest.raises(PermissionError, match="re-link"):
            await get_hub_identity(_ctx(verifier, "Bearer nope"))

    async def test_missing_scopes_are_named(
        self, verifier: HubTokenVerifier, fake_hub: FakeHub
    ) -> None:
        fake_hub.tokens["narrow"] = ("alice", ["read:users:name!user=alice"])
        with pytest.raises(MissingScopesError) as excinfo:
            await get_hub_identity(_ctx(verifier, "Bearer narrow"))
        message = str(excinfo.value)
        assert "servers!user" in message
        assert "access:servers!user" in message
        assert "re-link" in message
