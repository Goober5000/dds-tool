"""Shared paths and tool lookup for the dds_tool tests.

Generated files (fixtures and per-case work folders) go in tests\\_build,
which is safe to delete at any time.
"""

import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
BUILD = os.path.join(TESTS, "_build")
FIXTURES = os.path.join(BUILD, "fixtures")

sys.path.insert(0, REPO)
import dds_tool  # noqa: E402


def pick_magick():
    """The ImageMagick the tests use: bundled copy, then default install, then PATH.

    The default install comes before PATH because stale shells can still
    have an older ImageMagick first on PATH.
    """
    for candidate in (
        os.path.join(REPO, "imagemagick", "magick.exe"),
        dds_tool.KNOWN_MAGICK,
    ):
        if os.path.isfile(candidate):
            return candidate
    return dds_tool.find_magick()


def pick_texconv():
    return dds_tool.find_texconv()


def require_tools():
    """Return (magick, texconv), or exit with a message if either is missing."""
    magick, texconv = pick_magick(), pick_texconv()
    if not magick or not texconv:
        missing = [name for name, p in (("magick", magick), ("texconv", texconv)) if not p]
        sys.exit(f"error: tests need {' and '.join(missing)}")
    return magick, texconv


def ensure_fixtures():
    """Build the fixtures if they are not there yet."""
    if not os.path.isdir(FIXTURES):
        import make_fixtures
        make_fixtures.build()
