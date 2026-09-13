"""Tests for dds_tool_gui: drives the real window (withdrawn) end to end.

Message boxes are replaced by recorders, so nothing modal can stall the run.
Log and CSV output are compared with the command line's for the same inputs.

Usage: python tests\\test_gui.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk

import testlib
from testlib import dds_tool as t

import dds_tool_gui as gui  # testlib put the repo on sys.path

failures = []
dialogs = []  # (kind, message) for every message box the app tried to show
answer = {"askyesno": True}


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def record(kind):
    def show(title, message, **kwargs):
        dialogs.append((kind, message))
        return answer.get(kind)
    return show


for _kind in ("showinfo", "showerror", "showwarning", "askyesno"):
    setattr(gui.messagebox, _kind, record(_kind))


def fresh():
    work = tempfile.mkdtemp(prefix="gui_", dir=testlib.BUILD)
    shutil.copytree(testlib.FIXTURES, work, dirs_exist_ok=True)
    return work


def make_app(magick, texconv, inputs=()):
    root = tk.Tk()
    root.withdraw()
    return root, gui.App(root, magick, texconv, inputs)


def wait(root, app, timeout=300):
    end = time.time() + timeout
    while app.worker is not None and time.time() < end:
        root.update()
        time.sleep(0.01)
    root.update()
    return app.worker is None


def rows(app):
    """{basename: (values, tag)} in display order."""
    out = {}
    for iid in app.tree.get_children():
        values = app.tree.item(iid, "values")
        out[os.path.basename(values[0])] = (values, app.tree.item(iid, "tags")[0])
    return out


def disabled(widget):
    return widget.instate(["disabled"])


def cli(magick, *args):
    proc = subprocess.run(
        [sys.executable, os.path.join(testlib.REPO, "dds_tool.py"), *args, "--magick", magick],
        capture_output=True, text=True,
    )
    return proc.stdout


def temp_folders():
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("dds_tool_")}


def test_startup(magick, texconv):
    root, app = make_app(magick, texconv)
    check("ImageMagick 7." in app.magick_status["text"], f"status shows version ({app.magick_status['text']})")
    check(texconv in app.texconv_status["text"], "status shows texconv path")
    check(not disabled(app.run_button), "Run enabled with tools present")
    check(disabled(app.cancel_button) and disabled(app.copy_button) and disabled(app.csv_button),
          "Cancel, Copy log and Export CSV disabled before a run")
    check(app.hint.winfo_manager() == "place", "empty input list shows the hint")
    app.add_paths([testlib.FIXTURES, testlib.FIXTURES + os.sep, testlib.FIXTURES.upper()])
    check(app.listbox.size() == 1, "duplicate inputs ignored")
    check(app.hint.winfo_manager() == "", "hint hidden once the list has entries")

    class Drop:
        data = "{C:/some folder/with space.png} C:/other/plain.tga"
        action = "copy"

    check(app.on_drop(Drop()) == "copy", "drop handler returns the action")
    check(app.listbox.get(1, 2) == (r"C:\some folder\with space.png", r"C:\other\plain.tga"),
          "dropped brace-quoted paths split and normalized")
    app.listbox.selection_set(1, 2)
    app.remove_selected()
    check(app.listbox.size() == 1, "Remove deletes the selection")
    app.mode.set("audit")
    app.update_controls()
    check(disabled(app.force_check) and disabled(app.out_entry) and not disabled(app.bc7_check),
          "audit mode disables convert-only options")
    root.destroy()


def test_dnd(magick, texconv):
    if gui.TkinterDnD is None:
        print("  skip  tkinterdnd2 not installed; drag-and-drop not tested")
    else:
        root, dnd = gui.make_root()
        root.withdraw()
        check(dnd, "tkinterdnd2 root created")
        app = gui.App(root, magick, texconv, dnd=True)
        check(bool(app.listbox.bind("<<Drop>>")), "input list is a drop target")
        check(app.hint["text"].startswith("Drop files"), "hint mentions dropping")
        root.destroy()
    saved = gui.TkinterDnD
    gui.TkinterDnD = None
    try:
        root, dnd = gui.make_root()
        check(not dnd and type(root) is tk.Tk, "plain Tk root without tkinterdnd2")
        app = gui.App(root, magick, texconv, dnd=False)
        check(app.hint["text"].startswith("Use Add Files"), "hint points at the buttons")
        root.destroy()
    finally:
        gui.TkinterDnD = saved


def test_missing_tools(texconv):
    root, app = make_app(None, None)
    check(disabled(app.run_button), "Run disabled without ImageMagick")
    check("missing" in app.magick_status["text"] and "missing" in app.texconv_status["text"],
          "status says both tools are missing")
    root.destroy()

    dummy = os.path.join(testlib.BUILD, "not_magick.exe")
    with open(dummy, "w") as f:
        f.write("not a program\n")
    root, app = make_app(dummy, texconv)
    check(disabled(app.run_button) and "cannot run" in app.magick_status["text"],
          "a broken magick.exe disables Run and says why")
    root.destroy()


def test_run_checks(magick):
    work = fresh()
    root, app = make_app(magick, None, [os.path.join(work, "conv")])
    dialogs.clear()
    app.bc7.set(True)
    app.start()
    check(app.worker is None and dialogs and "texconv" in dialogs[-1][1],
          "BC7 without texconv is refused")
    app.bc7.set(False)
    app.opaque_alpha.set("300")
    app.start()
    check(app.worker is None and "0 to 255" in dialogs[-1][1], "bad alpha threshold is refused")
    app.opaque_alpha.set("248")
    app.force.set(True)
    answer["askyesno"] = False
    app.start()
    check(app.worker is None and dialogs[-1][0] == "askyesno", "Overwrite asks first; No stops")
    answer["askyesno"] = True
    root.destroy()
    shutil.rmtree(work)


def test_convert(magick, texconv):
    work = fresh()
    conv = os.path.join(work, "conv")
    root, app = make_app(magick, texconv, [conv, os.path.join(work, "nothing here")])
    app.recursive.set(True)
    app.dry_run.set(True)
    app.start()
    check(disabled(app.run_button) and not disabled(app.cancel_button), "running: Run off, Cancel on")
    check(disabled(app.input_buttons[0]) and disabled(app.bc7_check), "running: inputs and options locked")
    check(wait(root, app), "dry run finished")
    r = rows(app)
    check(len(app.results) == 15 and len(r) == 16, f"15 results + 1 unmatched row ({len(r)})")
    check(r["nothing here"][0][1] == "no files" and r["nothing here"][1] == "error", "unmatched input row")
    check(r["broken.png"][1] == "error", "broken.png tagged error")
    check(r["npot.png"][1] == "issue" and r["exists.png"][1] == "issue", "skips tagged issue")
    check(r["alpha_opaque.png"][1] == "suggestion", "alpha_opaque.png tagged suggestion")
    check(r["opaque.png"][1] == "ok" and r["opaque.png"][0][1] == "would convert", "opaque.png ok")
    check(r["deep.png"][0][0] == os.path.join("sub dir", "deeper", "deep.png"),
          "File column is relative to the common folder")
    check(app.summary_text.get().startswith("Would convert 11 of 15 file(s) (5 DXT1, 6 DXT5)"),
          f"summary line ({app.summary_text.get()})")
    check(not os.path.exists(os.path.join(conv, "opaque.dds")), "dry run wrote nothing")

    expected = cli(magick, "convert", conv, os.path.join(work, "nothing here"), "-r", "--dry-run")
    check(app.log_text() == expected, "Copy log text matches the CLI's stdout")
    app.copy_log()
    check(root.clipboard_get() == app.log_text().rstrip("\n") or root.clipboard_get() == app.log_text(),
          "Copy log puts the text on the clipboard")
    check(disabled(app.csv_button), "Export CSV stays off after a convert")

    app.sort_by("size")
    sizes = [app.rows[i][0].width * app.rows[i][0].height
             for i in app.tree.get_children() if app.rows[i][0] and app.rows[i][0].width]
    check(sizes == sorted(sizes), "Size sorts ascending numerically")
    check(app.tree.heading("size", "text") == "Size ▲", "sorted heading shows an arrow")
    app.sort_by("size")
    sizes = [app.rows[i][0].width * app.rows[i][0].height
             for i in app.tree.get_children() if app.rows[i][0] and app.rows[i][0].width]
    check(sizes == sorted(sizes, reverse=True), "second click sorts descending")

    app.listbox.delete(1)
    app.dry_run.set(False)
    app.bc7.set(True)
    app.start()
    check(wait(root, app), "BC7 convert finished")
    r = rows(app)
    check(r["trans.png"][0][2] == "BC7" and os.path.isfile(os.path.join(conv, "trans.dds")),
          "BC7 row and file written")
    check(app.summary_text.get().startswith("Converted 11 of 15 file(s) (5 DXT1, 6 BC7)"),
          f"BC7 summary ({app.summary_text.get()})")
    root.destroy()
    shutil.rmtree(work)


def test_cancel(magick, texconv):
    work = fresh()
    before = temp_folders()
    root, app = make_app(magick, texconv, [os.path.join(work, "conv")])
    app.recursive.set(True)
    app.bc7.set(True)
    app.start()
    end = time.time() + 120
    while not app.results and time.time() < end:
        root.update()
        time.sleep(0.01)
    app.cancel_run()
    check(disabled(app.cancel_button) and "Cancelling" in app.progress_text.get(),
          "Cancel shows it is waiting for the current file")
    check(wait(root, app), "cancelled run finished")
    total = app.run_info["total"]
    check(0 < len(app.results) < total, f"stopped early ({len(app.results)} of {total})")
    check("cancelled after" in app.summary_text.get(), "summary says cancelled")
    check(app.log_text().rstrip().endswith(f"Cancelled after {len(app.results)} of {total} file(s)."),
          "log says cancelled")
    check(not disabled(app.run_button) and not disabled(app.input_buttons[0]), "controls re-enabled")
    check(temp_folders() == before, "no temp folder left behind")
    root.destroy()
    shutil.rmtree(work)


def test_close_while_running(magick, texconv):
    work = fresh()
    conv = os.path.join(work, "conv")
    before = temp_folders()
    root, app = make_app(magick, texconv, [conv])
    app.recursive.set(True)
    app.bc7.set(True)
    app.start()
    end = time.time() + 120
    while not app.results and time.time() < end:
        root.update()
        time.sleep(0.01)
    dialogs.clear()
    app.on_close()
    check(dialogs and dialogs[-1][0] == "askyesno", "closing mid-run asks first")
    end = time.time() + 120
    while not app.closed and time.time() < end:
        root.update()
        time.sleep(0.01)
    check(app.closed and app.worker is None, "window closes once the current file is done")
    staging = [n for _, _, names in os.walk(conv) for n in names if n.endswith(".tmp")]
    check(not staging, "no half-written staging files left")
    check(temp_folders() == before, "no temp folder left behind")
    shutil.rmtree(work)


def test_worker_error(magick, texconv):
    work = fresh()
    blocker = os.path.join(work, "a file")
    with open(blocker, "w") as f:
        f.write("in the way\n")
    root, app = make_app(magick, texconv, [os.path.join(work, "conv")])
    app.out_dir.set(os.path.join(blocker, "out"))  # cannot be created
    dialogs.clear()
    app.start()
    check(wait(root, app), "failed run finished")
    check(dialogs and dialogs[-1][0] == "showerror" and "unexpected error" in dialogs[-1][1],
          "unexpected worker error is shown, not swallowed")
    check("stopped by an error" in app.summary_text.get(), "summary says it stopped")
    check(not disabled(app.run_button), "controls re-enabled after the error")
    root.destroy()
    shutil.rmtree(work)


def test_audit(magick, texconv):
    work = fresh()
    aud = os.path.join(work, "aud")
    root, app = make_app(magick, texconv, [aud])
    app.mode.set("audit")
    app.bc7.set(True)
    app.start()
    check(wait(root, app), "audit finished")
    r = rows(app)
    check(len(r) == 15, f"15 audit rows ({len(r)})")
    check(r["notdds.dds"][1] == "error" and r["nomips.dds"][1] == "issue", "unreadable=error, issues=issue")
    check(r["opaque_bc7.dds"][1] == "suggestion" and r["dxt1_good.dds"][1] == "ok", "suggestion and ok rows")
    check(r["opaque_bc7.dds"][0][5] == "255" and r["dxt1_good.dds"][0][5] == "none", "Alpha column")
    check(not disabled(app.csv_button), "Export CSV enabled after an audit")
    iid = next(i for i in app.rows if app.rows[i][0] and app.rows[i][0].path.endswith("npot.dds"))
    app.tree.selection_set(iid)
    root.update()
    detail = app.detail_text.get()
    check(detail.startswith(os.path.join(aud, "npot.dds") + "\n")
          and detail.endswith("could drop the alpha channel and use DXT1"),
          "selecting a row shows its full path and details")

    check(app.log_text() == cli(magick, "audit", aud, "--bc7"), "audit log matches the CLI")
    gui_csv = os.path.join(work, "gui.csv")
    cli_csv = os.path.join(work, "cli.csv")
    app.save_csv(gui_csv)
    cli(magick, "audit", aud, "--bc7", "--csv", cli_csv)
    with open(gui_csv, "rb") as a, open(cli_csv, "rb") as b:
        check(a.read() == b.read(), "exported CSV is byte-identical to the CLI's")

    app.sort_by("alpha")
    keys = [gui.sort_key("alpha", 5, *app.rows[i]) for i in app.tree.get_children()]
    check(keys == sorted(keys), "Alpha sorts numerically")
    root.destroy()
    shutil.rmtree(work)


def main():
    magick, texconv = testlib.require_tools()
    testlib.ensure_fixtures()
    print(f"magick:  {magick}\ntexconv: {texconv}\n")
    test_startup(magick, texconv)
    test_dnd(magick, texconv)
    test_missing_tools(texconv)
    test_run_checks(magick)
    test_convert(magick, texconv)
    test_cancel(magick, texconv)
    test_close_while_running(magick, texconv)
    test_worker_error(magick, texconv)
    test_audit(magick, texconv)

    print()
    print("=" * 72)
    print(f"{len(failures)} failure(s)")
    for what in failures:
        print(f"  {what}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
