"""Gateway tests against a fake backend: auth, tool list, and tool calls over Streamable HTTP.

    pip install modal mcp==2.3.0 pytest httpx
    pytest tests/
"""

import base64
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "modal"))
from gateway import NotRunning, create_app  # noqa: E402

TOKEN = "test-token-123"
PNG = b"\x89PNG\r\n\x1a\nfake"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class FakeBackend:
    def __init__(self, running=True):
        self.running = running
        self.calls = []

    async def status(self):
        return {"running": self.running, "phase": "ready" if self.running else "stopped"}

    async def start(self):
        self.running = True
        return await self.status()

    async def stop(self, save):
        self.calls.append(("stop", save))
        self.running = False
        return {"stopped": True}

    async def run_python(self, code):
        if not self.running:
            raise NotRunning("Editor is not running.")
        self.calls.append(("run_python", code))
        return {"ok": True, "stdout": "5.5.0", "error": "", "result": ""}

    async def screenshot(self, width, height):
        return PNG

    async def list_assets(self, path, recursive):
        return [f"{path}/Maps/Sandbox"]

    async def save_all(self):
        return {"saved": True}


def rpc(client, method, params=None, token=TOKEN):
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call_tool(client, name, args=None):
    r = rpc(client, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, TOKEN), base_url="https://example--unreal-gateway.modal.run") as c:
        yield c


@pytest.mark.parametrize("token", [None, "wrong", TOKEN + "x", ""])
def test_rejects_bad_tokens(client, token):
    r = rpc(client, "tools/list", token=token)
    assert r.status_code == 401
    assert "bearer" in r.headers["www-authenticate"].lower()


def test_empty_server_token_fails_closed(backend):
    with TestClient(create_app(backend, "")) as c:
        assert rpc(c, "tools/list", token="").status_code == 401


def test_lists_tools(client):
    r = rpc(client, "tools/list")
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"editor_start", "editor_status", "editor_stop", "run_python", "screenshot", "list_assets", "save_all"}


def test_run_python(client, backend):
    result = call_tool(client, "run_python", {"code": "print(1)"})
    assert not result.get("isError")
    assert json.loads(result["content"][0]["text"])["stdout"] == "5.5.0"
    assert backend.calls == [("run_python", "print(1)")]


def test_screenshot_returns_png_image(client):
    content = call_tool(client, "screenshot")["content"]
    assert content[0]["type"] == "image"
    assert content[0]["mimeType"] == "image/png"
    assert base64.b64decode(content[0]["data"]) == PNG


def test_tool_error_when_editor_not_running(backend):
    backend.running = False
    with TestClient(create_app(backend, TOKEN)) as c:
        result = call_tool(c, "run_python", {"code": "print(1)"})
    assert result["isError"]
    assert "not running" in result["content"][0]["text"]


def test_stop_passes_save_flag(client, backend):
    call_tool(client, "editor_stop", {"save": False})
    assert backend.calls == [("stop", False)]
