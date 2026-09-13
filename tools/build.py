#!/usr/bin/env python3
"""Build the DDS Tool package: dist\\DDS Tool\\ and a zip of it.

  1. PyInstaller builds dds_tool.spec into dist\\DDS Tool\\: "DDS Tool.exe"
     (the window), dds_tool.exe (the command line) and _internal\\ (the
     Python runtime they share).
  2. tools\\fetch_tools.py puts the verified ImageMagick and texconv beside
     them, where dds_tool.app_dir() looks first.
  3. LICENSE, licenses\\ and README.txt (once it exists) are copied in.
  4. The folder is zipped as dist\\DDS-Tool-<yyyymmdd>-win64.zip, with
     "DDS Tool\\" as its top folder.

Test the result with tests\\test_package.py and
"tests\\run_cases.py --exe "dist\\DDS Tool\\dds_tool.exe"".

Needs: pip install -r requirements-build.txt

Usage: python tools\\build.py [--no-zip]
"""

import argparse
import datetime
import importlib.util
import os
import shutil
import subprocess
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOLS)
DIST = os.path.join(REPO, "dist")
WORK = os.path.join(REPO, "build")
SPEC = os.path.join(REPO, "dds_tool.spec")
PACKAGE = os.path.join(DIST, "DDS Tool")
EXTRAS = ["LICENSE", "README.txt"]  # copied into the package when present

sys.path.insert(0, TOOLS)
import fetch_tools  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Build the DDS Tool package.")
    parser.add_argument("--no-zip", action="store_true", help="skip making the zip")
    args = parser.parse_args()
    for module in ("PyInstaller", "tkinterdnd2"):
        if importlib.util.find_spec(module) is None:
            print(f"error: {module} is not installed; pip install -r requirements-build.txt")
            return 2

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", DIST, "--workpath", WORK, SPEC],
        cwd=REPO, check=True,
    )
    fetch_tools.fetch_magick(PACKAGE, force=False)
    fetch_tools.fetch_texconv(PACKAGE, force=False)
    for name in EXTRAS:
        if os.path.isfile(os.path.join(REPO, name)):
            shutil.copy2(os.path.join(REPO, name), PACKAGE)
    shutil.copytree(os.path.join(REPO, "licenses"), os.path.join(PACKAGE, "licenses"),
                    dirs_exist_ok=True)

    size = sum(
        os.path.getsize(os.path.join(root, name))
        for root, _, names in os.walk(PACKAGE) for name in names
    )
    print()
    print("=" * 72)
    print(f"Built {PACKAGE} ({size / 2**20:.1f} MB)")
    if not args.no_zip:
        stamp = datetime.date.today().strftime("%Y%m%d")
        base = os.path.join(DIST, f"DDS-Tool-{stamp}-win64")
        archive = shutil.make_archive(base, "zip", root_dir=DIST, base_dir="DDS Tool")
        print(f"Zipped to {archive} ({os.path.getsize(archive) / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
