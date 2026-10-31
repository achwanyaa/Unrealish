"""Upload the local Unreal project to the `unreal-data` Volume at /project.

    python modal/volume_setup.py           # upload unreal-project/ (skips Saved/, Intermediate/, ...)
                                           # models to import go in unreal-project/SourceAssets/*.glb
    python modal/volume_setup.py --list    # show what is on the volume

Overwrites files with the same path; files only on the volume (e.g. levels the
editor saved) are kept. Pull those back with:
    modal volume get unreal-data /project/Content ./unreal-project/ --force
"""

import argparse
from pathlib import Path

import modal

VOLUME = "unreal-data"
PROJECT_DIR = Path(__file__).resolve().parent.parent / "unreal-project"
SKIP_DIRS = {"Saved", "Intermediate", "DerivedDataCache", "Binaries", ".vs"}


def upload():
    vol = modal.Volume.from_name(VOLUME, create_if_missing=True)
    files = [
        p
        for p in PROJECT_DIR.rglob("*")
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(PROJECT_DIR).parts)
    ]
    with vol.batch_upload(force=True) as batch:
        for p in files:
            batch.put_file(p, f"/project/{p.relative_to(PROJECT_DIR).as_posix()}")
    print(f"Uploaded {len(files)} files from {PROJECT_DIR} to {VOLUME}:/project")


def list_volume():
    vol = modal.Volume.from_name(VOLUME)
    for entry in vol.iterdir("/", recursive=True):
        print(entry.path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list volume contents instead of uploading")
    args = parser.parse_args()
    list_volume() if args.list else upload()
