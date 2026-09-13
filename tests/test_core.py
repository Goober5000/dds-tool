"""Tests for dds_tool's core API, the part the GUI builds on.

Covers FileResult contents, the progress callback, cancelling from a worker
thread, temp-folder cleanup, CSV rows, BC7 accuracy (PSNR and corner
orientation), tool lookup in a frozen build, and the subprocess flags.

Usage: python tests\\test_core.py
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading

import testlib
from testlib import dds_tool as t

failures = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def fresh():
    work = tempfile.mkdtemp(prefix="core_", dir=testlib.BUILD)
    shutil.copytree(testlib.FIXTURES, work, dirs_exist_ok=True)
    return work


def temp_folders():
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dds_tool_")}


def test_convert_results(magick, texconv):
    work = fresh()
    files, _ = t.expand_inputs([os.path.join(work, "conv")], t.IMAGE_EXTS, True)
    calls = []
    results = list(t.convert_files(
        files, magick, texconv, bc7=True,
        progress=lambda n, total, p: calls.append((n, total, p)),
    ))
    check(len(results) == len(files) == 15, f"one result per file ({len(results)})")
    check([c[0] for c in calls] == list(range(1, 16)), "progress n counts 1..N")
    check(all(c[1] == 15 for c in calls), "progress total is N")
    check([c[2] for c in calls] == files, "progress path matches file order")
    by = {os.path.basename(r.path): r for r in results}
    r = by["trans.png"]
    check(r.status == t.CONVERTED and r.format == "BC7" and r.mips == 7,
          "trans.png -> BC7, 7 mips")
    check(r.has_alpha and r.min_alpha == 0 and os.path.isfile(r.output_path),
          "trans.png alpha 0, output written")
    check(by["opaque.png"].format == "DXT1" and by["opaque.png"].min_alpha is None,
          "opaque.png DXT1, min_alpha None")
    check(by["alpha_opaque.png"].suggestion.startswith("alpha is effectively opaque"),
          "alpha_opaque.png suggestion")
    check(by["npot.png"].status == t.SKIPPED and by["npot.png"].skip_reason == t.SKIP_NOT_POW2,
          "npot.png skipped")
    check(by["exists.png"].skip_reason == t.SKIP_EXISTS, "exists.png skipped")
    d = by["dup.tga"]
    check(d.skip_reason == t.SKIP_SAME_OUTPUT and os.path.basename(d.first_input) == "dup.png",
          "dup.tga collision names dup.png")
    check(by["broken.png"].status == t.ERROR and "improper image header" in by["broken.png"].message,
          "broken.png error")
    check(by["anim.gif"].frames == 3, "anim.gif frames 3")
    shutil.rmtree(work)


def test_bc7_accuracy(magick, texconv):
    work = fresh()
    src = os.path.join(work, "conv", "trans.png")
    [r] = t.convert_files([src], magick, texconv, bc7=True)
    subprocess.run(
        [texconv, "-nologo", "-y", "-ft", "png", "-f", "R8G8B8A8_UNORM", "-m", "1",
         "-o", work, r.output_path],
        check=True, capture_output=True,
    )
    back = os.path.join(work, "trans.png")
    cmp = subprocess.run(
        [magick, "compare", "-metric", "PSNR", src, back, "null:"],
        capture_output=True, text=True,
    )
    psnr = float(cmp.stderr.split()[0])
    check(psnr >= 45, f"BC7 round trip PSNR {psnr:.1f} dB >= 45")
    corners = subprocess.run(
        [magick, back, "-format", "%[pixel:p{0,0}] %[pixel:p{63,31}]", "info:"],
        capture_output=True, text=True,
    ).stdout.split()
    check(len(corners) == 2 and "255,0,0" in corners[0] and "0,0,255" in corners[1],
          f"BC7 corners not flipped ({' '.join(corners)})")
    shutil.rmtree(work)


def test_cancel(magick, texconv):
    work = fresh()
    files, _ = t.expand_inputs([os.path.join(work, "conv")], t.IMAGE_EXTS, True)
    before = temp_folders()
    cancel = threading.Event()
    q = queue.Queue()

    def progress(n, total, path):
        if n == 4:
            cancel.set()  # as if Cancel were pressed while file 4 runs

    def worker():
        for res in t.convert_files(
            files, magick, texconv, dry_run=True, progress=progress, cancel=cancel
        ):
            q.put(res)
        q.put(None)

    th = threading.Thread(target=worker)
    th.start()
    th.join(120)
    got = []
    while (item := q.get_nowait()) is not None:
        got.append(item)
    check(not th.is_alive(), "worker thread finished")
    check(len(got) == 4, f"cancel during file 4 stops after it ({len(got)} results)")
    check(temp_folders() == before, "cancelled batch removes its temp folder")

    cancel = threading.Event()
    cancel.set()
    check(list(t.audit_files(files, magick, texconv, cancel=cancel)) == [],
          "cancel set beforehand yields nothing")

    gen = t.convert_files(files, magick, texconv, dry_run=True)
    next(gen)
    gen.close()
    check(temp_folders() == before, "abandoned generator removes its temp folder")
    shutil.rmtree(work)


def test_audit_and_csv(magick):
    work = fresh()
    files, _ = t.expand_inputs([os.path.join(work, "aud")], t.DDS_EXTS, False)
    results = list(t.audit_files(files, magick, None))
    by = {os.path.basename(r.path): r for r in results}
    check(by["notdds.dds"].status == t.ISSUES and by["notdds.dds"].width is None,
          "notdds.dds unreadable, no header fields")
    check(by["opaque_bc7.dds"].alpha_note == "BC7 needs texconv to read",
          "BC7 gets an alpha_note without texconv")
    check(by["dxt1_good.dds"].status == t.OK and by["dxt1_good.dds"].mips == 7,
          "dxt1_good.dds ok")
    check(by["opaque_dxt5.dds"].status == t.OK and by["opaque_dxt5.dds"].suggestion,
          "opaque_dxt5.dds ok with a suggestion")
    n = t.write_audit_csv(os.path.join(work, "x.csv"), results)
    check(n == 9, f"CSV rows {n} (8 with issues + opaque_dxt5; BC7 unread)")
    shutil.rmtree(work)


def test_lookup(magick):
    check(t.app_dir() == testlib.REPO, "app_dir is the script folder")
    fake = tempfile.mkdtemp(prefix="frozen_", dir=testlib.BUILD)
    os.makedirs(os.path.join(fake, "imagemagick"))
    for name in (os.path.join("imagemagick", "magick.exe"), "texconv.exe", "DDS Tool.exe"):
        open(os.path.join(fake, name), "wb").close()
    saved_env = os.environ.pop("TEXCONV", None)
    sys.frozen, saved_exe = True, sys.executable
    sys.executable = os.path.join(fake, "DDS Tool.exe")
    try:
        check(t.app_dir() == fake, "frozen app_dir is the exe folder")
        check(t.find_magick() == os.path.join(fake, "imagemagick", "magick.exe"),
              f"bundled magick wins over PATH ({shutil.which('magick')})")
        check(t.find_texconv() == os.path.join(fake, "texconv.exe"), "bundled texconv found")
        check(t.find_magick(magick) == magick, "--magick override still wins")
    finally:
        del sys.frozen
        sys.executable = saved_exe
        if saved_env is not None:
            os.environ["TEXCONV"] = saved_env
        shutil.rmtree(fake)
    if not os.path.isdir(os.path.join(testlib.REPO, "imagemagick")):
        check(t.find_magick() in (shutil.which("magick"), t.KNOWN_MAGICK),
              "without a bundled copy, falls back to PATH or the default install")


def test_run_flags(magick):
    seen = {}
    real = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    t.subprocess.run = spy
    try:
        t.run([magick, "-version"])
    finally:
        t.subprocess.run = real
    check(seen.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0),
          "run() passes CREATE_NO_WINDOW")
    check(seen.get("stdin") == subprocess.DEVNULL, "run() gives tools no stdin")


def main():
    magick, texconv = testlib.require_tools()
    testlib.ensure_fixtures()
    print(f"magick:  {magick}\ntexconv: {texconv}\n")
    test_convert_results(magick, texconv)
    test_bc7_accuracy(magick, texconv)
    test_cancel(magick, texconv)
    test_audit_and_csv(magick)
    test_lookup(magick)
    test_run_flags(magick)

    print()
    print("=" * 72)
    print(f"{len(failures)} failure(s)")
    for what in failures:
        print(f"  {what}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
