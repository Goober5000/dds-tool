"""Regression test for the dds_tool command line.

Runs each case against a fresh copy of the fixtures and records the exact
stdout, stderr, exit code, any CSV written, and a hash of every file left
in the work folder.  The report is compared with expected_cases.txt.

The expected report was recorded with ImageMagick 7.1.2-31 Q16-HDRI and
texconv 2026.5.8.1.  Other versions can change error text and output
hashes, so check such differences before accepting them with --update.

With --exe, the cases run a built dds_tool.exe instead of the script, to
check the frozen program behaves identically.  Its argparse errors name
dds_tool.exe rather than dds_tool.py, so the program name and spacing of
stderr lines are normalized on both sides before comparing.

Usage: python tests\\run_cases.py [--update] [--exe PATH]
       --update   replace expected_cases.txt with this run's report
       --exe      run PATH (e.g. "dist\\DDS Tool\\dds_tool.exe") instead
"""

import argparse
import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys

import testlib

TOOL = os.path.join(testlib.REPO, "dds_tool.py")
EXPECTED = os.path.join(testlib.TESTS, "expected_cases.txt")
REPORT = os.path.join(testlib.BUILD, "cases.txt")
WORK = os.path.join(testlib.BUILD, "work")

CASES = [
    ("conv-folder", ["convert", "conv"]),
    ("conv-recursive", ["convert", "conv", "-r"]),
    ("conv-bc7", ["convert", "conv", "-r", "--bc7"]),
    ("conv-dry", ["convert", "conv", "-r", "--dry-run"]),
    ("conv-dry-bc7", ["convert", "conv", "-r", "--dry-run", "--bc7"]),
    ("conv-outdir-force", ["convert", "conv", "-r", "--out-dir", "out dir", "--force"]),
    ("conv-force", ["convert", "conv", "--force"]),
    ("conv-opaque250", ["convert", "conv", "--opaque-alpha", "250"]),
    ("conv-explicit", [
        "convert", r"conv\exists.dds", r"conv\broken.png", r"conv\*.png",
        "nomatch*.png", r"conv\exists.png", r"conv\trans.png", "missingfile.png",
        "conv\\sub dir",
    ]),
    ("conv-good-only", ["convert", r"conv\opaque.png", r"conv\trans.png", "--bc7"]),
    ("conv-bc7-notexconv", ["convert", "conv", "--bc7", "--texconv", "missing.exe"]),
    ("conv-dry-bc7-notexconv", [
        "convert", "conv", "--bc7", "--dry-run", "--texconv", "missing.exe",
    ]),
    ("conv-nomagick", ["convert", "conv", "--magick", "missing.exe"]),
    ("aud-folder", ["audit", "aud"]),
    ("aud-bc7-csv", ["audit", "aud", "--bc7", "--csv", "report.csv"]),
    ("aud-csv-252", ["audit", "aud", "-r", "--csv", "report.csv", "--opaque-alpha", "252"]),
    ("aud-nomatch", ["audit", "aud", "nomatch*.dds", r"conv\*.dds"]),
    ("aud-notexconv", ["audit", "aud", "--texconv", "missing.exe"]),
    ("aud-clean", ["audit", r"aud\dxt1_good.dds", "aud\\sub dir"]),
    ("aud-nomagick", ["audit", "aud", "--magick", "missing.exe"]),
    ("aud-badalpha", ["audit", "aud", "--opaque-alpha", "300"]),
]


def snapshot(root):
    lines = []
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            p = os.path.join(dirpath, name)
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()[:16]
            lines.append(f"    {os.path.relpath(p, root)}  {digest}")
    return lines


def normalize_stderr(report):
    """Make argparse's stderr independent of the program's file name."""
    out, in_stderr = [], False
    for line in report.splitlines():
        if line.startswith("--- "):
            in_stderr = line == "--- stderr"
        elif in_stderr:
            line = re.sub(r" +", " ", line.replace("dds_tool.exe", "dds_tool.py")).strip()
        out.append(line)
    return "\n".join(out) + "\n"


def run_cases(magick, command):
    # dds_tool finds magick on PATH (unless a bundled copy exists) and
    # texconv in its own folder; TEXCONV would override the latter.
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(magick) + os.pathsep + env["PATH"]
    env.pop("TEXCONV", None)
    out = []
    for name, args in CASES:
        work = os.path.join(WORK, name)
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(testlib.FIXTURES, work)
        proc = subprocess.run(
            [*command, *args], cwd=work, env=env, capture_output=True, text=True,
        )
        out.append(f"===== {name}: {' '.join(args)}")
        out.append(f"--- exit {proc.returncode}")
        out.append("--- stdout")
        out.append(proc.stdout.rstrip("\n"))
        if proc.stderr.strip():
            out.append("--- stderr")
            out.append(proc.stderr.rstrip("\n"))
        csv_path = os.path.join(work, "report.csv")
        if os.path.exists(csv_path):
            out.append("--- csv")
            with open(csv_path, encoding="utf-8") as f:
                out.append(f.read().rstrip("\n"))
        out.append("--- files")
        out.extend(snapshot(work))
        out.append("")
        print(f"  {name}: exit {proc.returncode}")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Regression test for the dds_tool CLI.")
    parser.add_argument(
        "--update", action="store_true", help="replace expected_cases.txt with this run"
    )
    parser.add_argument("--exe", metavar="PATH", help="run a built dds_tool.exe instead")
    args = parser.parse_args()
    if args.exe and args.update:
        parser.error("--update records the script's output; do not combine it with --exe")

    magick, texconv = testlib.require_tools()
    testlib.ensure_fixtures()
    command = [os.path.abspath(args.exe)] if args.exe else [sys.executable, TOOL]
    print(f"running: {command[-1]}")
    if not args.exe:
        print(f"magick:  {magick}\ntexconv: {texconv}")
    print()
    report = run_cases(magick, command)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(report)

    print()
    print("=" * 72)
    if args.update or (not args.exe and not os.path.exists(EXPECTED)):
        with open(EXPECTED, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Wrote {len(CASES)} case(s) to {EXPECTED}")
        return 0
    with open(EXPECTED, encoding="utf-8") as f:
        expected = f.read()
    if args.exe:
        report, expected = normalize_stderr(report), normalize_stderr(expected)
    if report == expected:
        print(f"All {len(CASES)} case(s) match {os.path.basename(EXPECTED)}.")
        return 0
    diff = difflib.unified_diff(
        expected.splitlines(), report.splitlines(),
        "expected_cases.txt", "this run", lineterm="",
    )
    print("\n".join(diff))
    print(f"\nOutput differs from {os.path.basename(EXPECTED)}; full report in {REPORT}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
