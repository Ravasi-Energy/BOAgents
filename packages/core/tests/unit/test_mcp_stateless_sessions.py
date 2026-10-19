"""MCP #45 (GHSA-84m7-p3x7-pcfv) — Streamable HTTP session accumulation.

The advisory hits Streamable-HTTP servers in *stateful* mode (the default):
sessions never expired, `DELETE` left entries registered, refused openings
kept a running task, and the count was unbounded. It explicitly exempts
`stateless_http=True` servers ("no sessions are kept").

Our deployment at ``/mcp`` passes ``stateless_http=True``. These probes pin
that control on the REAL manager — a source grep would be silent if someone
flipped the flag, and a version bump alone would not prove the deployed
config is unaffected.

Probes:
  1. the mounted server instance runs its session manager in stateless mode
     (fails if `stateless_http=True` is ever dropped from the FastMCP ctor);
  2. a real request replay through a stateless manager leaves NO session
     entries and returns NO `mcp-session-id` — there is nothing to
     accumulate;
  3. `DELETE` — the stateful termination request — is refused outright
     (405), so no client can hold or fake a session handle;
  4. initialize params are not retained: the per-request transport is torn
     down with the request, `_server_instances` stays empty.

Library pin note: mcp>=1.30.0 is additionally enforced in pyproject so even
the dormant stateful path carries the upstream idle-timeout/max_sessions
bounds — defense in depth, not the control being relied on.
"""
from __future__ import annotations

import httpx
import pytest

from openexecutive.mcp_server import server as mcp_server

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}
_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


def _mounted_manager():
    """The production session manager — created lazily by
    ``streamable_http_app()``; calling it here yields the same cached
    instance the app lifespan runs."""
    mcp_server.mcp.streamable_http_app()
    return mcp_server.mcp.session_manager


def test_mounted_mcp_server_is_stateless() -> None:
    """The deployed instance must keep `stateless_http=True` — the flag is
    the exemption named by the advisory."""
    assert _mounted_manager().stateless is True


def test_stateful_bounds_exist_for_defense_in_depth() -> None:
    """mcp 1.30.0 ships the upstream session bounds; pinned so a future
    stateful flip still inherits a capped/expiring registry."""
    sm = _mounted_manager()
    assert sm.max_sessions is not None and sm.max_sessions > 0
    assert sm.session_idle_timeout is not None and sm.session_idle_timeout > 0


@pytest.mark.asyncio
async def test_stateless_replay_retains_no_sessions() -> None:
    """Synthetic replay of the advisory's accumulation vector on the real
    manager: N ordinary short requests + a DELETE leave zero retained
    session state."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # A fresh manager with the same config the mounted server uses — the
    # production manager's run() can only be entered once per process and
    # is owned by the app lifespan, so this probes an identical sibling.
    probe = FastMCP(
        name="oe-stateless-probe",
        stateless_http=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app = probe.streamable_http_app()
    sm = probe.session_manager
    assert sm.stateless is True

    async with sm.run(), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://probe"
    ) as client:
        for i in range(6):
            resp = await client.post(
                "/", json={**_INIT, "id": i}, headers=_HEADERS
            )
            assert resp.status_code == 200, resp.text[:200]
            # The stateful contract returns mcp-session-id on initialize;
            # its absence is the absence of a session to accumulate.
            assert "mcp-session-id" not in resp.headers
        # The stateful teardown verb must find nothing to terminate.
        delete = await client.delete("/", headers=_HEADERS)
        assert delete.status_code == 405
        # A refused/garbage opening request also leaves no residue.
        bad = await client.post(
            "/", json={"jsonrpc": "2.0", "id": 99}, headers=_HEADERS
        )
        assert bad.status_code != 200

    # The registry the advisory grows without bound: empty throughout.
    assert sm._server_instances == {}
    assert sm._session_owners == {}
