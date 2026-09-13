"""Checks on the built package, as a modder would receive it.

Unzips the newest dist\\DDS-Tool-*-win64.zip into a fresh folder whose path
has spaces, away from the repository, and then:
  * checks the layout: both programs (window and console subsystems), the
    bundled ImageMagick and texconv with their pinned hashes, LICENSE,
    licenses\\, and only this platform's TkDND files;
  * runs the packaged dds_tool.exe in a hostile environment -- installed
    ImageMagick builds first on PATH, MAGICK_* variables pointing at a
    policy.xml that forbids PNG and DDS, Python removed from PATH, and
    PYTHONHOME/PYTHONPATH pointing at nothing -- so conversions only work
    if the package uses its own ImageMagick and its own Python;
  * starts "DDS Tool.exe" in the same environment and checks that it opens
    its window with Python, Tcl/Tk and TkDND loaded from the package.

For the full CLI comparison, also run:
    python tests\\run_cases.py --exe "dist\\DDS Tool\\dds_tool.exe"

Usage: python tests\\test_package.py [zip]
"""

import ctypes
import ctypes.wintypes
import glob
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import time
import zipfile

import testlib

sys.path.insert(0, os.path.join(testlib.REPO, "tools"))
import fetch_tools  # noqa: E402

failures = []
HOSTILE_POLICY = """<?xml version="1.0" encoding="UTF-8"?>
<policymap>
  <policy domain="coder" rights="none" pattern="{DDS,PNG}" />
</policymap>
"""
GUI_SUBSYSTEM, CONSOLE_SUBSYSTEM = 2, 3


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def subsystem(exe):
    """The PE subsystem field: 2 for a window program, 3 for a console one."""
    with open(exe, "rb") as f:
        data = f.read(4096)
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", data, pe + 24 + 68)[0]


def hostile_env(work):
    policy_dir = os.path.join(work, "hostile policy")
    os.makedirs(policy_dir, exist_ok=True)
    with open(os.path.join(policy_dir, "policy.xml"), "w") as f:
        f.write(HOSTILE_POLICY)
    installed = [
        d for d in glob.glob(r"C:\Program Files\ImageMagick-*")
        if os.path.isfile(os.path.join(d, "magick.exe"))
    ]
    path = [p for p in os.environ["PATH"].split(os.pathsep)
            if p and "python" not in p.lower()]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(installed + path)
    for var in ("MAGICK_HOME", "MAGICK_CONFIGURE_PATH", "MAGICK_CODER_MODULE_PATH"):
        env[var] = policy_dir
    env["PYTHONHOME"] = os.path.join(work, "no such python")
    env["PYTHONPATH"] = os.path.join(work, "no such path")
    env.pop("TEXCONV", None)
    return env, installed


def window_of(pid, title, timeout=20):
    """Wait for a visible top-level window of process pid with this title."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def visit(hwnd, _):
        owner = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(hwnd)
        return True

    end = time.time() + timeout
    while not found and time.time() < end:
        user32.EnumWindows(visit, 0)
        time.sleep(0.2)
    return bool(found)


def loaded_modules(pid):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Id {pid}).Modules | ForEach-Object FileName"],
        capture_output=True, text=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main():
    zips = sorted(glob.glob(os.path.join(testlib.REPO, "dist", "DDS-Tool-*-win64.zip")))
    archive = sys.argv[1] if len(sys.argv) > 1 else (zips[-1] if zips else None)
    if not archive or not os.path.isfile(archive):
        print("error: no package zip; run python tools\\build.py first")
        return 2
    testlib.ensure_fixtures()
    work = os.path.join(testlib.BUILD, "package test", "unzipped here")
    shutil.rmtree(os.path.dirname(work), ignore_errors=True)
    os.makedirs(work)
    with zipfile.ZipFile(archive) as z:
        z.extractall(work)
    pkg = os.path.join(work, "DDS Tool")
    print(f"zip:     {archive}\nunpacked to {pkg}\n")

    # ---- layout ------------------------------------------------------------
    gui_exe = os.path.join(pkg, "DDS Tool.exe")
    cli_exe = os.path.join(pkg, "dds_tool.exe")
    check(subsystem(gui_exe) == GUI_SUBSYSTEM, "DDS Tool.exe is a window program (no console)")
    check(subsystem(cli_exe) == CONSOLE_SUBSYSTEM, "dds_tool.exe is a console program")
    check(sha256(os.path.join(pkg, "imagemagick", "magick.exe")) == fetch_tools.MAGICK_EXE_SHA256,
          "bundled magick.exe matches its pin")
    check(sha256(os.path.join(pkg, "texconv.exe")) == fetch_tools.TEXCONV_SHA256,
          "bundled texconv.exe matches its pin")
    for name in ("LICENSE", os.path.join("licenses", "THIRD_PARTY_NOTICES.txt"),
                 os.path.join("licenses", "ImageMagick-NOTICE.txt"),
                 os.path.join("imagemagick", "LICENSE.txt"), os.path.join("imagemagick", "policy.xml")):
        check(os.path.isfile(os.path.join(pkg, name)), f"{name} included")
    tkdnd = os.listdir(os.path.join(pkg, "_internal", "tkinterdnd2", "tkdnd"))
    check(tkdnd == ["win-x64"], f"only this platform's TkDND files ({tkdnd})")
    extras = [n for n in os.listdir(os.path.join(pkg, "imagemagick"))
              if n.endswith(".exe") and n != "magick.exe"]
    check(not extras, "no duplicate ImageMagick programs")

    # ---- the command line in a hostile environment ----------------------------
    env, installed = hostile_env(os.path.dirname(work))
    print(f"  (installed ImageMagick first on PATH: {installed or 'none'})")
    data = os.path.join(os.path.dirname(work), "some textures")
    shutil.copytree(os.path.join(testlib.FIXTURES, "conv"), data)
    proc = subprocess.run(
        [cli_exe, "convert", os.path.join(data, "opaque.png"), os.path.join(data, "trans.png"),
         "--bc7"],
        env=env, capture_output=True, text=True, cwd=os.path.dirname(work),
    )
    check(proc.returncode == 0 and "Converted 2 of 2 file(s) (1 DXT1, 1 BC7)." in proc.stdout,
          "packaged CLI converts PNG to DXT1 and BC7 despite the hostile policy")
    outputs = [os.path.join(data, "opaque.dds"), os.path.join(data, "trans.dds")]
    proc = subprocess.run([cli_exe, "audit", *outputs, "--bc7"], env=env, capture_output=True,
                          text=True, cwd=os.path.dirname(work))
    check(proc.returncode == 0 and "Audited 2 DDS file(s); 0 with issues" in proc.stdout,
          "packaged CLI audits its two outputs as clean")

    # ---- the window ------------------------------------------------------------
    gui = subprocess.Popen([gui_exe, data], env=env, cwd=os.path.dirname(work))
    try:
        check(window_of(gui.pid, "DDS Tool"), "DDS Tool.exe opens its window")
        time.sleep(1)
        check(gui.poll() is None, "window stays open")
        modules = loaded_modules(gui.pid)
        norm_pkg = os.path.normcase(pkg)
        for dll in ("python312.dll", "tcl86t.dll", "tk86t.dll", "libtkdnd2.10.2.dll"):
            hits = [m for m in modules if os.path.basename(m).lower() == dll]
            check(len(hits) == 1 and os.path.normcase(hits[0]).startswith(norm_pkg),
                  f"{dll} loaded from the package ({hits})")
        foreign = [m for m in modules if "python3" in os.path.basename(m).lower()
                   and not os.path.normcase(m).startswith(norm_pkg)]
        check(not foreign, f"no Python DLL loaded from outside the package ({foreign})")
    finally:
        gui.kill()
        gui.wait()

    print()
    print("=" * 72)
    print(f"{len(failures)} failure(s)")
    for what in failures:
        print(f"  {what}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
