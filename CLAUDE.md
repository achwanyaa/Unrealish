# Unrealish

Claude drives a headless Unreal Editor on a Modal GPU through a small MCP server.
Full design and build order: `CONNECTION_PLAN.md`.

## Layout

- `modal/image.py`: shared Modal app (`unreal`), images, volume `unreal-data`, state dict `unreal-state`
- `modal/worker.py`: `UnrealWorker` (L4 GPU, one container, owns the editor process) + build-step runner
- `modal/gateway.py`: MCP server (Streamable HTTP, bearer auth, CPU only); deploying it ships the worker too
- `modal/volume_setup.py`: uploads `unreal-project/` to the volume at `/project`
- `scripts/`: Unreal Python run inside the editor (`spawn_test.py`, `screenshot.py`, `import_glb.py`, `render_views.py`)
- `unreal-project/`: `SajimSandbox.uproject` (UE 5.8) + `Config/`; models to import go in `SourceAssets/*.glb`
- `.github/workflows/unreal-modal.yml`: runs the steps below from GitHub (this cloud sandbox can't reach Modal)
- `tests/`: gateway tests with a fake backend (no Modal account needed)

## Commands

```bash
pip install -r requirements-dev.txt
pytest tests/                                   # gateway auth + tools, runs locally

python modal/volume_setup.py                    # push project to the volume (re-run after edits)
modal run modal/worker.py --step diag           # GPU/Vulkan visibility, no editor
modal run modal/worker.py --step boot           # step 2
modal run modal/worker.py --step python         # step 3
modal run modal/worker.py --step screenshot     # step 4 (go/no-go), PNG lands in ./shots/
modal run modal/worker.py --step model          # import SourceAssets/*.glb, render 6 views to ./shots/<model>/
modal deploy modal/gateway.py                   # step 5
modal volume get unreal-data /shots ./shots     # pull screenshots
```

Use another engine version with `UE_IMAGE_TAG=dev-5.4 modal ...` (default `dev-slim-5.8`).
From a phone: GitHub → Actions → "Unreal on Modal" → Run workflow → pick the step. Run `diag` first.

## Rules

- **Secrets never go in the repo, chat, or code.** Code only names them: Modal Secrets `ghcr-epic`
  (`REGISTRY_USERNAME`, `REGISTRY_PASSWORD`) and `unreal-mcp-auth` (`MCP_AUTH_TOKEN`); Claude Code env
  vars `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `UNREAL_MCP_TOKEN`, `UNREAL_MCP_URL`.
- **Cost guardrails stay on:** `max_containers=1`, `scaledown_window=600`, `timeout=3600` on the worker,
  plus a 2 h hard cap (`MAX_UPTIME_S`), and a 90 min job timeout in the workflow. Worker is L4 + 8 CPU + 32 GiB
  (~$1.45/h). The user wants Modal costs kept low: don't loosen any of this without asking.
- **The repo is public.** Client models (e.g. the Rafiki industrial park .glb) only get committed once it's private.
- The editor runs as user `ue4` (it refuses root). Anything it writes on the volume must be ue4-writable.

## Using the `unreal` MCP tools

1. `editor_start`, then poll `editor_status` until `phase` is `ready`. `editor_status` never wakes the GPU.
2. Do almost everything with `run_python` (`import unreal`; `print()` what you need back).
3. `screenshot` renders the level-viewport camera; move it with
   `unreal.UnrealEditorSubsystem().set_level_viewport_camera_info(loc, rot)`.
4. `save_all` before stopping, then `editor_stop`. If a stop fails, `modal container stop <id>`
   (id is in `editor_status`), or the Modal dashboard.
