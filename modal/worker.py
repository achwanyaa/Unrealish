"""UE worker: one GPU container that owns a headless Unreal Editor process.

Build-order checks (CONNECTION_PLAN.md section 3) run from here:

    modal run modal/worker.py --step diag        # GPU / Vulkan visible?
    modal run modal/worker.py --step boot        # step 2: editor up, port 30010 answers
    modal run modal/worker.py --step python      # step 3: Python round-trip
    modal run modal/worker.py --step screenshot  # step 4: cube + light -> PNG (go/no-go)
    modal run modal/worker.py --step model       # import SourceAssets/*.glb, render views -> PNGs

Each `modal run` is one ephemeral app: the GPU container stops when the command ends.
"""

import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import modal

from image import CPU_CORES, DATA, EDITOR_USER, GPU_TYPE, MEMORY_MB, SCRIPTS, UE_ROOT, app, state, ue_image, vol

RC_URL = "http://127.0.0.1:30010"
EDITOR_BIN = f"{UE_ROOT}/Engine/Binaries/Linux/UnrealEditor"
BOOT_TIMEOUT_S = 3600  # first boot compiles shaders; DDC on the volume makes later boots faster
PYTHON_TIMEOUT_S = 1800
HEARTBEAT_S = 30
MAX_UPTIME_S = 2 * 3600  # hard cap per container, whatever the idle timer says
RESULT_MARK = "@@RESULT@@"

# Bypass any proxy env for localhost calls to the editor.
_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@app.function(image=ue_image, gpu=GPU_TYPE, timeout=600)
def gpu_diagnostics() -> str:
    """Report what the container can see of the GPU, without starting the editor."""
    out = []
    for cmd in (
        "nvidia-smi",
        "ls -l /etc/vulkan/icd.d /usr/share/vulkan/icd.d",
        "ldconfig -p | grep -i -E 'nvidia|vulkan'",
        "vulkaninfo --summary",
        f"ls -l {EDITOR_BIN}",
    ):
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        out.append(f"$ {cmd}\n{r.stdout}{r.stderr}")
    return "\n".join(out)


@app.cls(
    image=ue_image,
    gpu=GPU_TYPE,
    cpu=CPU_CORES,
    memory=MEMORY_MB,
    max_containers=1,  # every call hits the same editor
    scaledown_window=600,  # 10 min idle -> shut down
    timeout=3600,
    startup_timeout=BOOT_TIMEOUT_S,
    volumes={DATA: vol},
)
@modal.concurrent(max_inputs=4)  # lets status/stop through while run_python is busy
class UnrealWorker:
    @modal.enter()
    def boot(self):
        self.started_at = time.time()
        self.ready = False
        self.stopping = False
        self.lock = threading.Lock()  # one editor command at a time
        self.state_lock = threading.Lock()  # no heartbeat may re-publish after a stop
        self.container_id = os.environ.get("MODAL_TASK_ID", "")
        self._publish_state()
        try:
            self._launch_editor()
        except Exception:
            self._halt()
            raise
        self.ready = True
        self._publish_state()
        print(f"Editor ready in {time.time() - self.started_at:.0f}s", flush=True)

    def _launch_editor(self):
        uprojects = sorted(glob.glob(f"{DATA}/project/*.uproject"))
        if not uprojects:
            raise RuntimeError(f"No .uproject in {DATA}/project. Run `python modal/volume_setup.py` first.")
        self.uproject = uprojects[0]
        self.log_path = Path(self.uproject).parent / "Saved/Logs" / (Path(self.uproject).stem + ".log")

        for d in ("ddc", "shots", "logs"):
            os.makedirs(f"{DATA}/{d}", exist_ok=True)
        # Files uploaded by volume_setup.py are root-owned; the editor runs as ue4.
        subprocess.run(
            ["chown", "-R", f"{EDITOR_USER}:{EDITOR_USER}", f"{DATA}/project", f"{DATA}/shots", f"{DATA}/ddc"],
            check=True,
        )

        env = dict(os.environ)
        env["UE-LocalDataCachePath"] = f"{DATA}/ddc"
        cmd = [
            "runuser", "-u", EDITOR_USER, "--",
            EDITOR_BIN, self.uproject,
            "-RenderOffScreen", "-vulkan", "-unattended", "-nosplash", "-nosound",
            "-RCWebControlEnable", "-ExecCmds=WebControl.StartServer",
            "-stdout", "-FullStdOutLogOutput",
        ]  # fmt: skip
        print("Launching:", " ".join(cmd), flush=True)
        # stdout is inherited, so the editor log shows up in `modal run` / the Modal dashboard.
        self.proc = subprocess.Popen(cmd, env=env, start_new_session=True)

        threading.Thread(target=self._heartbeat, daemon=True).start()
        self._wait_until_ready()

    @modal.exit()
    def teardown(self):
        self._halt()

    # ---- MCP-facing methods -------------------------------------------------

    @modal.method()
    def run_python(self, code: str) -> dict:
        """Run Unreal Python in the editor. Returns {ok, stdout, error, result}."""
        return self._exec(code)

    @modal.method()
    def screenshot(self, width: int = 1280, height: int = 720) -> bytes:
        """Render the active viewport camera to PNG in /data/shots and return the bytes."""
        out = f"{DATA}/shots/shot-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06d}.png"
        r = self._exec(f"{SCRIPTS}/screenshot.py {out} {int(width)} {int(height)}")
        if not r["ok"]:
            raise RuntimeError(f"screenshot failed: {r['error'] or r['stdout']}")
        vol.commit()
        return Path(out).read_bytes()

    @modal.method()
    def status(self) -> dict:
        info = self._snippet(
            "w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
            "emit({'level': w.get_path_name() if w else None,"
            " 'engine': unreal.SystemLibrary.get_engine_version()})"
        )
        return {
            "alive": self._alive(),
            "ready": self.ready,
            "uptime_s": round(time.time() - self.started_at),
            "uproject": self.uproject,
            **info,
        }

    @modal.method()
    def list_assets(self, path: str = "/Game", recursive: bool = True) -> list:
        return self._snippet(f"emit(list(unreal.EditorAssetLibrary.list_assets({path!r}, {bool(recursive)!r})))")

    @modal.method()
    def import_model(self, name: str = "") -> dict:
        """Import a .glb from /data/project/SourceAssets into its own level. Returns the import summary."""
        found = sorted(glob.glob(f"{DATA}/project/SourceAssets/*.glb"))
        if name:
            found = [f for f in found if Path(f).name == name]
        if not found:
            raise RuntimeError(f"No .glb in {DATA}/project/SourceAssets (asked for {name!r}).")
        glb = found[0]
        if " " in glb:
            raise RuntimeError(f"Rename {Path(glb).name}: no spaces allowed in the file name.")
        stem = re.sub(r"\W+", "_", Path(glb).stem)[:48]
        r = self._exec(f"{SCRIPTS}/import_glb.py {glb} /Game/Imported/{stem} /Game/Maps/{stem}")
        if not r["ok"]:
            raise RuntimeError(f"import failed: {r['error'] or r['result']}")
        summary = json.loads(r["stdout"].strip().splitlines()[-1])
        vol.commit()
        return summary

    @modal.method()
    def render_views(self, width: int = 1920, height: int = 1080) -> dict:
        """Render the standard views of the imported model. Returns {file name: PNG bytes}."""
        out = f"{DATA}/shots/views-{time.strftime('%Y%m%d-%H%M%S')}"
        r = self._exec(f"{SCRIPTS}/render_views.py {out} {int(width)} {int(height)}")
        if not r["ok"]:
            raise RuntimeError(f"render failed: {r['error'] or r['result']}")
        vol.commit()
        return {p.name: p.read_bytes() for p in sorted(Path(out).glob("*.png"))}

    @modal.method()
    def save_all(self) -> dict:
        ok = self._snippet("emit(unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True))")
        vol.commit()
        return {"saved": bool(ok)}

    @modal.method()
    def shutdown(self, save: bool = True) -> dict:
        """Stop the editor and let the container exit right after this call."""
        saved = None
        if save and self._alive():
            try:
                saved = bool(self._snippet("emit(unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True))"))
            except Exception as e:  # still stop, but say the save failed
                saved = f"save failed: {e}"
        self._halt()
        modal.experimental.stop_fetching_inputs()
        return {"stopped": True, "saved": saved, "uptime_s": round(time.time() - self.started_at)}

    # ---- internals ----------------------------------------------------------

    def _alive(self) -> bool:
        return getattr(self, "proc", None) is not None and self.proc.poll() is None

    def _publish_state(self):
        with self.state_lock:
            if self.stopping:
                return
            state["worker"] = {
                "started_at": self.started_at,
                "last_seen": time.time(),
                "ready": self.ready,
                "container_id": self.container_id,
            }

    def _halt(self):
        """Mark stopped, stop the editor, persist the volume. Safe to call more than once."""
        with self.state_lock:
            self.stopping = True
            _clear_state()
        self._stop_editor()
        vol.commit()

    def _heartbeat(self):
        while not self.stopping:
            time.sleep(HEARTBEAT_S)
            if self.stopping:
                return
            if not self._alive():
                print("Editor process exited; container will stop.", flush=True)
            elif time.time() - self.started_at > MAX_UPTIME_S:
                print(f"Hit MAX_UPTIME_S={MAX_UPTIME_S}; stopping editor.", flush=True)
            else:
                self._publish_state()
                continue
            self._halt()
            modal.experimental.stop_fetching_inputs()
            return

    def _stop_editor(self):
        if not self._alive():
            return
        os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()

    def _log_tail(self, n: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return "(no editor log yet)"

    def _wait_until_ready(self):
        deadline = self.started_at + BOOT_TIMEOUT_S - 60
        phase = "remote control"
        last_error = None
        while time.time() < deadline:
            if not self._alive():
                raise RuntimeError(f"Editor exited with code {self.proc.returncode}.\n{self._log_tail()}")
            try:
                if phase == "remote control":
                    self._rc("GET", "/remote/info", timeout=5)
                    print("Remote Control answering on :30010", flush=True)
                    phase = "python"
                r = self._exec("import unreal; print(unreal.SystemLibrary.get_engine_version())", timeout=60)
                if r["ok"]:
                    print("Python round-trip:", r["stdout"].strip(), flush=True)
                    return
                print("Python not ready yet:", r["error"], flush=True)
            except Exception as e:  # not up yet; keep polling until the deadline
                last_error = repr(e)
            time.sleep(5)
        raise RuntimeError(f"Editor not ready after {BOOT_TIMEOUT_S}s (stuck at: {phase}, last error: {last_error}).\n{self._log_tail()}")

    def _rc(self, method: str, route: str, body: dict | None = None, timeout: float = PYTHON_TIMEOUT_S) -> dict:
        req = urllib.request.Request(
            RC_URL + route,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with _http.open(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def _exec(self, code: str, timeout: float = PYTHON_TIMEOUT_S) -> dict:
        """Run code (or `path/to/file.py args`) via Remote Control, falling back to remote execution."""
        if not self._alive():
            raise RuntimeError(f"Editor is not running.\n{self._log_tail()}")
        with self.lock:
            try:
                resp = self._rc(
                    "PUT",
                    "/remote/object/call",
                    {
                        "objectPath": "/Script/PythonScriptPlugin.Default__PythonScriptLibrary",
                        "functionName": "ExecutePythonCommandEx",
                        "parameters": {
                            "PythonCommand": code,
                            "ExecutionMode": "ExecuteFile",
                            "FileExecutionScope": "Public",
                        },
                        "generateTransaction": False,
                    },
                    timeout=timeout,
                )
                return _normalize(resp.get("ReturnValue"), resp.get("CommandResult", ""), resp.get("LogOutput", []), "Type", "Output")
            except urllib.error.HTTPError as e:
                # 4xx/5xx from Remote Control itself (e.g. remote Python disabled): try the other channel.
                print(f"Remote Control call failed ({e.code}); using Python remote execution.", flush=True)
                return _remote_exec(code, timeout)

    def _snippet(self, body: str):
        """Run a small snippet that calls emit(value) once; return the value."""
        code = (
            "import json, unreal\n"
            f"def emit(v): print({RESULT_MARK!r} + json.dumps(v, default=str))\n"
            + body
        )
        r = self._exec(code)
        if not r["ok"]:
            raise RuntimeError(r["error"] or r["result"])
        for line in reversed(r["stdout"].splitlines()):
            if line.startswith(RESULT_MARK):
                return json.loads(line[len(RESULT_MARK):])
        raise RuntimeError(f"no result from snippet; stdout was:\n{r['stdout']}")


def _clear_state():
    """Forget the worker and any pending start request, so the gateway reports 'stopped'."""
    state.pop("worker", None)
    state.pop("requested", None)


def _normalize(ok, result: str, log: list, type_key: str, text_key: str) -> dict:
    stdout = "\n".join(e.get(text_key, "") for e in log if e.get(type_key) == "Info")
    error = "\n".join(e.get(text_key, "") for e in log if e.get(type_key) in ("Warning", "Error"))
    if not ok and not error:
        error = result
    return {"ok": bool(ok), "stdout": stdout, "error": error, "result": result}


def _remote_exec(code: str, timeout: float) -> dict:
    """Fallback: Unreal's own Python remote execution protocol (needs bRemoteExecution=True)."""
    found = glob.glob(f"{UE_ROOT}/Engine/Plugins/**/PythonScriptPlugin/Content/Python/remote_execution.py", recursive=True)
    if not found:
        raise RuntimeError("remote_execution.py not found in the engine install")
    sys.path.insert(0, os.path.dirname(found[0]))
    import remote_execution as rx

    cfg = rx.RemoteExecutionConfig()
    cfg.multicast_bind_address = "127.0.0.1"
    remote = rx.RemoteExecution(cfg)
    remote.start()
    try:
        deadline = time.time() + 15
        while not remote.remote_nodes and time.time() < deadline:
            time.sleep(0.5)
        if not remote.remote_nodes:
            raise RuntimeError("No editor answered Python remote execution discovery")
        remote.open_command_connection(remote.remote_nodes[0]["node_id"])
        resp = remote.run_command(code, unattended=True, exec_mode=rx.MODE_EXEC_FILE)
        return _normalize(resp.get("success"), resp.get("result", ""), resp.get("output", []), "type", "output")
    finally:
        remote.stop()


@app.local_entrypoint()
def main(step: str = "screenshot"):
    steps = ["diag", "boot", "python", "screenshot", "model"]
    if step not in steps:
        raise SystemExit(f"--step must be one of {steps}")
    if step == "diag":
        print(gpu_diagnostics.remote())
        return

    worker = UnrealWorker()
    print("status:", json.dumps(worker.status.remote(), indent=2))  # boots the editor
    if step == "boot":
        return

    r = worker.run_python.remote("import unreal\nprint(unreal.SystemLibrary.get_engine_version())")
    print("python:", json.dumps(r, indent=2))
    if not r["ok"]:
        raise SystemExit("Step 3 failed: Python round-trip")
    if step == "python":
        return

    if step == "model":
        summary = worker.import_model.remote()
        print("import:", json.dumps(summary, indent=2))
        views = worker.render_views.remote()
        out_dir = Path("shots") / summary["level"].rsplit("/", 1)[-1]
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, png in views.items():
            (out_dir / name).write_bytes(png)
        print(f"Wrote {len(views)} views to {out_dir}/: {', '.join(views)}")
        return

    r = worker.run_python.remote(f"{SCRIPTS}/spawn_test.py")
    print("spawn_test:", json.dumps(r, indent=2))
    if not r["ok"]:
        raise SystemExit("Step 4 failed: spawn_test.py")
    png = worker.screenshot.remote()
    os.makedirs("shots", exist_ok=True)
    out = f"shots/step4-{time.strftime('%Y%m%d-%H%M%S')}.png"
    Path(out).write_bytes(png)
    print(f"Step 4: wrote {out} ({len(png)} bytes). Open it: a cube on a floor means GO.")
