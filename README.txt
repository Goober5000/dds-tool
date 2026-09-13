DDS Tool
========

Converts textures to DDS the way FreeSpace Open expects them, and audits
existing DDS files for the same rules.  Everything it needs is in this
folder: nothing to install, no Python or ImageMagick required.

  DDS Tool.exe   the program, with a window
  dds_tool.exe   the same tool from a command prompt

Unzip the whole "DDS Tool" folder anywhere and keep its contents together:
the programs look for imagemagick\ and texconv.exe beside themselves.

Tested on 64-bit Windows 11.


The rules
---------

Converting:
  * Both sides must be a power of 2 (512x512, 1024x256, ...).  Other sizes
    are skipped and listed, never resized.
  * An image without an alpha channel becomes DXT1.  An image with an
    alpha channel becomes DXT5, or BC7 if you choose BC7.  Whether the
    alpha channel exists decides the format, not what is in it.
  * Every DDS gets a full set of mipmaps, down to 1x1.
  * Output is <name>.dds beside each image, or in the output folder you
    choose.  Existing DDS files are skipped unless you choose to overwrite
    them.  If two images would make the same DDS name (ship.png and
    ship.tga), the first one wins and the second is skipped.  A failed
    conversion never leaves a half-written DDS behind.
  * Reads PNG, TGA, BMP, JPG, TIF, PCX, GIF and WEBP.  Only the first frame
    of an animated GIF is used.

Auditing reports these as issues:
  * a file that cannot be read, or a size that is not a power of 2
  * no mipmaps, or an incomplete set ("3 of 7 levels")
  * a DXT1 with its alpha flag set: it should be DXT5 (or BC7 in BC7 mode)
  * a DXT5 when auditing in BC7 mode: it should be BC7
  * any format other than DXT1, DXT5 or BC7
BC7 is accepted in place of DXT5 unless you audit in BC7 mode.  DXT5 and
BC7 always count as having an alpha channel; DXT1 counts only if its alpha
flag is set.

Suggestions are advice, never issues.  When an alpha channel's lowest value
is at least 248 (out of 255), the alpha is effectively opaque, so the
texture could drop its alpha channel and use DXT1, which is half the size.
The threshold can be changed; the default leaves room for BC7, which can
store fully opaque alpha as low as 251.


Using DDS Tool.exe
------------------

1. Add files or folders: drag them onto the list, or use Add Files... or
   Add Folder....  Dropping them onto the DDS Tool.exe icon starts the
   program with them already in the list.
   Tick "Include subfolders" to search folders all the way down.
2. Choose "Convert images to DDS" or "Audit DDS files".  The same list
   works for both, so you can convert a folder and then audit it.
3. Set the options:
     BC7 instead of DXT5       for images with alpha (convert and audit)
     Overwrite existing DDS    replace DDS files that are already there
     Dry run                   show what would happen, write nothing
     Output folder             blank puts each DDS beside its image
     Suggest DXT1 when lowest alpha >= N   the suggestion threshold
4. Press Run.  Cancel stops after the file being worked on.

Results are coloured: red for errors and unreadable files, amber for
issues and skipped files, blue for suggestions.  Click a column heading to
sort, and click a row to see its full details under the table.

Copy log copies a text report of the run, the same one dds_tool.exe
prints; paste it when asking someone for help.  After an audit, Export
CSV... saves the files with issues or suggestions as a spreadsheet.

The bottom of the window shows which ImageMagick and texconv are in use.


Using dds_tool.exe
------------------

  dds_tool.exe convert <files, folders or "wildcards"> [options]
  dds_tool.exe audit   <files, folders or "wildcards"> [options]

  Both:     --bc7              BC7 instead of DXT5
            -r                 search folders all the way down
            --opaque-alpha N   suggestion threshold (default 248)
  convert:  --out-dir DIR      write every DDS into DIR
            --force            overwrite existing DDS files
            --dry-run          show what would happen, write nothing
  audit:    --csv FILE         also save issues and suggestions as CSV

Put quotes around wildcards ("maps\*.png"); the tool expands them itself.
Folders are searched one level deep unless -r is given.  The exit code is
0 when all is well, 1 if anything was skipped, failed or has issues, and 2
if ImageMagick (or texconv, for BC7) is missing or the command is wrong.
"dds_tool.exe convert --help" and "dds_tool.exe audit --help" list every
option.

Examples:
  dds_tool.exe convert data\maps --bc7
  dds_tool.exe audit data\maps -r --csv audit.csv


Windows security warnings
-------------------------

DDS Tool.exe and dds_tool.exe are not code-signed, so the first time you
run them Windows SmartScreen may say "Windows protected your PC".  Click
"More info", then "Run anyway".  Some antivirus programs also distrust
unsigned programs built with PyInstaller, which DDS Tool is; if yours
quarantines a file, restore it or add an exception for the folder.

The bundled ImageMagick (magick.exe) and texconv.exe are the unmodified,
signed releases from ImageMagick Studio LLC and Microsoft.


What is included
----------------

  imagemagick\   ImageMagick 7.1.2-31, portable build.  Does all the image
                 reading and the DXT1/DXT5 encoding.  It uses only its own
                 settings, even if another ImageMagick is installed.
  texconv.exe    Microsoft's DirectXTex texture converter (May 2026).
                 Encodes BC7 and reads BC7 files for the audit.
  _internal\     The Python runtime and libraries the programs run on,
                 including tkinterdnd2 for drag-and-drop.

DDS Tool itself is under the MIT License; see LICENSE.  The bundled
software keeps its own licenses: see licenses\THIRD_PARTY_NOTICES.txt for
what each part is, its license and where its source is, with the full
license texts beside it.  DDS Tool is not affiliated with or endorsed by
ImageMagick Studio LLC or Microsoft.


Building from source
--------------------

Only needed with the source code, not with the zip.  With Python 3.12:

  pip install -r requirements-build.txt
  python tools\build.py

tools\build.py downloads the pinned ImageMagick and texconv, checks their
SHA-256 hashes, and writes dist\DDS-Tool-<date>-win64.zip.

To run the scripts directly (python dds_tool_gui.py, python dds_tool.py),
first run python tools\fetch_tools.py, which puts the same tools beside
them.  The tests are in tests\: run_cases.py, test_core.py, test_gui.py,
and test_package.py for a built zip.
