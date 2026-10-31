"""MCP gateway: a small CPU-only Streamable HTTP MCP server in front of the UE worker.

Deploy (ships the worker too, same app):
    modal deploy modal/gateway.py
URL: https://<workspace>--unreal-gateway.modal.run/mcp

Every request must carry `Authorization: Bearer <MCP_AUTH_TOKEN>` (Modal Secret
`unreal-mcp-auth`). Tools other than editor_start/status/stop refuse to run
unless the editor is already up, so a stray call never wakes the GPU.
"""

import asyncio
import hmac
import json
import os
import time

import modal
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from image import GPU_TYPE, USD_PER_HOUR, app, gateway_image, state
from worker import HEARTBEAT_S, UnrealWorker

STALE_S = 3 * HEARTBEAT_S + 30  # no heartbeat for this long -> treat the worker as gone
FRESH_S = HEARTBEAT_S + 15  # status only asks the worker itself when the heartbeat is this recent
START_GRACE_S = 20 * 60  # a spawned worker may take this long to pull the image and reach enter()

INSTRUCTIONS = """\
Controls a headless Unreal Editor on a Modal GPU. The GPU bills while it runs.
1. editor_start, then poll editor_status until phase is "ready" (minutes; first boot is slower).
2. Use run_python for nearly everything (the `unreal` module is available). Print what you need.
3. screenshot to look at the result. save_all to persist work to the volume.
4. editor_stop when done. It also stops by itself after 10 idle minutes.
"""


class NotRunning(ToolError):
    pass


async def _surface(coro):
    """Await a backend call, passing its error text to Claude.

    MCPServer replaces the message of any non-ToolError exception with a generic one.
    """
    try:
        return await coro
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"{type(e).__name__}: {e}") from e


class ModalBackend:
    """Talks to the UnrealWorker class and the shared state dict."""

    def __init__(self):
        self.worker = UnrealWorker()

    async def _worker_state(self):
        info = await state.get.aio("worker")
        if info and time.time() - info["last_seen"] > STALE_S:
            return None
        return info

    async def status(self) -> dict:
        info = await self._worker_state()
        if info is None:
            req = await state.get.aio("requested")
            if req and time.time() - req["at"] < START_GRACE_S:
                return {
                    "running": False,
                    "phase": "starting",
                    "detail": "GPU container is being scheduled / pulling the image",
                    "requested_s_ago": round(time.time() - req["at"]),
                }
            return {"running": False, "phase": "stopped"}
        uptime = time.time() - info["started_at"]
        out = {
            "running": True,
            "phase": "ready" if info["ready"] else "booting",
            "uptime_s": round(uptime),
            "est_cost_usd": round(uptime / 3600 * USD_PER_HOUR, 2),
            "cost_note": f"{GPU_TYPE} worker at ~${USD_PER_HOUR}/h (GPU+CPU+RAM), approximate",
            "container_id": info.get("container_id"),
        }
        # A just-killed container can leave a "ready" entry behind; calling it would boot a new GPU.
        if info["ready"] and time.time() - info["last_seen"] < FRESH_S:
            out.update(await self.worker.status.remote.aio())
        return out

    async def start(self) -> dict:
        s = await self.status()
        if s["phase"] != "stopped":
            return s
        call = await self.worker.status.spawn.aio()  # wakes the container; enter() boots the editor
        await state.put.aio("requested", {"at": time.time(), "call_id": call.object_id})
        s = await self.status()
        s["hint"] = "Booting takes minutes (longer on first run). Poll editor_status until phase is 'ready'."
        return s

    async def stop(self, save: bool) -> dict:
        info = await self._worker_state()
        req = await state.pop.aio("requested", None)
        if info and info["ready"]:
            try:
                return await asyncio.wait_for(self.worker.shutdown.remote.aio(save=save), timeout=180)
            except Exception as e:
                graceful_error = repr(e)
        else:
            graceful_error = None
        # Booting, or the graceful path failed: cancel the spawn and kill the container.
        if req and req.get("call_id"):
            await modal.FunctionCall.from_id(req["call_id"]).cancel.aio(terminate_containers=True)
            await state.pop.aio("worker", None)
            return {"stopped": True, "how": "terminated container", "graceful_error": graceful_error}
        if info is None and req is None:
            return {"stopped": True, "how": "was not running"}
        cid = (info or {}).get("container_id", "<id from `modal container list`>")
        return {
            "stopped": False,
            "graceful_error": graceful_error,
            "next": f"Run `modal container stop {cid}` or stop it in the Modal dashboard.",
        }

    async def _ready_worker(self):
        info = await self._worker_state()
        if not info or not info["ready"]:
            raise NotRunning("Editor is not running. Call editor_start, then wait for editor_status phase 'ready'.")
        return self.worker

    async def run_python(self, code: str) -> dict:
        return await (await self._ready_worker()).run_python.remote.aio(code)

    async def screenshot(self, width: int, height: int) -> bytes:
        return await (await self._ready_worker()).screenshot.remote.aio(width, height)

    async def list_assets(self, path: str, recursive: bool) -> list:
        return await (await self._ready_worker()).list_assets.remote.aio(path, recursive)

    async def save_all(self) -> dict:
        return await (await self._ready_worker()).save_all.remote.aio()


def build_server(backend) -> MCPServer:
    mcp = MCPServer(name="unreal", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def editor_start() -> dict:
        """Start (warm) the GPU worker and headless editor. Returns status; poll editor_status until 'ready'."""
        return await _surface(backend.start())

    @mcp.tool()
    async def editor_status() -> dict:
        """Editor phase (stopped/starting/booting/ready), loaded level, uptime and estimated cost. Never wakes the GPU."""
        return await _surface(backend.status())

    @mcp.tool()
    async def editor_stop(save: bool = True) -> dict:
        """Stop the GPU worker now. Saves dirty levels/assets first unless save=false."""
        return await _surface(backend.stop(save))

    @mcp.tool()
    async def run_python(code: str) -> dict:
        """Run Unreal Editor Python (module `unreal`) and return {ok, stdout, error, result}. Use print() for output."""
        return await _surface(backend.run_python(code))

    @mcp.tool()
    async def screenshot(width: int = 1280, height: int = 720) -> Image:
        """Render the current level-viewport camera to a PNG and return it. Also kept in /data/shots on the volume."""
        return Image(data=await _surface(backend.screenshot(width, height)), format="png")

    @mcp.tool()
    async def list_assets(path: str = "/Game", recursive: bool = True) -> list[str]:
        """List asset paths under a content path such as /Game or /Game/Maps."""
        return await _surface(backend.list_assets(path, recursive))

    @mcp.tool()
    async def save_all() -> dict:
        """Save all dirty levels and assets to the volume."""
        return await _surface(backend.save_all())

    return mcp


class BearerAuth:
    """ASGI middleware: reject HTTP requests without the exact bearer token (constant-time compare)."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if self.expected is None or not hmac.compare_digest(got, self.expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="unreal-mcp"'),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def create_app(backend, token: str):
    server = build_server(backend)
    asgi = server.streamable_http_app(
        # Stateless + JSON: any gateway container can answer any request.
        stateless_http=True,
        json_response=True,
        # Host checks are for unauthenticated localhost servers; the bearer token guards this one.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuth(asgi, token)


@app.function(image=gateway_image, secrets=[modal.Secret.from_name("unreal-mcp-auth")], timeout=1800)
@modal.concurrent(max_inputs=32)
@modal.asgi_app()
def gateway():
    return create_app(ModalBackend(), os.environ.get("MCP_AUTH_TOKEN", ""))
