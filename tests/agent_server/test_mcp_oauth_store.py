from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.client.auth import OAuth
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.settings import ClientRegistrationOptions
from pydantic import SecretStr

import openhands.sdk.mcp.utils as mcp_utils
from openhands.agent_server.config import Config
from openhands.agent_server.mcp_oauth_store import (
    MCPSettingsOAuthTokenStore,
    SettingsBackedMCPToolProvider,
    create_settings_backed_mcp_tool_provider,
)
from openhands.agent_server.persistence import (
    PersistedSettings,
    get_settings_store,
    reset_stores,
)
from openhands.sdk.mcp.config import coerce_mcp_config, dump_mcp_config


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return int(sock.getsockname()[1])


def _wait_for_http_server(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.2) as client:
                client.get(f"http://127.0.0.1:{port}/")
            return
        except httpx.ConnectError:
            time.sleep(0.05)
        except Exception:
            return
    raise RuntimeError(f"Timed out waiting for test MCP server on port {port}")


class _HeadlessOAuth(OAuth):
    """FastMCP OAuth client that follows the authorization redirect itself.

    This preserves the real DCR/PKCE/token exchange while avoiding an external
    browser and local callback server in CI.
    """

    reject_redirects = False
    redirect_count = 0

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("callback_port", _find_free_port())
        super().__init__(*args, **kwargs)
        self._redirect_location: str | None = None

    async def redirect_handler(self, authorization_url: str) -> None:
        type(self).redirect_count += 1
        if type(self).reject_redirects:
            raise AssertionError("OAuth redirect should not run with stored tokens")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(authorization_url)
        assert response.status_code in {302, 303, 307, 308}
        self._redirect_location = response.headers["location"]

    async def callback_handler(self) -> tuple[str, str | None]:
        assert self._redirect_location is not None
        query = parse_qs(urlparse(self._redirect_location).query)
        code = query.get("code", [None])[0]
        assert code is not None
        return code, query.get("state", [None])[0]


@pytest.fixture
def protected_oauth_mcp_server():
    port = _find_free_port()
    provider = InMemoryOAuthProvider(
        base_url=f"http://127.0.0.1:{port}",
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mail.read"],
            default_scopes=["mail.read"],
        ),
        required_scopes=["mail.read"],
    )
    mcp = FastMCP("protected-oauth-mcp", auth=provider)

    @mcp.tool()
    def read_subject(subject: str) -> str:
        return f"OAuth mail subject: {subject}"

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            mcp.run_http_async(
                host="127.0.0.1",
                port=port,
                transport="http",
                show_banner=False,
                path="/mcp",
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_for_http_server(port)
    yield f"http://127.0.0.1:{port}/mcp"


@pytest.mark.asyncio
async def test_mcp_oauth_token_store_persists_values_in_settings(
    tmp_path: Path,
):
    reset_stores()
    try:
        config = Config(
            session_api_keys=[],
            conversations_path=tmp_path / "conversations",
            secret_key=SecretStr("mcp-oauth-test-key"),
        )
        settings = PersistedSettings()
        settings.agent_settings = settings.agent_settings.model_copy(
            update={
                "mcp_config": coerce_mcp_config(
                    {
                        "superhuman": {
                            "url": "https://mcp.example.com/mcp",
                            "auth": {
                                "strategy": "oauth2",
                                "authentication": {
                                    "type": "oauth",
                                    "client_auth_method": "none",
                                },
                            },
                        }
                    }
                )
            }
        )
        settings_store = get_settings_store(config)
        settings_store.save(settings)
        create_settings_backed_mcp_tool_provider(config)

        key = "https://mcp.example.com/mcp/tokens"
        client_info_key = "https://mcp.example.com/mcp/client_info"
        token_expiry_key = "https://mcp.example.com/mcp/token_expiry"
        value = {
            "access_token": "super-secret-token",
            "refresh_token": "refresh-token",
        }
        client_info = {
            "redirect_uris": ["http://127.0.0.1:64801/callback"],
            "client_id": "superhuman-client",
            "client_secret": "superhuman-client-secret",
        }
        token_expiry = {"expires_at": 12345.0}

        store = MCPSettingsOAuthTokenStore()
        await store.put(key=key, value=value, collection="mcp-oauth-token")
        await store.put(
            key=client_info_key,
            value=client_info,
            collection="mcp-oauth-client-info",
        )
        await store.put(
            key=token_expiry_key,
            value=token_expiry,
            collection="mcp-oauth-token-expiry",
        )

        reloaded_store = MCPSettingsOAuthTokenStore()
        assert (
            await reloaded_store.get(key=key, collection="mcp-oauth-token")
        ) == value
        assert (
            await reloaded_store.get(
                key=client_info_key,
                collection="mcp-oauth-client-info",
            )
        ) == client_info
        assert (
            await reloaded_store.get(
                key=token_expiry_key,
                collection="mcp-oauth-token-expiry",
            )
        ) == token_expiry

        on_disk_text = (settings_store.persistence_dir / "settings.json").read_text()
        assert "super-secret-token" not in on_disk_text
        assert "refresh-token" not in on_disk_text
        assert "superhuman-client-secret" not in on_disk_text

        on_disk = json.loads(on_disk_text)
        stored_state = on_disk["agent_settings"]["mcp_config"]["superhuman"]["auth"][
            "state"
        ]
        stored_value = stored_state["tokens"]
        assert stored_value["access_token"].startswith("gAAAA")
        assert stored_value["refresh_token"].startswith("gAAAA")
        assert stored_state["client_info"]["client_secret"].startswith("gAAAA")
        assert stored_state["token_expires_at"] == 12345.0

        loaded = settings_store.load()
        assert loaded is not None
        server = dump_mcp_config(loaded.agent_settings.mcp_config)["superhuman"]
        auth = server["auth"]
        assert isinstance(auth, dict)
        assert auth["state"] == {
            "tokens": value,
            "client_info": client_info,
            "token_expires_at": 12345.0,
        }
    finally:
        reset_stores()


def test_oauth_mcp_connection_persists_and_reuses_settings_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_oauth_mcp_server: str,
):
    """First-auth persists tokens; runtime provider reuses them headlessly.

    The server uses FastMCP's in-memory OAuth provider. The interactive
    first-auth (the install/test flow's job — conversation start never goes
    interactive) uses the SDK's plain OAuth with only the browser/callback
    step replaced by a deterministic redirect follower. The runtime provider
    then reuses the persisted tokens without any redirect at all.
    """

    reset_stores()
    _HeadlessOAuth.redirect_count = 0
    _HeadlessOAuth.reject_redirects = False
    monkeypatch.setattr(mcp_utils, "OAuth", _HeadlessOAuth)
    try:
        config = Config(
            session_api_keys=[],
            conversations_path=tmp_path / "conversations",
            secret_key=SecretStr("mcp-oauth-e2e-test-key"),
        )
        mcp_config = coerce_mcp_config(
            {
                "mail": {
                    "url": protected_oauth_mcp_server,
                    "transport": "http",
                    "auth": {
                        "strategy": "oauth2",
                        "authentication": {
                            "type": "oauth",
                            "client_auth_method": "none",
                            "scopes": ["mail.read"],
                        },
                    },
                }
            }
        )
        settings = PersistedSettings()
        settings.agent_settings = settings.agent_settings.model_copy(
            update={"mcp_config": mcp_config}
        )
        settings_store = get_settings_store(config)
        settings_store.save(settings)
        tool_provider = create_settings_backed_mcp_tool_provider(config)

        # Explicit first-auth (what the install-time OAuth probe does):
        # interactive OAuth + settings-backed token storage.
        from openhands.sdk.mcp.utils import create_mcp_tools

        with create_mcp_tools(
            mcp_config,
            timeout=10.0,
            mcp_oauth_token_storage=MCPSettingsOAuthTokenStore(),
        ) as client:
            tool = next(tool for tool in client.tools if tool.name == "read_subject")
            assert tool.executor is not None
            observation = tool.executor(
                tool.action_from_arguments({"subject": "Quarterly Plan"})
            )
            assert "OAuth mail subject: Quarterly Plan" in observation.text

        assert _HeadlessOAuth.redirect_count == 1
        on_disk_text = (settings_store.persistence_dir / "settings.json").read_text()
        assert "test_access_token_" not in on_disk_text
        assert "test_refresh_token_" not in on_disk_text

        reloaded = settings_store.load()
        assert reloaded is not None
        persisted_mcp_config = reloaded.agent_settings.mcp_config
        server = dump_mcp_config(persisted_mcp_config)["mail"]
        auth = server["auth"]
        assert isinstance(auth, dict)
        state = auth["state"]
        assert isinstance(state, dict)
        tokens = state["tokens"]
        assert isinstance(tokens, dict)
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        assert isinstance(access_token, str)
        assert isinstance(refresh_token, str)
        assert access_token.startswith("test_access_token_")
        assert refresh_token.startswith("test_refresh_token_")
        client_info = state["client_info"]
        assert isinstance(client_info, dict)
        assert client_info["client_id"]

        # Runtime path: reuses persisted tokens, never re-authorizes.
        _HeadlessOAuth.reject_redirects = True
        with tool_provider.create_tools(
            persisted_mcp_config,
            timeout=10.0,
        ) as client:
            tool = next(tool for tool in client.tools if tool.name == "read_subject")
            assert tool.executor is not None
            observation = tool.executor(
                tool.action_from_arguments({"subject": "Follow-up"})
            )
            assert "OAuth mail subject: Follow-up" in observation.text

        assert _HeadlessOAuth.redirect_count == 1
    finally:
        reset_stores()


def test_runtime_provider_rejects_interactive_auth_when_tokens_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_oauth_mcp_server: str,
):
    """Conversation-start OAuth with no stored tokens fails fast, no browser.

    The runtime provider must never fall into FastMCP's interactive flow
    (browser + 300s callback wait) — needing it means the credential is
    missing/dead and belongs back in the install-time re-auth flow.
    """
    reset_stores()
    try:
        config = Config(
            session_api_keys=[],
            conversations_path=tmp_path / "conversations",
            secret_key=SecretStr("mcp-oauth-e2e-test-key"),
        )
        mcp_config = coerce_mcp_config(
            {
                "mail": {
                    "url": protected_oauth_mcp_server,
                    "transport": "http",
                    "auth": {
                        "strategy": "oauth2",
                        "authentication": {
                            "type": "oauth",
                            "client_auth_method": "none",
                            "scopes": ["mail.read"],
                        },
                    },
                }
            }
        )
        settings = PersistedSettings()
        settings.agent_settings = settings.agent_settings.model_copy(
            update={"mcp_config": mcp_config}
        )
        get_settings_store(config).save(settings)
        tool_provider = create_settings_backed_mcp_tool_provider(config)

        with patch("webbrowser.open") as mock_open:
            with pytest.raises(Exception) as exc_info:
                tool_provider.create_tools(mcp_config, timeout=10.0)
        mock_open.assert_not_called()
        from openhands.sdk.mcp.exceptions import MCPAuthorizationRequiredError

        chain: list[BaseException] = []
        current: BaseException | None = exc_info.value
        while current is not None and len(chain) < 20:
            chain.append(current)
            current = current.__cause__ or current.__context__
        assert any(isinstance(cause, MCPAuthorizationRequiredError) for cause in chain)
    finally:
        reset_stores()


@pytest.mark.asyncio
async def test_mcp_oauth_token_storage_does_not_attach_to_non_oauth_server(
    tmp_path: Path,
):
    reset_stores()
    try:
        config = Config(
            session_api_keys=[],
            conversations_path=tmp_path / "conversations",
            secret_key=SecretStr("mcp-oauth-test-key"),
        )
        settings = PersistedSettings()
        settings.agent_settings = settings.agent_settings.model_copy(
            update={
                "mcp_config": coerce_mcp_config(
                    {"plain": {"url": "https://mcp.example.com/mcp"}}
                )
            }
        )
        settings_store = get_settings_store(config)
        settings_store.save(settings)

        create_settings_backed_mcp_tool_provider(config)
        store = MCPSettingsOAuthTokenStore()

        await store.put(
            key="https://mcp.example.com/mcp/tokens",
            value={"access_token": "super-secret-token"},
            collection="mcp-oauth-token",
        )

        loaded = settings_store.load()
        assert loaded is not None
        server = dump_mcp_config(loaded.agent_settings.mcp_config)["plain"]
        assert "auth" not in server
    finally:
        reset_stores()


def test_settings_backed_provider_forwards_on_tools_reconciled():
    """on_tools_reconciled must reach create_mcp_tools(), not be dropped.

    Previously this provider only accepted on_tools_changed, so callers had
    to attach on_tools_reconciled to the returned client after the fact --
    missing any notification that arrived during the initial connect.
    """
    provider = SettingsBackedMCPToolProvider()
    config = coerce_mcp_config({"fake": {"command": "true"}})

    def callback(client, tools):
        return None

    with patch(
        "openhands.agent_server.mcp_oauth_store.create_mcp_tools"
    ) as mock_create:
        provider.create_tools(config, on_tools_reconciled=callback)

    assert mock_create.call_args.kwargs["on_tools_reconciled"] is callback


async def test_non_interactive_oauth_raises_instead_of_opening_a_browser():
    """Runtime OAuth must never pop a browser during conversation start.

    FastMCP's redirect_handler opens the system browser and then waits up
    to 300s for the callback — far past the runtime connect timeout, so
    the flow could never complete and retried (another browser tab) on
    every conversation start. The non-interactive variant raises so the
    per-server isolation can skip the server (perdix #17).
    """
    from openhands.agent_server.mcp_oauth_store import (
        _non_interactive_oauth_factory,
    )
    from openhands.sdk.mcp.config import (
        MCPOAuthAuthCredential,
        MCPOAuthAuthentication,
    )
    from openhands.sdk.mcp.exceptions import MCPAuthorizationRequiredError

    auth = MCPOAuthAuthCredential(
        strategy="oauth2",
        authentication=MCPOAuthAuthentication(type="oauth"),
    )
    server_spec = coerce_mcp_config({"sentry": {"url": "https://mcp.sentry.dev/mcp"}})[
        "sentry"
    ]
    oauth = _non_interactive_oauth_factory("sentry", server_spec, auth, None)

    assert oauth is not None
    with patch("webbrowser.open") as mock_open:
        with pytest.raises(MCPAuthorizationRequiredError):
            await oauth.redirect_handler("https://sentry.io/oauth/authorize?x=1")
    mock_open.assert_not_called()


def test_settings_backed_provider_uses_non_interactive_oauth_factory():
    """The runtime provider injects the browser-suppressing OAuth factory."""
    provider = SettingsBackedMCPToolProvider()
    config = coerce_mcp_config({"fake": {"command": "true"}})

    with patch(
        "openhands.agent_server.mcp_oauth_store.create_mcp_tools"
    ) as mock_create:
        provider.create_tools(config)

    from openhands.agent_server.mcp_oauth_store import (
        _non_interactive_oauth_factory,
    )

    assert (
        mock_create.call_args.kwargs["mcp_oauth_factory"]
        is _non_interactive_oauth_factory
    )
