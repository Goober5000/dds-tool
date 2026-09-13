"""Build the dds_tool test fixtures in tests\\_build\\fixtures.

conv\\ holds convert inputs:
  * opaque.png: opaque PNG (DXT1)
  * trans.png: transparent PNG with distinct corners (DXT5 / BC7, flip check)
  * alpha_opaque.png: alpha channel but fully opaque (DXT5 plus a suggestion)
  * npot.png: 300x200 (skipped)
  * palette_trans.png: palette PNG with transparency (BC7 TrueColorAlpha fix)
  * rgba16.png: 16-bit PNG with alpha
  * tga99.tga: 32-bit TGA at 99% alpha (suggestion)
  * palette.pcx: palette PCX
  * anim.gif: 3 frames (first frame used)
  * dup.png + dup.tga: same stem (collision)
  * exists.png + exists.dds: output already there
  * broken.png: not really a PNG (error); readme.txt: not an image
  * "sub dir"\\...: nested files for -r, in a folder with a space

aud\\ holds audit inputs:
  * opaque_dxt5 (suggestion), trans_dxt5 (issue under --bc7)
  * dxt1_flag_trans / dxt1_flag_opaque: DXT1 with the alpha flag set
  * dxt1_noflag_trans: transparent DXT1 without the flag (passes silently)
  * dxt1_good, nomips, partialmips, uncompressed, npot
  * dx10_bc1: DX10-header BC1; opaque_bc7 (suggestion), trans_bc7
  * notdds, truncated: unreadable
  * "sub dir"\\nested.dds: for -r

Usage: python tests\\make_fixtures.py   (rebuilds from scratch)
"""

import os
import shutil
import struct
import subprocess
import sys

import testlib

CONV = os.path.join(testlib.FIXTURES, "conv")
AUD = os.path.join(testlib.FIXTURES, "aud")
TMP = os.path.join(testlib.BUILD, "fixtmp")


def set_alpha_flag(path, on):
    """Set or clear DDPF_ALPHAPIXELS in a DDS header."""
    with open(path, "r+b") as f:
        f.seek(80)
        flags = struct.unpack("<I", f.read(4))[0]
        flags = flags | 0x1 if on else flags & ~0x1
        f.seek(80)
        f.write(struct.pack("<I", flags))


def build():
    magick_exe, texconv_exe = testlib.require_tools()

    def magick(*args):
        # PNGs otherwise carry creation/modification times, which would
        # change the fixture hashes in expected_cases.txt on every rebuild
        *ops, output = args
        subprocess.run(
            [magick_exe, *ops, "-define", "png:exclude-chunks=date,time", output],
            check=True,
        )

    def texconv(src, fmt, mips, *extra):
        subprocess.run(
            [texconv_exe, "-nologo", "-y", "-f", fmt, "-m", str(mips), "-o", TMP, *extra, src],
            check=True, capture_output=True,
        )
        return os.path.join(TMP, os.path.splitext(os.path.basename(src))[0] + ".dds")

    for d in (testlib.FIXTURES, TMP):
        shutil.rmtree(d, ignore_errors=True)
    for d in (
        os.path.join(CONV, "sub dir", "deeper"),
        os.path.join(AUD, "sub dir"),
        TMP,
    ):
        os.makedirs(d)

    def c(name):
        return os.path.join(CONV, name)

    def a(name):
        return os.path.join(AUD, name)

    def t(name):
        return os.path.join(TMP, name)

    # ---- convert inputs ----
    magick("-size", "64x64", "gradient:red-blue", "PNG24:" + c("opaque.png"))
    magick("-size", "64x32", "xc:none",
           "-fill", "rgba(0,255,0,0.5)", "-draw", "circle 32,16 32,4",
           "-fill", "red", "-draw", "rectangle 0,0 3,3",
           "-fill", "blue", "-draw", "rectangle 60,28 63,31",
           "PNG32:" + c("trans.png"))
    magick("-size", "32x32", "gradient:yellow-purple", "-alpha", "set",
           "PNG32:" + c("alpha_opaque.png"))
    magick("-size", "300x200", "xc:gray", "PNG24:" + c("npot.png"))
    magick("-size", "32x32", "xc:none", "-fill", "red", "-draw", "rectangle 0,0 15,15",
           "PNG8:" + c("palette_trans.png"))
    magick("-size", "32x32", "gradient:rgba(0,0,0,0.2)-rgba(255,255,255,1)",
           "-depth", "16", "PNG64:" + c("rgba16.png"))
    magick("-size", "32x32", "xc:blue", "-alpha", "set", "-channel", "A",
           "-evaluate", "set", "99%", "+channel", "-type", "TrueColorAlpha",
           c("tga99.tga"))
    magick("-size", "32x32", "gradient:red-green", "-colors", "16", "-type", "Palette",
           "PCX:" + c("palette.pcx"))
    magick("-size", "32x32", "xc:red", "xc:green", "xc:blue", "-loop", "0", c("anim.gif"))
    magick("-size", "16x16", "xc:orange", "PNG24:" + c("dup.png"))
    magick("-size", "16x16", "xc:cyan", "-type", "TrueColor", c("dup.tga"))
    magick("-size", "16x16", "xc:white", "PNG24:" + c("exists.png"))
    magick("-size", "16x16", "xc:white", "-define", "dds:compression=dxt1",
           "DDS:" + c("exists.dds"))
    with open(c("broken.png"), "wb") as f:
        f.write(b"this is not a png\n")
    with open(c("readme.txt"), "w") as f:
        f.write("not an image\n")
    magick("-size", "128x64", "xc:navy", "PNG24:" + c(os.path.join("sub dir", "nested.png")))
    magick("-size", "8x8", "xc:none", "-fill", "white", "-draw", "point 1,1",
           "PNG32:" + c(os.path.join("sub dir", "deeper", "deep.png")))

    # ---- audit inputs ----
    magick("-size", "64x64", "gradient:red-blue", "-alpha", "set",
           "-type", "TrueColorAlpha", t("opaque_rgba.tga"))
    magick("-size", "64x64", "xc:none", "-fill", "red", "-draw", "rectangle 0,0 31,31",
           "-type", "TrueColorAlpha", t("hard_trans.tga"))
    magick("-size", "64x64", "gradient:red-blue", "-type", "TrueColor", t("opaque_rgb.tga"))
    magick("-size", "300x200", "xc:gray", "-alpha", "set", "-type", "TrueColorAlpha",
           t("npot_rgba.tga"))

    def place(src, name):
        shutil.move(src, a(name))

    def dxt1(src, name, flag):
        p = texconv(t(src), "DXT1", 0)
        set_alpha_flag(p, flag)
        place(p, name)

    place(texconv(t("opaque_rgba.tga"), "DXT5", 0), "opaque_dxt5.dds")
    place(texconv(t("hard_trans.tga"), "DXT5", 0), "trans_dxt5.dds")
    dxt1("hard_trans.tga", "dxt1_flag_trans.dds", True)
    dxt1("hard_trans.tga", "dxt1_noflag_trans.dds", False)
    dxt1("opaque_rgb.tga", "dxt1_flag_opaque.dds", True)
    dxt1("opaque_rgb.tga", "dxt1_good.dds", False)
    place(texconv(t("opaque_rgb.tga"), "DXT1", 1), "nomips.dds")
    place(texconv(t("opaque_rgb.tga"), "DXT1", 3), "partialmips.dds")
    place(texconv(t("opaque_rgba.tga"), "R8G8B8A8_UNORM", 0), "uncompressed.dds")
    place(texconv(t("npot_rgba.tga"), "DXT5", 1), "npot.dds")
    place(texconv(t("opaque_rgb.tga"), "BC1_UNORM", 0, "-dx10"), "dx10_bc1.dds")
    place(texconv(t("opaque_rgba.tga"), "BC7_UNORM", 0), "opaque_bc7.dds")
    place(texconv(t("hard_trans.tga"), "BC7_UNORM", 0), "trans_bc7.dds")
    with open(a("notdds.dds"), "wb") as f:
        f.write(b"hello, not a dds file at all\n")
    with open(a("truncated.dds"), "wb") as f:
        f.write(b"DDS " + b"\x7c\x00\x00\x00" + b"\x00" * 20)
    shutil.copy(a("dxt1_good.dds"), a(os.path.join("sub dir", "nested.dds")))

    shutil.rmtree(TMP)
    print(f"Built fixtures in {testlib.FIXTURES}")


def main():
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
