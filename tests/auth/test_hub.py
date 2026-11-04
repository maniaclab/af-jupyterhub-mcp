"""Tests for HubTokenVerifier, scope checks, and get_hub_identity."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

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
from tests.fakes import ALICE_TOKEN, HUB_URL, FakeHub

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
async def hub(fake_hub: FakeHub) -> AsyncIterator[HubClient]:
    client = HubClient(HUB_URL, transport=fake_hub.transport())
    yield client
    await client.aclose()


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

    async def test_hub_failure_is_rejected_and_logged_without_token(
        self,
        verifier: HubTokenVerifier,
        fake_hub: FakeHub,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake_hub.whoami_status = 500
        with caplog.at_level(logging.WARNING, logger="af_jupyterhub_mcp.auth.hub"):
            assert await verifier.verify_token(ALICE_TOKEN) is None
        assert "500" in caplog.text
        assert ALICE_TOKEN not in caplog.text

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
        fake_hub.whoami_status = 500
        assert await verifier.verify_token(ALICE_TOKEN) is None
        fake_hub.whoami_status = None
        assert await verifier.verify_token(ALICE_TOKEN) is not None

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
