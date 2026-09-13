#!/usr/bin/env python3
"""Batch-convert images to DDS, and audit existing DDS files.

Conversion rules:
  * Both sides must be a power of 2 (non-square is fine, e.g. 512x256).
    Anything else is skipped and listed at the end.
  * An image without an alpha channel is written as DXT1.  An image with an
    alpha channel is written as DXT5, or as BC7 with --bc7 -- whatever the
    alpha values are.
  * Every output gets a full mipmap chain down to 1x1.

The audit applies the same rules to existing DDS files, judging by format
alone, and lists each file that breaks one: a size that is not a power of
2, missing or partial mipmaps, a DXT1 with an alpha channel (its alpha flag
set), a DXT5 when auditing with --bc7, or a format other than DXT1/DXT5/BC7.
Without --bc7, BC7 is accepted in place of DXT5.

Both commands also make suggestions, which are not counted as problems: an
alpha channel whose lowest value is at least --opaque-alpha (default 248)
is effectively opaque, so the image could drop it and use DXT1.  The
default leaves room for BC7, which stores fully opaque alpha as low as 251.

DXT1 and DXT5 are encoded with ImageMagick.  BC7 needs Microsoft's texconv
(https://github.com/microsoft/DirectXTex/releases), which the audit also
uses to read BC7 alpha values.  texconv is looked up via --texconv, the
TEXCONV environment variable, this script's folder, then PATH.

Usage: python dds_tool.py convert <files|wildcards|folders>... [--bc7] [-r]
                          [--out-dir DIR] [--force] [--dry-run]
                          [--opaque-alpha N]
       python dds_tool.py audit <files|wildcards|folders>... [--bc7] [-r]
                          [--csv FILE] [--opaque-alpha N]

Quote wildcards ("textures\\*.png"); the script expands them itself.
Folders are scanned one level deep unless -r is given.
"""

import argparse
import collections
import csv
import glob
import os
import shutil
import struct
import subprocess
import sys
import tempfile

IMAGE_EXTS = {
    ".png", ".tga", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff", ".pcx", ".gif",
    ".webp",
}
DDS_EXTS = {".dds"}
KNOWN_MAGICK = r"C:\Program Files\ImageMagick-7.1.2-Q16-HDRI\magick.exe"
TEXCONV_URL = "https://github.com/microsoft/DirectXTex/releases"
DEFAULT_OPAQUE_ALPHA = 248

# DDS header fields (see Microsoft's DDS_HEADER / DDS_PIXELFORMAT docs)
DDPF_ALPHAPIXELS = 0x1
DDPF_ALPHA = 0x2
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_LUMINANCE = 0x20000
DDSCAPS2_CUBEMAP = 0x200
DDS_RESOURCE_MISC_TEXTURECUBE = 0x4

LEGACY_FOURCC = {
    "DXT1": "DXT1", "DXT2": "DXT2", "DXT3": "DXT3", "DXT4": "DXT4",
    "DXT5": "DXT5", "ATI1": "BC4", "BC4U": "BC4", "BC4S": "BC4",
    "ATI2": "BC5", "BC5U": "BC5", "BC5S": "BC5",
}
DXGI_FORMATS = {
    70: "DXT1", 71: "DXT1", 72: "DXT1",
    73: "DXT3", 74: "DXT3", 75: "DXT3",
    76: "DXT5", 77: "DXT5", 78: "DXT5",
    79: "BC4", 80: "BC4", 81: "BC4",
    82: "BC5", 83: "BC5", 84: "BC5",
    94: "BC6H", 95: "BC6H", 96: "BC6H",
    97: "BC7", 98: "BC7", 99: "BC7",
    27: "uncompressed", 28: "uncompressed", 29: "uncompressed",
    87: "uncompressed", 88: "uncompressed", 90: "uncompressed",
    91: "uncompressed", 92: "uncompressed", 93: "uncompressed",
}
DXGI_NO_ALPHA = {88, 92, 93}  # B8G8R8X8 variants
ALWAYS_ALPHA = {"DXT2", "DXT3", "DXT4", "DXT5", "BC7"}
ALLOWED_FORMATS = ("DXT1", "DXT5", "BC7")
MAGICK_DECODES = {"DXT3", "DXT5", "uncompressed"}  # legacy headers only

AUDIT_CATEGORIES = [
    "unreadable",
    "not power-of-2",
    "missing mipmaps",
    "has alpha channel, should be DXT5",
    "has alpha channel, should be BC7",
    "unexpected format",
]


class ToolError(RuntimeError):
    pass


def find_magick(override=None):
    if override:
        return override if os.path.isfile(override) else None
    for candidate in (shutil.which("magick"), KNOWN_MAGICK):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def find_texconv(override=None):
    if override:
        return override if os.path.isfile(override) else None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.environ.get("TEXCONV"),
        os.path.join(script_dir, "texconv.exe"),
        shutil.which("texconv"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def run(cmd):
    """Run an external tool and return its stdout; raise ToolError on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        raise ToolError(f"cannot run {os.path.basename(cmd[0])}: {exc}")
    if proc.returncode != 0:
        lines = [
            line.strip()
            for line in (proc.stderr + "\n" + proc.stdout).splitlines()
            if line.strip()
        ]
        failed = [line for line in lines if "FAILED" in line]  # texconv
        msg = (failed or lines or [f"exit code {proc.returncode}"])[0]
        raise ToolError(msg)
    return proc.stdout


def expand_inputs(args, exts, recursive):
    """Expand named files, wildcards and folders into a list of files.

    Returns (files, unmatched) where unmatched lists arguments that named
    nothing.  Named files are taken whatever their extension; folders and
    wildcards only yield files whose extension is in exts.
    """
    files, unmatched, seen = [], [], set()

    def add(path):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            files.append(path)

    def wanted(path):
        return os.path.splitext(path)[1].lower() in exts

    def scan(folder):
        found = 0
        if recursive:
            for root, dirs, names in os.walk(folder):
                dirs.sort()
                for name in sorted(names):
                    if wanted(name):
                        add(os.path.join(root, name))
                        found += 1
        else:
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if os.path.isfile(path) and wanted(name):
                    add(path)
                    found += 1
        return found

    for arg in args:
        if os.path.isdir(arg):
            if not scan(arg):
                unmatched.append(arg)
        elif os.path.isfile(arg):
            add(arg)
        elif any(c in arg for c in "*?["):
            found = 0
            for match in sorted(glob.glob(arg, recursive=True)):
                if os.path.isdir(match):
                    found += scan(match)
                elif wanted(match):
                    add(match)
                    found += 1
            if not found:
                unmatched.append(arg)
        else:
            unmatched.append(arg)
    return files, unmatched


def is_pow2(n):
    return n > 0 and n & (n - 1) == 0


def mip_levels(width, height):
    """Levels in a full mip chain, counting the full-size image."""
    return max(width, height, 1).bit_length()


def choose_format(has_alpha, bc7):
    if not has_alpha:
        return "DXT1"
    return "BC7" if bc7 else "DXT5"


def probe_image(magick, path):
    """Return (width, height, has_alpha, min_alpha, frames) for frame 0.

    min_alpha is the lowest alpha value on a 0-255 scale (255 when the image
    has no alpha channel).
    """
    out = run([magick, "identify", "-format", "%w %h %A %[fx:minima.a]\n", path])
    rows = [line.split() for line in out.splitlines() if line.strip()]
    if not rows or len(rows[0]) != 4:
        raise ToolError(f"unexpected identify output: {out.strip()[:80]!r}")
    width, height, alpha_trait, min_alpha = rows[0]
    has_alpha = alpha_trait.lower() not in ("undefined", "false")
    return int(width), int(height), has_alpha, round(float(min_alpha) * 255), len(rows)


def encode_dxt(magick, src, fmt, levels, tmpdir):
    out = os.path.join(tmpdir, "encoded.dds")
    cmd = [magick, src + "[0]"]
    if fmt == "DXT1":
        cmd += ["-alpha", "off"]
    cmd += [
        "-define", f"dds:compression={fmt.lower()}",
        "-define", f"dds:mipmaps={levels - 1}",
        "DDS:" + out,
    ]
    run(cmd)
    return out


def encode_bc7(magick, texconv, src, tmpdir):
    # Normalize every input to 32-bit TGA first: texconv reads it directly,
    # and TGA carries no gamma metadata that could trigger an sRGB conversion.
    # TrueColorAlpha stops ImageMagick writing palette sources as a
    # colour-mapped TGA, which texconv cannot read.
    tga = os.path.join(tmpdir, "bc7src.tga")
    run([magick, src + "[0]", "-alpha", "set", "-type", "TrueColorAlpha", "TGA:" + tga])
    run([texconv, "-nologo", "-y", "-f", "BC7_UNORM", "-m", "0", "-o", tmpdir, tga])
    out = os.path.join(tmpdir, "bc7src.dds")
    if not os.path.isfile(out):
        raise ToolError("texconv reported success but wrote no file")
    return out


def install(tmp_file, dest):
    """Copy tmp_file beside dest, then swap it in, so dest is never half-written."""
    staging = dest + ".tmp"
    shutil.copyfile(tmp_file, staging)
    os.replace(staging, dest)


def read_dds_header(path):
    """Parse the DDS header.  Returns a dict, or raises ToolError."""
    with open(path, "rb") as f:
        data = f.read(148)
    if data[:4] != b"DDS ":
        raise ToolError("not a DDS file")
    if len(data) < 128:
        raise ToolError("truncated header")
    _flags, height, width, _pitch, _depth, mips = struct.unpack_from("<6I", data, 8)
    pf_flags, fourcc_raw = struct.unpack_from("<I4s", data, 80)
    caps2 = struct.unpack_from("<I", data, 112)[0]
    cubemap = bool(caps2 & DDSCAPS2_CUBEMAP)
    alpha_flag = bool(pf_flags & (DDPF_ALPHAPIXELS | DDPF_ALPHA))
    dx10 = False

    if pf_flags & DDPF_FOURCC:
        fourcc = fourcc_raw.decode("ascii", "replace")
        if fourcc == "DX10":
            if len(data) < 148:
                raise ToolError("truncated DX10 header")
            dx10 = True
            dxgi, _dim, misc = struct.unpack_from("<3I", data, 128)
            fmt = DXGI_FORMATS.get(dxgi, f"DXGI:{dxgi}")
            cubemap = cubemap or bool(misc & DDS_RESOURCE_MISC_TEXTURECUBE)
            if fmt == "uncompressed":
                alpha_flag = dxgi not in DXGI_NO_ALPHA
        elif fourcc in LEGACY_FOURCC:
            fmt = LEGACY_FOURCC[fourcc]
        elif fourcc.isprintable() and fourcc.strip():
            fmt = f"FourCC:{fourcc}"
        else:  # D3DFMT code, e.g. 113 = A16B16G16R16F
            fmt = f"D3DFMT:{struct.unpack('<I', fourcc_raw)[0]}"
    elif pf_flags & (DDPF_RGB | DDPF_LUMINANCE | DDPF_ALPHA):
        fmt = "uncompressed"
    else:
        fmt = "unknown"

    if fmt in ALWAYS_ALPHA:
        has_alpha = True
    elif fmt in ("DXT1", "uncompressed"):
        has_alpha = alpha_flag
    else:
        has_alpha = False

    return {
        "width": width,
        "height": height,
        "mips": max(mips, 1),
        "format": fmt,
        "dx10": dx10,
        "cubemap": cubemap,
        "has_alpha": has_alpha,
    }


def dxt1_has_transparency(path, header):
    """True if any top-level DXT1 block encodes a transparent texel.

    A DXT1 block is in 3-colour mode when color0 <= color1, and index 3 then
    means transparent black.  ImageMagick reads such files as opaque, so the
    blocks are inspected directly.
    """
    blocks = max(1, (header["width"] + 3) // 4) * max(1, (header["height"] + 3) // 4)
    with open(path, "rb") as f:
        f.seek(148 if header["dx10"] else 128)
        data = f.read(blocks * 8)
    data = data[: len(data) // 8 * 8]
    for color0, color1, indices in struct.iter_unpack("<HHI", data):
        # a 2-bit index is 3 when both of its bits are set
        if color0 <= color1 and indices & (indices >> 1) & 0x55555555:
            return True
    return False


def dds_min_alpha(magick, texconv, path, header, tmpdir):
    """Return (min_alpha, note): lowest alpha on a 0-255 scale, or None."""
    if header["format"] == "DXT1":
        return (0 if dxt1_has_transparency(path, header) else 255), None
    if not header["dx10"] and header["format"] in MAGICK_DECODES:
        target, reader = path, "ImageMagick"
    elif texconv:
        run([
            texconv, "-nologo", "-y", "-ft", "tga", "-f", "R8G8B8A8_UNORM",
            "-m", "1", "-o", tmpdir, path,
        ])
        target = os.path.join(
            tmpdir, os.path.splitext(os.path.basename(path))[0] + ".tga"
        )
        if not os.path.isfile(target):
            raise ToolError("texconv reported success but wrote no file")
        reader = "texconv"
    else:
        return None, f"{header['format']} needs texconv to read"
    try:
        out = run([magick, "identify", "-format", "%[fx:minima.a]\n", target])
    finally:
        if target != path and os.path.exists(target):
            os.remove(target)
    try:
        values = [float(line) for line in out.splitlines() if line.strip()]
    except ValueError:
        return None, f"unexpected alpha reading from {reader}: {out.strip()[:40]!r}"
    if not values:
        return None, f"{reader} returned no alpha information"
    return round(min(values) * 255), None


def audit_file(header, bc7):
    """Return a list of (category, detail) issues for one DDS file."""
    issues = []
    w, h = header["width"], header["height"]
    if not (is_pow2(w) and is_pow2(h)):
        issues.append(("not power-of-2", None))
    want = mip_levels(w, h)
    if header["mips"] <= 1:
        issues.append(("missing mipmaps", "no mipmaps"))
    elif header["mips"] < want:
        issues.append(("missing mipmaps", f"{header['mips']} of {want} levels"))

    fmt = header["format"]
    if fmt not in ALLOWED_FORMATS:
        issues.append(("unexpected format", None))
    elif fmt == "DXT1" and header["has_alpha"]:
        issues.append((
            f"has alpha channel, should be {'BC7' if bc7 else 'DXT5'}",
            "DXT1 with its alpha flag set",
        ))
    elif fmt == "DXT5" and bc7:
        issues.append(("has alpha channel, should be BC7", None))
    return issues


def opaque_suggestion(fmt, min_alpha):
    if fmt == "DXT1":
        how = "could be plain DXT1 without the alpha flag"
    else:
        how = "could drop the alpha channel and use DXT1"
    return f"alpha is effectively opaque (lowest {min_alpha}); {how}"


def describe(header, min_alpha):
    if not header["has_alpha"]:
        alpha = "no alpha channel"
    elif min_alpha is None:
        alpha = "alpha channel"
    else:
        alpha = f"alpha channel, lowest {min_alpha}"
    kind = ", cubemap" if header["cubemap"] else ""
    return (
        f"{header['width']}x{header['height']} {header['format']}{kind}, "
        f"{header['mips']} mip level(s), {alpha}"
    )


def beside(path, other):
    """other's bare name if it is in path's folder, else its full path."""
    if os.path.dirname(os.path.abspath(path)) == os.path.dirname(os.path.abspath(other)):
        return os.path.basename(other)
    return other


def cmd_convert(args):
    magick = find_magick(args.magick)
    if not magick:
        print("error: ImageMagick (magick.exe) not found; use --magick PATH")
        return 2
    texconv = find_texconv(args.texconv) if args.bc7 else None
    if args.bc7 and not texconv:
        msg = f"--bc7 needs texconv.exe; download it from {TEXCONV_URL}"
        if not args.dry_run:
            print(f"error: {msg}")
            return 2
        print(f"warning: {msg}\n")

    files, unmatched = expand_inputs(args.inputs, IMAGE_EXTS, args.recursive)
    if args.out_dir and not args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)

    converted = []  # (path, fmt)
    not_pow2 = []  # (path, width, height)
    existing = []  # (path, out)
    collisions = []  # (path, out, first_path)
    suggestions = []  # (path, min_alpha)
    errors = [(arg, "no matching image files") for arg in unmatched]
    claimed = {}  # normalized output path -> input that claimed it

    verb = "Checking" if args.dry_run else "Converting"
    print(f"{verb} {len(files)} file(s)...\n")
    with tempfile.TemporaryDirectory(prefix="dds_tool_") as tmpdir:
        for path in files:
            if os.path.splitext(path)[1].lower() == ".dds":
                errors.append((path, "already a DDS file"))
                print(f"  [error] {path}: already a DDS file")
                continue
            try:
                width, height, has_alpha, min_alpha, frames = probe_image(magick, path)
            except (ToolError, ValueError) as exc:
                errors.append((path, str(exc)))
                print(f"  [error] {path}: {exc}")
                continue

            if not (is_pow2(width) and is_pow2(height)):
                not_pow2.append((path, width, height))
                print(f"  [skip] {path}: {width}x{height} is not a power of 2")
                continue

            fmt = choose_format(has_alpha, args.bc7)
            stem = os.path.splitext(os.path.basename(path))[0]
            out = os.path.join(args.out_dir or os.path.dirname(path), stem + ".dds")
            key = os.path.normcase(os.path.abspath(out))
            if key in claimed:
                collisions.append((path, out, claimed[key]))
                print(
                    f"  [skip] {path}: {beside(path, out)} is already the "
                    f"output of {beside(path, claimed[key])}"
                )
                continue
            claimed[key] = path
            if os.path.exists(out) and not args.force:
                existing.append((path, out))
                print(f"  [skip] {path}: {beside(path, out)} already exists")
                continue

            if has_alpha:
                info = f"{width}x{height}, alpha channel, lowest {min_alpha}"
            else:
                info = f"{width}x{height}, no alpha channel"
            if frames > 1:
                info += f", first of {frames} frames"
            if not args.dry_run:
                try:
                    if fmt == "BC7":
                        encoded = encode_bc7(magick, texconv, path, tmpdir)
                    else:
                        encoded = encode_dxt(
                            magick, path, fmt, mip_levels(width, height), tmpdir
                        )
                    install(encoded, out)
                except (ToolError, OSError) as exc:
                    errors.append((path, str(exc)))
                    print(f"  [error] {path}: {exc}")
                    continue
            converted.append((path, fmt))
            print(f"  [{fmt}] {path} -> {beside(path, out)}  ({info})")
            if has_alpha and min_alpha >= args.opaque_alpha:
                suggestions.append((path, min_alpha))
                print(f"         suggestion: {opaque_suggestion(fmt, min_alpha)}")

    print()
    print("=" * 72)
    counts = collections.Counter(fmt for _, fmt in converted)
    breakdown = ", ".join(f"{counts[f]} {f}" for f in ALLOWED_FORMATS if counts[f])
    done = "Would convert" if args.dry_run else "Converted"
    print(
        f"{done} {len(converted)} of {len(files)} file(s)"
        + (f" ({breakdown})" if breakdown else "")
        + "."
    )
    if not_pow2:
        print(f"\n{len(not_pow2)} file(s) skipped, size not a power of 2:")
        for path, width, height in not_pow2:
            print(f"  {path}  ({width}x{height})")
    if existing:
        print(
            f"\n{len(existing)} file(s) skipped, output already exists "
            "(use --force to overwrite):"
        )
        for path, out in existing:
            print(f"  {path} -> {beside(path, out)}")
    if collisions:
        print(f"\n{len(collisions)} file(s) skipped, same output name as another input:")
        for path, out, first in collisions:
            print(f"  {path} -> {beside(path, out)}  (already used by {beside(path, first)})")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for path, msg in errors:
            print(f"  {path}: {msg}")
    if suggestions:
        print(
            f"\nSuggestions ({len(suggestions)}) -- alpha channel is effectively "
            "opaque; removing it would allow DXT1:"
        )
        for path, min_alpha in suggestions:
            print(f"  {path}  (lowest alpha {min_alpha})")
    return 1 if (not_pow2 or existing or collisions or errors) else 0


def cmd_audit(args):
    magick = find_magick(args.magick)
    if not magick:
        print("error: ImageMagick (magick.exe) not found; use --magick PATH")
        return 2
    texconv = find_texconv(args.texconv)

    files, unmatched = expand_inputs(args.inputs, DDS_EXTS, args.recursive)
    mode = "BC7 rules" if args.bc7 else "DXT1/DXT5 rules, BC7 accepted"
    print(f"Auditing {len(files)} DDS file(s) ({mode})...\n")

    results = []  # (path, header or None, min_alpha, issues, suggestion)
    unchecked = []  # (path, reason) where alpha values could not be read
    with tempfile.TemporaryDirectory(prefix="dds_tool_") as tmpdir:
        for path in files:
            try:
                header = read_dds_header(path)
            except (ToolError, OSError) as exc:
                results.append((path, None, None, [("unreadable", str(exc))], None))
                print(f"  [issues] {path}")
                continue
            issues = audit_file(header, args.bc7)

            min_alpha, suggestion = None, None
            if header["has_alpha"] and header["format"] in ALLOWED_FORMATS:
                try:
                    min_alpha, note = dds_min_alpha(magick, texconv, path, header, tmpdir)
                except (ToolError, OSError) as exc:
                    note = str(exc)
                if note:
                    unchecked.append((path, note))
                elif min_alpha >= args.opaque_alpha:
                    suggestion = opaque_suggestion(header["format"], min_alpha)

            results.append((path, header, min_alpha, issues, suggestion))
            status = "issues" if issues else "suggestion" if suggestion else "ok"
            print(f"  [{status}] {path}")

    flagged = [r for r in results if r[3]]
    suggested = [r for r in results if r[4]]
    print()
    print("=" * 72)
    print(
        f"Audited {len(files)} DDS file(s); {len(flagged)} with issues, "
        f"{len(suggested)} with suggestions."
    )
    for arg in unmatched:
        print(f"  (no DDS files matched {arg})")

    for category in AUDIT_CATEGORIES:
        hits = [
            (path, header, min_alpha, detail)
            for path, header, min_alpha, issues, _ in flagged
            for cat, detail in issues
            if cat == category
        ]
        if not hits:
            continue
        print(f"\n{category} ({len(hits)}):")
        for path, header, min_alpha, detail in hits:
            info = f"  [{describe(header, min_alpha)}]" if header else ""
            extra = f"  -- {detail}" if detail else ""
            print(f"  {path}{info}{extra}")

    if suggested:
        print(f"\nSuggestions ({len(suggested)}), not counted as issues:")
        for path, header, min_alpha, _, suggestion in suggested:
            print(f"  {path}  [{describe(header, min_alpha)}]  -- {suggestion}")

    if unchecked:
        print(
            f"\n{len(unchecked)} file(s) whose alpha values could not be read "
            "(DXT1 suggestion not checked):"
        )
        for path, reason in unchecked:
            print(f"  {path}: {reason}")
        if not texconv:
            print(f"  texconv.exe is needed to read these; see {TEXCONV_URL}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "file", "width", "height", "format", "mip_levels",
                "alpha_channel", "lowest_alpha", "issues", "suggestion",
            ])
            for path, header, min_alpha, issues, suggestion in results:
                if not (issues or suggestion):
                    continue
                h = header or {}
                writer.writerow([
                    path, h.get("width", ""), h.get("height", ""),
                    h.get("format", ""), h.get("mips", ""),
                    {True: "yes", False: "no"}.get(h.get("has_alpha"), ""),
                    "" if min_alpha is None else min_alpha,
                    "; ".join(cat + (f" ({d})" if d else "") for cat, d in issues),
                    suggestion or "",
                ])
        rows = len(set(r[0] for r in flagged + suggested))
        print(f"\nWrote {rows} row(s) to {args.csv}")

    return 1 if flagged or unmatched else 0


def alpha_level(text):
    value = int(text)
    if not 0 <= value <= 255:
        raise argparse.ArgumentTypeError("must be between 0 and 255")
    return value


def main():
    parser = argparse.ArgumentParser(
        description="Batch-convert images to DDS, and audit existing DDS files.",
        epilog="Quote wildcards so the script can expand them.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("inputs", nargs="+", help="files, wildcards or folders")
    common.add_argument(
        "--bc7", action="store_true",
        help="images with an alpha channel use BC7 instead of DXT5",
    )
    common.add_argument(
        "-r", "--recursive", action="store_true", help="scan folders recursively"
    )
    common.add_argument(
        "--opaque-alpha", metavar="N", type=alpha_level, default=DEFAULT_OPAQUE_ALPHA,
        help="suggest DXT1 when an alpha channel's lowest value is at least N "
             f"(0-255, default {DEFAULT_OPAQUE_ALPHA})",
    )
    common.add_argument("--magick", metavar="PATH", help="path to magick.exe")
    common.add_argument("--texconv", metavar="PATH", help="path to texconv.exe")

    conv = sub.add_parser(
        "convert", parents=[common], help="convert images to DDS"
    )
    conv.add_argument(
        "--out-dir", metavar="DIR",
        help="write all DDS files here (default: beside each source)",
    )
    conv.add_argument(
        "--force", action="store_true", help="overwrite existing DDS files"
    )
    conv.add_argument(
        "--dry-run", action="store_true",
        help="show what would be converted without writing anything",
    )

    aud = sub.add_parser(
        "audit", parents=[common], help="list DDS files that break the rules"
    )
    aud.add_argument(
        "--csv", metavar="FILE",
        help="also write files with issues or suggestions to CSV",
    )

    args = parser.parse_args()
    if args.command == "convert":
        return cmd_convert(args)
    return cmd_audit(args)


if __name__ == "__main__":
    sys.exit(main())
