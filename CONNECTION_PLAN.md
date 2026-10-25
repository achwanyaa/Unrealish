# Unreal ↔ Claude Connection Plan (Unreal on Modal, controlled over MCP)

Goal: Claude (Claude Code first, then the claude.ai app) can drive a headless Unreal Editor running on a Modal GPU through a small set of MCP tools.

---

## 1. Architecture

```
Claude Code / claude.ai connector
        │  HTTPS (MCP, Streamable HTTP) + auth
        ▼
[Modal] MCP gateway — CPU, cheap, scales to zero
        │  Modal function call (.remote)
        ▼
[Modal] UE worker — GPU (L4), one container, holds the editor process
        │  localhost
        ▼
Unreal Editor (headless: -RenderOffScreen, Vulkan)
   └─ Python + Remote Control API plugin (HTTP :30010)
        │
        ▼
Modal Volume "unreal-data" (project files, DDC cache, screenshots)
```

Why two pieces: the gateway is light and can wake instantly. The GPU worker is expensive and should only run while you're actually working.

---

## 2. Components

### 2a. UE worker (`modal/worker.py`)
- `@app.cls(gpu="L4", max_containers=1, scaledown_window=600, timeout=3600, volumes={"/data": vol})`
  - `max_containers=1` → every call hits the same editor (state is preserved).
  - `scaledown_window=600` → shuts down after 10 min idle.
- `@modal.enter()`: start the editor in the background with the project from `/data/project/*.uproject`, flags `-RenderOffScreen -unattended -nosplash -RCWebControlEnable`. Wait until port 30010 responds.
- Methods:
  - `run_python(code) -> {stdout, error}`: call `PythonScriptLibrary.ExecutePythonCommandEx` through the Remote Control API.
  - `screenshot() -> bytes`: render the viewport to PNG in `/data/shots/`, then return the bytes.
  - `status()`: editor alive, loaded level, uptime.
- Fallback if Remote Control is flaky: Unreal's Python remote execution (`remote_execution.py`) on localhost.

### 2b. MCP gateway (`modal/gateway.py`)
- Python MCP server (FastMCP, Streamable HTTP) mounted with `@modal.asgi_app()`. No GPU.
- Auth checked on every request (see section 4).
- Tools (keep it small; `run_python` covers almost everything):

| Tool | What it does |
|---|---|
| `editor_start` | Warms the GPU worker, returns status |
| `editor_status` | Alive / level / uptime / est. cost so far |
| `editor_stop` | Stops the worker immediately |
| `run_python` | Runs Unreal Python, returns output/errors |
| `screenshot` | Returns the viewport as an image |
| `list_assets` | Lists assets under a content path |
| `save_all` | Saves dirty levels and assets to the Volume |

### 2c. Image (`modal/image.py`)
- Base: Epic's official `ghcr.io/epicgames/unreal-engine` dev image (private; needs the registry secret).
- Add: Python deps for the worker, Vulkan ICD for NVIDIA, Remote Control + Python Editor Script plugins enabled in the `.uproject`.
- First build is slow (tens of GB). Cached after that.

---

## 3. Build order (each step has a pass/fail check)

1. **Image pulls**: Modal builds the image from ghcr. ✅ when the build succeeds.
2. **Editor boots headless**: `modal run` starts the worker, the editor log shows the project loaded. ✅ when port 30010 answers.
3. **Python round-trip**: `run_python("print(unreal.SystemLibrary.get_engine_version())")` returns a version string. ✅
4. **Screenshot**: spawn a cube plus a light, screenshot, PNG lands in `/data/shots/` and is committed or viewable. ✅ **This is the go/no-go gate.**
5. **Gateway**: deploy `gateway.py`; call `editor_status` with curl and a valid token (works) and an invalid one (401).
6. **Connect Claude Code**: add to `.mcp.json` (below), call the tools from a session.
7. **Connect claude.ai app**: add as a custom connector (needs the auth approach in 4c).

Stop after step 4 if it fails and reassess (fallbacks: packaged build instead of editor, or a Windows host).

---

## 4. Secrets: where each token lives

**Rule: tokens never go in the repo, never in chat, never in code. Only in Modal Secrets or Claude Code environment variables.**

| Token | Where it goes | Name(s) |
|---|---|---|
| Modal token (ID + secret) | Claude Code cloud environment, as environment variables | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` |
| GitHub token with `read:packages` (for Epic image) | Modal dashboard → Secrets → new custom secret `ghcr-epic` | `REGISTRY_USERNAME` = your GitHub username, `REGISTRY_PASSWORD` = the token |
| MCP auth token (generate a long random string) | Modal Secret `unreal-mcp-auth` **and** Claude Code env var | `MCP_AUTH_TOKEN` (Modal), `UNREAL_MCP_TOKEN` (Claude Code) |

Code references them only by name:
```python
image = modal.Image.from_registry(
    "ghcr.io/epicgames/unreal-engine:dev-<version>",
    secret=modal.Secret.from_name("ghcr-epic"),
)
@app.function(secrets=[modal.Secret.from_name("unreal-mcp-auth")])
```

### 4a. Claude Code `.mcp.json` (safe to commit, no secret inside)
```json
{
  "mcpServers": {
    "unreal": {
      "type": "http",
      "url": "https://<workspace>--unreal-gateway.modal.run/mcp",
      "headers": { "Authorization": "Bearer ${UNREAL_MCP_TOKEN}" }
    }
  }
}
```

### 4b. Gateway check
Reject any request whose `Authorization` header doesn't match `MCP_AUTH_TOKEN` (constant-time compare).

### 4c. claude.ai app connector
Custom connectors in the app don't take a static bearer header the way Claude Code does. Options, simplest first:
1. Use Claude Code only for now. Recommended until the spike works.
2. Add OAuth to the gateway later, the proper fix for the app.
Avoid putting the token in the URL. URLs leak into logs.

---

## 5. Cost guardrails (non-negotiable)
- `max_containers=1`, `scaledown_window=600`, `timeout=3600` on the GPU worker.
- `editor_stop` tool always available; `editor_status` reports running time.
- Set a monthly spend limit in the Modal dashboard before the first GPU run.
- Gateway has no GPU and scales to zero.

---

## 6. Repo layout
```
/unreal-project/   SajimSandbox.uproject (+ Content/, Config/)
/modal/            image.py  worker.py  gateway.py  volume_setup.py
/scripts/          UE python snippets (spawn_test.py, screenshot.py)
.mcp.json
CLAUDE.md
CONNECTION_PLAN.md
```

## 7. Open questions
- Which UE version tag (5.4 / 5.5 / latest available on ghcr)?
- L4 vs A10G after the spike (check frame time and boot time).
- What we build first once connected (decide after step 4).

---

## 8. As built (scaffold, not yet run on Modal)

Status: code for steps 1–6 is written. Only the gateway is tested (locally, fake backend). Steps 1–4 need a
Modal account with the secrets above.

Where the code differs from the plan, or adds to it:

- **One Modal app, two files.** `worker.py` and `gateway.py` both attach to app `unreal` (defined in
  `image.py`), so `modal deploy modal/gateway.py` ships both and the URL is `<workspace>--unreal-gateway.modal.run`.
- **MCP SDK 2.x.** FastMCP is now `MCPServer` (`mcp==2.3.0`). The server runs stateless with JSON
  responses, so any gateway container can answer any request. DNS-rebinding host checks are off, because
  they'd reject the `*.modal.run` Host header. The bearer token is the guard.
- **Status never wakes the GPU.** The worker writes a heartbeat to the Modal Dict `unreal-state` every 30 s.
  `editor_status` reads that first and only calls the worker once it's `ready`. `run_python`, `screenshot`,
  `list_assets` and `save_all` refuse to run unless the editor is ready, so only `editor_start` can start a GPU.
- **Stopping.** `editor_stop` asks the worker to save and exit (`stop_fetching_inputs`). While it's still
  booting, it cancels the spawned call with `terminate_containers=True` instead.
- **Extra guardrail.** A 4 h hard cap per container (`MAX_UPTIME_S`), on top of the idle timeout. The
  `timeout=3600` only limits a single call.
- **Editor runs as `ue4`** via `runuser`, since UnrealEditor refuses root. `/data/project` gets chowned on boot.
- **Remote Python is opt-in in UE 5.x.** `Config/DefaultRemoteControl.ini` sets `bEnableRemotePythonExecution`.
  `DefaultEngine.ini` turns on Python remote execution (localhost) for the fallback channel.
- **Screenshot** uses a temporary SceneCapture2D at the viewport camera → render target → PNG, because there's
  no on-screen viewport under `-RenderOffScreen`.
- **`.mcp.json`** reads the URL from `UNREAL_MCP_URL` too, so the workspace name stays out of the repo.
- **Default image tag** is `dev-slim-5.5`; override with `UE_IMAGE_TAG`. This is still open question 1.

Biggest unverified risk: whether Modal's GPU containers expose the NVIDIA **Vulkan/graphics** driver libraries,
not just CUDA. `modal run modal/worker.py --step diag` answers this in a minute, before any editor boot. If
`vulkaninfo` finds no NVIDIA device, that's the step-4 fallback discussion (packaged build, or another host).
