# PyInstaller spec for DDS Tool.  Build with tools\build.py, which also adds
# the bundled tools, LICENSE and licenses\ and zips the result.
#
# One folder, two programs sharing one Python runtime (in _internal\):
#   DDS Tool.exe   the window (dds_tool_gui.py), no console
#   dds_tool.exe   the command line (dds_tool.py)
# Both look for imagemagick\ and texconv.exe beside themselves (app_dir()).
#
# tkinterdnd2's native files are collected by the hook in
# pyinstaller-hooks-contrib, which takes only this platform's tkdnd folder.
# UPX is off: packed executables draw more antivirus false positives.

gui = Analysis(["dds_tool_gui.py"])
cli = Analysis(["dds_tool.py"])

gui_exe = EXE(
    PYZ(gui.pure),
    gui.scripts,
    [],
    exclude_binaries=True,
    name="DDS Tool",
    console=False,
    upx=False,
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="dds_tool",
    console=True,
    upx=False,
)

COLLECT(
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    name="DDS Tool",
    upx=False,
)
