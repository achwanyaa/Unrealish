"""Vulkan probe on a plain Debian image: no Epic image, no ghcr-epic secret needed.

    modal run modal/vulkan_probe.py

Answers the plan's biggest risk in about a minute of L4 time: does a Modal GPU
container expose the NVIDIA Vulkan driver, or only CUDA? Uses the same ICD file
and driver env as the UE image. Its own app ("unreal-probe"), because `modal run`
builds every image in an app and the "unreal" app's image needs the Epic secret.
"""

import subprocess

import modal

from image import GPU_TYPE, NVIDIA_ICD

app = modal.App("unreal-probe")

probe_image = (
    modal.Image.debian_slim(python_version="3.12")
    # libGLX_nvidia (the Vulkan ICD) needs the X and GL-dispatch libs, or Vulkan falls back to llvmpipe.
    .apt_install("libvulkan1", "vulkan-tools", "libx11-6", "libxext6", "libegl1", "libgl1")
    .run_commands(
        "mkdir -p /etc/vulkan/icd.d",
        f"echo '{NVIDIA_ICD}' > /etc/vulkan/icd.d/nvidia_icd.json",
    )
    .env({"NVIDIA_DRIVER_CAPABILITIES": "all", "NVIDIA_VISIBLE_DEVICES": "all"})
    .add_local_python_source("image")
)


@app.function(image=probe_image, gpu=GPU_TYPE, timeout=300)
def probe() -> dict:
    out = []
    for cmd in (
        "nvidia-smi",
        "ldconfig -p | grep -i -E 'nvidia|vulkan'",
        "vulkaninfo --summary",
    ):
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        out.append(f"$ {cmd}\n{r.stdout}{r.stderr}")
    summary = out[-1]
    has_glx = "libGLX_nvidia.so.0" in out[1]
    has_nvidia_vk = "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU" in summary and "NVIDIA" in summary
    return {"log": "\n".join(out), "libGLX_nvidia": has_glx, "nvidia_vulkan_device": has_nvidia_vk}


@app.local_entrypoint()
def main():
    r = probe.remote()
    print(r["log"])
    print(f"libGLX_nvidia.so.0 present: {r['libGLX_nvidia']}")
    print(f"NVIDIA Vulkan device:       {r['nvidia_vulkan_device']}")
    print("GO: Vulkan rendering should work." if r["nvidia_vulkan_device"] else "NO-GO: no NVIDIA Vulkan device (see plan step 4 fallbacks).")
