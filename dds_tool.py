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
uses to read BC7 alpha values.  ImageMagick is looked up via --magick, an
imagemagick\\magick.exe beside this script, PATH, then its default install
folder.  texconv is looked up via --texconv, the TEXCONV environment
variable, this script's folder, then PATH.  When frozen (PyInstaller),
"this script's folder" is the executable's folder.

Usage: python dds_tool.py convert <files|wildcards|folders>... [--bc7] [-r]
                          [--out-dir DIR] [--force] [--dry-run]
                          [--opaque-alpha N]
       python dds_tool.py audit <files|wildcards|folders>... [--bc7] [-r]
                          [--csv FILE] [--opaque-alpha N]

Quote wildcards ("textures\\*.png"); the script expands them itself.
Folders are scanned one level deep unless -r is given.

The work is done by convert_files() and audit_files(), which yield one
FileResult per file and take an optional progress callback and cancel
event, so other front ends (dds_tool_gui.py) can share them.
"""

import argparse
import collections
import csv
import dataclasses
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
MAGICK_URL = "https://imagemagick.org/script/download.php#windows"
DEFAULT_OPAQUE_ALPHA = 248
RULE = "=" * 72  # separates the per-file lines from the summary

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
CSV_COLUMNS = [
    "file", "width", "height", "format", "mip_levels",
    "alpha_channel", "lowest_alpha", "issues", "suggestion",
]

# FileResult.status values.  Convert yields the first three, audit the last two.
CONVERTED = "converted"
SKIPPED = "skipped"
ERROR = "error"
OK = "ok"
ISSUES = "issues"

# FileResult.skip_reason values
SKIP_NOT_POW2 = "not power of 2"
SKIP_EXISTS = "output exists"
SKIP_SAME_OUTPUT = "same output name"

# Windows: stop each tool call flashing a console window in a windowed app
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ToolError(RuntimeError):
    pass


@dataclasses.dataclass
class FileResult:
    """The outcome for one file, from convert_files() or audit_files().

    Convert fills in the source image's size and alpha, and for files that
    got as far as choosing one, the output format, mip count and path.
    Audit fills in what the DDS header says; all of it is left empty when
    the header could not be read.
    """

    path: str
    status: str  # CONVERTED, SKIPPED or ERROR; OK or ISSUES
    skip_reason: str = ""  # SKIP_* when status is SKIPPED
    format: str = ""  # format written (convert) or found (audit)
    width: int = None
    height: int = None
    mips: int = None
    has_alpha: bool = None
    min_alpha: int = None  # lowest alpha 0-255; None if no alpha channel or unread
    frames: int = 1  # convert: frames in the source; only the first is used
    cubemap: bool = False
    issues: list = dataclasses.field(default_factory=list)  # (category, detail)
    suggestion: str = ""
    output_path: str = ""
    first_input: str = ""  # SKIP_SAME_OUTPUT: the input that claimed output_path
    message: str = ""  # ERROR: what went wrong
    alpha_note: str = ""  # audit: why the alpha values could not be read


def app_dir():
    """This script's folder, or the executable's folder when frozen."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)  # PyInstaller onedir build
    return os.path.dirname(os.path.abspath(__file__))


def find_magick(override=None):
    if override:
        return override if os.path.isfile(override) else None
    for candidate in (
        os.path.join(app_dir(), "imagemagick", "magick.exe"),
        shutil.which("magick"),
        KNOWN_MAGICK,
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def find_texconv(override=None):
    if override:
        return override if os.path.isfile(override) else None
    for candidate in (
        os.environ.get("TEXCONV"),
        os.path.join(app_dir(), "texconv.exe"),
        shutil.which("texconv"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def run(cmd):
    """Run an external tool and return its stdout; raise ToolError on failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )
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


def describe(result):
    if not result.has_alpha:
        alpha = "no alpha channel"
    elif result.min_alpha is None:
        alpha = "alpha channel"
    else:
        alpha = f"alpha channel, lowest {result.min_alpha}"
    kind = ", cubemap" if result.cubemap else ""
    return (
        f"{result.width}x{result.height} {result.format}{kind}, "
        f"{result.mips} mip level(s), {alpha}"
    )


def beside(path, other):
    """other's bare name if it is in path's folder, else its full path."""
    if os.path.dirname(os.path.abspath(path)) == os.path.dirname(os.path.abspath(other)):
        return os.path.basename(other)
    return other


def convert_files(
    files, magick, texconv=None, bc7=False, out_dir=None, force=False,
    dry_run=False, opaque_alpha=DEFAULT_OPAQUE_ALPHA, progress=None, cancel=None,
):
    """Convert images to DDS, yielding a FileResult for each file in turn.

    texconv is needed only to write BC7.  progress, if given, is called as
    progress(n, total, path) before file n (counting from 1) is started.
    cancel, if given, is a threading.Event; once it is set, the batch stops
    before the next file.
    """
    if out_dir and not dry_run:
        os.makedirs(out_dir, exist_ok=True)
    claimed = {}  # normalized output path -> input that claimed it

    with tempfile.TemporaryDirectory(prefix="dds_tool_") as tmpdir:
        for n, path in enumerate(files, 1):
            if cancel is not None and cancel.is_set():
                return
            if progress:
                progress(n, len(files), path)
            if os.path.splitext(path)[1].lower() in DDS_EXTS:
                yield FileResult(path, ERROR, message="already a DDS file")
                continue
            try:
                width, height, has_alpha, min_alpha, frames = probe_image(magick, path)
            except (ToolError, ValueError) as exc:
                yield FileResult(path, ERROR, message=str(exc))
                continue

            result = FileResult(
                path, SKIPPED, width=width, height=height, has_alpha=has_alpha,
                min_alpha=min_alpha if has_alpha else None, frames=frames,
            )
            if not (is_pow2(width) and is_pow2(height)):
                result.skip_reason = SKIP_NOT_POW2
                yield result
                continue

            result.format = choose_format(has_alpha, bc7)
            result.mips = mip_levels(width, height)
            stem = os.path.splitext(os.path.basename(path))[0]
            out = os.path.join(out_dir or os.path.dirname(path), stem + ".dds")
            result.output_path = out
            key = os.path.normcase(os.path.abspath(out))
            if key in claimed:
                result.skip_reason = SKIP_SAME_OUTPUT
                result.first_input = claimed[key]
                yield result
                continue
            claimed[key] = path
            if os.path.exists(out) and not force:
                result.skip_reason = SKIP_EXISTS
                yield result
                continue

            if not dry_run:
                try:
                    if result.format == "BC7":
                        encoded = encode_bc7(magick, texconv, path, tmpdir)
                    else:
                        encoded = encode_dxt(magick, path, result.format, result.mips, tmpdir)
                    install(encoded, out)
                except (ToolError, OSError) as exc:
                    result.status = ERROR
                    result.message = str(exc)
                    yield result
                    continue
            result.status = CONVERTED
            if has_alpha and min_alpha >= opaque_alpha:
                result.suggestion = opaque_suggestion(result.format, min_alpha)
            yield result


def audit_files(
    files, magick, texconv=None, bc7=False, opaque_alpha=DEFAULT_OPAQUE_ALPHA,
    progress=None, cancel=None,
):
    """Audit DDS files, yielding a FileResult for each file in turn.

    Without texconv, alpha values of DX10-header files (including BC7)
    cannot be read; those results get an alpha_note instead.  progress and
    cancel work as in convert_files().
    """
    with tempfile.TemporaryDirectory(prefix="dds_tool_") as tmpdir:
        for n, path in enumerate(files, 1):
            if cancel is not None and cancel.is_set():
                return
            if progress:
                progress(n, len(files), path)
            try:
                header = read_dds_header(path)
            except (ToolError, OSError) as exc:
                yield FileResult(path, ISSUES, issues=[("unreadable", str(exc))])
                continue

            result = FileResult(
                path, OK, format=header["format"], width=header["width"],
                height=header["height"], mips=header["mips"],
                has_alpha=header["has_alpha"], cubemap=header["cubemap"],
                issues=audit_file(header, bc7),
            )
            if header["has_alpha"] and header["format"] in ALLOWED_FORMATS:
                try:
                    min_alpha, note = dds_min_alpha(magick, texconv, path, header, tmpdir)
                except (ToolError, OSError) as exc:
                    min_alpha, note = None, str(exc)
                result.min_alpha = min_alpha
                if note:
                    result.alpha_note = note
                elif min_alpha >= opaque_alpha:
                    result.suggestion = opaque_suggestion(header["format"], min_alpha)
            if result.issues:
                result.status = ISSUES
            yield result


def write_audit_csv(path, results):
    """Write the audit results with issues or a suggestion to CSV.

    Returns the number of rows written.
    """
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in results:
            if not (r.issues or r.suggestion):
                continue
            writer.writerow([
                r.path,
                "" if r.width is None else r.width,
                "" if r.height is None else r.height,
                r.format,
                "" if r.mips is None else r.mips,
                {True: "yes", False: "no"}.get(r.has_alpha, ""),
                "" if r.min_alpha is None else r.min_alpha,
                "; ".join(cat + (f" ({d})" if d else "") for cat, d in r.issues),
                r.suggestion,
            ])
            rows += 1
    return rows


def convert_header(total, dry_run):
    verb = "Checking" if dry_run else "Converting"
    return f"{verb} {total} file(s)..."


def convert_line(r):
    """The report line for one convert result (two lines with a suggestion)."""
    if r.status == ERROR:
        return f"  [error] {r.path}: {r.message}"
    if r.skip_reason == SKIP_NOT_POW2:
        return f"  [skip] {r.path}: {r.width}x{r.height} is not a power of 2"
    if r.skip_reason == SKIP_SAME_OUTPUT:
        return (
            f"  [skip] {r.path}: {beside(r.path, r.output_path)} is already the "
            f"output of {beside(r.path, r.first_input)}"
        )
    if r.skip_reason == SKIP_EXISTS:
        return f"  [skip] {r.path}: {beside(r.path, r.output_path)} already exists"
    if r.has_alpha:
        info = f"{r.width}x{r.height}, alpha channel, lowest {r.min_alpha}"
    else:
        info = f"{r.width}x{r.height}, no alpha channel"
    if r.frames > 1:
        info += f", first of {r.frames} frames"
    line = f"  [{r.format}] {r.path} -> {beside(r.path, r.output_path)}  ({info})"
    if r.suggestion:
        line += f"\n         suggestion: {r.suggestion}"
    return line


def convert_summary(results, total, unmatched, dry_run):
    """The lines of the convert summary, which follow the RULE line."""
    converted = [r for r in results if r.status == CONVERTED]
    not_pow2 = [r for r in results if r.skip_reason == SKIP_NOT_POW2]
    existing = [r for r in results if r.skip_reason == SKIP_EXISTS]
    collisions = [r for r in results if r.skip_reason == SKIP_SAME_OUTPUT]
    errors = [(arg, "no matching image files") for arg in unmatched]
    errors += [(r.path, r.message) for r in results if r.status == ERROR]
    suggestions = [r for r in converted if r.suggestion]

    counts = collections.Counter(r.format for r in converted)
    breakdown = ", ".join(f"{counts[f]} {f}" for f in ALLOWED_FORMATS if counts[f])
    done = "Would convert" if dry_run else "Converted"
    lines = [
        f"{done} {len(converted)} of {total} file(s)"
        + (f" ({breakdown})" if breakdown else "")
        + "."
    ]
    if not_pow2:
        lines += ["", f"{len(not_pow2)} file(s) skipped, size not a power of 2:"]
        lines += [f"  {r.path}  ({r.width}x{r.height})" for r in not_pow2]
    if existing:
        lines += [
            "",
            f"{len(existing)} file(s) skipped, output already exists "
            "(use --force to overwrite):",
        ]
        lines += [f"  {r.path} -> {beside(r.path, r.output_path)}" for r in existing]
    if collisions:
        lines += ["", f"{len(collisions)} file(s) skipped, same output name as another input:"]
        lines += [
            f"  {r.path} -> {beside(r.path, r.output_path)}  "
            f"(already used by {beside(r.path, r.first_input)})"
            for r in collisions
        ]
    if errors:
        lines += ["", f"{len(errors)} error(s):"]
        lines += [f"  {path}: {msg}" for path, msg in errors]
    if suggestions:
        lines += [
            "",
            f"Suggestions ({len(suggestions)}) -- alpha channel is effectively "
            "opaque; removing it would allow DXT1:",
        ]
        lines += [f"  {r.path}  (lowest alpha {r.min_alpha})" for r in suggestions]
    return lines


def convert_exit_code(results, unmatched):
    """1 if anything was skipped, failed or unmatched, else 0."""
    return 1 if unmatched or any(r.status != CONVERTED for r in results) else 0


def audit_header(total, bc7):
    mode = "BC7 rules" if bc7 else "DXT1/DXT5 rules, BC7 accepted"
    return f"Auditing {total} DDS file(s) ({mode})..."


def audit_line(r):
    status = "issues" if r.issues else "suggestion" if r.suggestion else "ok"
    return f"  [{status}] {r.path}"


def audit_summary(results, total, unmatched, texconv_found):
    """The lines of the audit summary, which follow the RULE line."""
    flagged = [r for r in results if r.issues]
    suggested = [r for r in results if r.suggestion]
    unchecked = [r for r in results if r.alpha_note]
    lines = [
        f"Audited {total} DDS file(s); {len(flagged)} with issues, "
        f"{len(suggested)} with suggestions."
    ]
    lines += [f"  (no DDS files matched {arg})" for arg in unmatched]

    for category in AUDIT_CATEGORIES:
        hits = [
            (r, detail) for r in flagged for cat, detail in r.issues if cat == category
        ]
        if not hits:
            continue
        lines += ["", f"{category} ({len(hits)}):"]
        for r, detail in hits:
            info = f"  [{describe(r)}]" if r.format else ""
            extra = f"  -- {detail}" if detail else ""
            lines.append(f"  {r.path}{info}{extra}")

    if suggested:
        lines += ["", f"Suggestions ({len(suggested)}), not counted as issues:"]
        lines += [f"  {r.path}  [{describe(r)}]  -- {r.suggestion}" for r in suggested]

    if unchecked:
        lines += [
            "",
            f"{len(unchecked)} file(s) whose alpha values could not be read "
            "(DXT1 suggestion not checked):",
        ]
        lines += [f"  {r.path}: {r.alpha_note}" for r in unchecked]
        if not texconv_found:
            lines.append(f"  texconv.exe is needed to read these; see {TEXCONV_URL}")
    return lines


def audit_exit_code(results, unmatched):
    """1 if any file has issues or an input matched nothing, else 0."""
    return 1 if unmatched or any(r.issues for r in results) else 0


def report_text(header, lines, summary):
    """The whole report as the CLI prints it, for copying elsewhere."""
    return "\n".join([header, "", *lines, "", RULE, *summary]) + "\n"


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
    print(convert_header(len(files), args.dry_run) + "\n")
    results = []
    for r in convert_files(
        files, magick, texconv, bc7=args.bc7, out_dir=args.out_dir,
        force=args.force, dry_run=args.dry_run, opaque_alpha=args.opaque_alpha,
    ):
        results.append(r)
        print(convert_line(r))
    print()
    print(RULE)
    print("\n".join(convert_summary(results, len(files), unmatched, args.dry_run)))
    return convert_exit_code(results, unmatched)


def cmd_audit(args):
    magick = find_magick(args.magick)
    if not magick:
        print("error: ImageMagick (magick.exe) not found; use --magick PATH")
        return 2
    texconv = find_texconv(args.texconv)

    files, unmatched = expand_inputs(args.inputs, DDS_EXTS, args.recursive)
    print(audit_header(len(files), args.bc7) + "\n")
    results = []
    for r in audit_files(
        files, magick, texconv, bc7=args.bc7, opaque_alpha=args.opaque_alpha
    ):
        results.append(r)
        print(audit_line(r))
    print()
    print(RULE)
    print("\n".join(audit_summary(results, len(files), unmatched, bool(texconv))))
    if args.csv:
        rows = write_audit_csv(args.csv, results)
        print(f"\nWrote {rows} row(s) to {args.csv}")
    return audit_exit_code(results, unmatched)


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
