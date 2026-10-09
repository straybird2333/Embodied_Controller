"""Connect an external policy process to an authenticated RoboICL run session.

The supplied policy object implements the XPolicy model calls used by
RoboDojo (normally ``reset``, ``update_obs`` and ``get_action``). It may call
any model provider from the external machine; provider credentials never need
to be installed on the simulator host.
"""

from __future__ import annotations

import inspect
from typing import Any

from roboicl.paths import configure_imports


async def run_external_policy_session(url: str, token: str, policy: Any) -> None:
    """Serve one run's RPC requests over the client-initiated WSS connection."""
    if not isinstance(url, str) or not url.startswith("wss://"):
        raise ValueError("external policy URL must use wss://")
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise ValueError("external policy token is invalid")
    configure_imports("policy")
    import websockets
    from client_server.ws.model_server import PolicyServer, PolicyServerConfig
    from client_server.ws.protocol.codec import decode_envelope, encode_frame

    connect = websockets.connect
    try:
        parameters = inspect.signature(connect).parameters
    except (TypeError, ValueError) as exc:
        raise RuntimeError("cannot determine WebSocket header API") from exc
    header_arg = "additional_headers" if "additional_headers" in parameters else "extra_headers"
    if header_arg not in parameters:
        raise RuntimeError("installed websockets package cannot set Authorization headers")

    # Reuse the pinned protocol's message validation, observation image decode,
    # action RPC dispatch, and replay handling. This object processes frames
    # locally; it does not open a listening socket or start a model provider.
    processor = PolicyServer(policy, PolicyServerConfig(host="127.0.0.1", port=1))
    async with connect(
        url,
        **{
            header_arg: {"Authorization": f"Bearer {token}"},
            "max_size": 32 * 1024 * 1024,
            "compression": None,
            "ping_interval": 30,
            "ping_timeout": 600,
        },
    ) as websocket:
        async for raw in websocket:
            if not isinstance(raw, (bytes, bytearray)):
                raise ValueError("policy RPC messages must be binary")
            request = decode_envelope(bytes(raw))
            response = await processor.process_frame(request)
            if response is None:
                await websocket.close()
                return
            await websocket.send(encode_frame(response))
