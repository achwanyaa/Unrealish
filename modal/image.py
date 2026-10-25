"""Shared Modal definitions: the app, images, volume and state dict.

Both worker.py and gateway.py attach to the same app ("unreal"), so one
`modal deploy modal/gateway.py` ships both and the gateway URL is
https://<workspace>--unreal-gateway.modal.run.
"""

import json
import os
from pathlib import Path

import modal

APP_NAME = "unreal"

# Epic's private image. Override at deploy time: UE_IMAGE_TAG=dev-5.4 modal deploy ...
UE_IMAGE_TAG = os.environ.get("UE_IMAGE_TAG", "dev-slim-5.5")
UE_ROOT = "/home/ue4/UnrealEngine"
EDITOR_USER = "ue4"  # UnrealEditor refuses to run as root; Epic's image ships this user.

DATA = "/data"  # Volume mount point: project/, ddc/, shots/, logs/
SCRIPTS = "/opt/unreal-mcp/scripts"  # repo's scripts/ dir, mounted read-only

# Rough GPU price for the status estimate (GPU only, excludes CPU/RAM).
# Check https://modal.com/pricing if you change the GPU type.
GPU_TYPE = "L4"
GPU_USD_PER_HOUR = 0.80

REPO_ROOT = Path(__file__).resolve().parent.parent

app = modal.App(APP_NAME)
vol = modal.Volume.from_name("unreal-data", create_if_missing=True)
# Small shared state so the gateway can report status without waking the GPU.
state = modal.Dict.from_name("unreal-state", create_if_missing=True)

_NVIDIA_ICD = json.dumps(
    {"file_format_version": "1.0.0", "ICD": {"library_path": "libGLX_nvidia.so.0", "api_version": "1.3"}}
)

ue_image = (
    modal.Image.from_registry(
        f"ghcr.io/epicgames/unreal-engine:{UE_IMAGE_TAG}",
        secret=modal.Secret.from_name("ghcr-epic"),
        add_python="3.12",
        setup_dockerfile_commands=["USER root"],
    )
    .apt_install("libvulkan1", "vulkan-tools")
    .run_commands(
        "mkdir -p /etc/vulkan/icd.d",
        f"echo '{_NVIDIA_ICD}' > /etc/vulkan/icd.d/nvidia_icd.json",
    )
    .env(
        {
            # Ask for the graphics/display driver libs, not just compute.
            "NVIDIA_DRIVER_CAPABILITIES": "all",
            "NVIDIA_VISIBLE_DEVICES": "all",
            "UE_ROOT": UE_ROOT,
        }
    )
    .add_local_dir(REPO_ROOT / "scripts", SCRIPTS)
    .add_local_python_source("image", "worker")
)

gateway_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("mcp==2.3.0")
    .add_local_python_source("image", "worker")
)
