#!/usr/bin/env python3
"""Download the external tools that DDS Tool bundles, and verify them.

  * ImageMagick 7.1.2-31 portable Q16-HDRI x64 goes to imagemagick\\: its
    magick.exe, configuration files (*.xml, sRGB.icc), LICENSE.txt and
    NOTICE.txt.  The portable build is a single static program that needs
    no installer or registry entries.  The archive's other programs
    (convert.exe, identify.exe, ...) are identical copies of magick.exe and
    are left out, as is its ChangeLog.  It contains no Ghostscript.
  * texconv.exe from DirectXTex release may2026 (2026.5.8.1) goes beside it.

Every download is checked against a pinned SHA-256.  When the pins were
set, the ImageMagick archive was checked against its signed build
provenance (gh attestation verify --repo ImageMagick/ImageMagick), and both
programs carried valid Authenticode signatures (ImageMagick Studio LLC and
Microsoft Corporation).  Tools already in place with the right hash are
not downloaded again.  ImageMagick's LICENSE.txt and NOTICE.txt are also
copied into licenses\\, so the notices always match the bundled version.

The .7z archive is unpacked with the tar.exe that ships with Windows 10
and 11 (Git's GNU tar cannot read 7z, so PATH is not searched).

Usage: python tools\\fetch_tools.py [--dest DIR] [--force]
       --dest    install into DIR (default: the repository root)
       --force   download again even if the tools are already in place
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LICENSES = os.path.join(REPO, "licenses")

MAGICK_VERSION = "7.1.2-31"
MAGICK_URL = (
    f"https://github.com/ImageMagick/ImageMagick/releases/download/{MAGICK_VERSION}/"
    f"ImageMagick-{MAGICK_VERSION}-portable-Q16-HDRI-x64.7z"
)
MAGICK_SHA256 = "a6a83a77a5284a2cae5ca4a81d95e5fad21ecd56cdb647ee99f970e233504fff"
MAGICK_EXE_SHA256 = "6b6bd55206f23ed02738deda301c3c758738bb4d328c1c03c9a7ae778c3f960d"

TEXCONV_URL = "https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe"
TEXCONV_SHA256 = "dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06"

TAR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def has_hash(path, expected):
    return os.path.isfile(path) and sha256(path) == expected


def download(url, dest, expected):
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    actual = sha256(dest)
    if actual != expected:
        os.remove(dest)
        raise SystemExit(
            f"error: {os.path.basename(url)} has SHA-256 {actual}, expected {expected}"
        )


def keep_from_magick(name):
    return name in ("magick.exe", "LICENSE.txt", "NOTICE.txt") or name.endswith((".xml", ".icc"))


def fetch_magick(dest, force):
    target = os.path.join(dest, "imagemagick")
    exe = os.path.join(target, "magick.exe")
    if not force and has_hash(exe, MAGICK_EXE_SHA256):
        print(f"ImageMagick {MAGICK_VERSION}: already in {target}")
    else:
        print(f"ImageMagick {MAGICK_VERSION}:")
        with tempfile.TemporaryDirectory(prefix="fetch_tools_") as tmp:
            archive = os.path.join(tmp, "imagemagick.7z")
            download(MAGICK_URL, archive, MAGICK_SHA256)
            unpacked = os.path.join(tmp, "unpacked")
            os.makedirs(unpacked)
            subprocess.run([TAR, "-xf", archive, "-C", unpacked], check=True)
            if not has_hash(os.path.join(unpacked, "magick.exe"), MAGICK_EXE_SHA256):
                raise SystemExit("error: magick.exe in the archive does not match its pin")
            shutil.rmtree(target, ignore_errors=True)
            os.makedirs(target)
            for name in sorted(os.listdir(unpacked)):
                if keep_from_magick(name):
                    shutil.copy2(os.path.join(unpacked, name), target)
        print(f"  installed in {target}")

    os.makedirs(LICENSES, exist_ok=True)
    for name in ("LICENSE.txt", "NOTICE.txt"):
        shutil.copyfile(os.path.join(target, name), os.path.join(LICENSES, f"ImageMagick-{name}"))
    out = subprocess.run([exe, "-version"], capture_output=True, text=True).stdout
    first = out.splitlines()[0] if out else "(no output)"
    if f"ImageMagick {MAGICK_VERSION} " not in first:
        raise SystemExit(f"error: {exe} reports {first!r}")
    print(f"  {first}")


def fetch_texconv(dest, force):
    exe = os.path.join(dest, "texconv.exe")
    if not force and has_hash(exe, TEXCONV_SHA256):
        print(f"texconv: already in {dest}")
        return
    print("texconv:")
    with tempfile.TemporaryDirectory(prefix="fetch_tools_") as tmp:
        downloaded = os.path.join(tmp, "texconv.exe")
        download(TEXCONV_URL, downloaded, TEXCONV_SHA256)
        shutil.copy2(downloaded, exe)
    print(f"  installed {exe}")


def main():
    parser = argparse.ArgumentParser(description="Download and verify the bundled tools.")
    parser.add_argument("--dest", default=REPO, help="install into DIR (default: repository root)")
    parser.add_argument("--force", action="store_true", help="download even if already in place")
    args = parser.parse_args()
    if not os.path.isfile(TAR):
        print(f"error: {TAR} not found; it is needed to unpack the .7z archive")
        return 2
    os.makedirs(args.dest, exist_ok=True)
    fetch_magick(args.dest, args.force)
    fetch_texconv(args.dest, args.force)
    print()
    print("=" * 72)
    print(f"Tools ready in {os.path.abspath(args.dest)}; notices refreshed in {LICENSES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
