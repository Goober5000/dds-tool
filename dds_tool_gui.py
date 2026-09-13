#!/usr/bin/env python3
"""DDS Tool: a window for dds_tool.py's convert and audit commands.

Add image or DDS files and folders, choose Convert or Audit, and press Run.
The rules, formats and suggestions are exactly those of the command-line
tool (see dds_tool.py); this file is only the front end.

  * Convert writes <stem>.dds beside each image, or into the output folder.
    "Overwrite existing DDS" replaces files that are already there; "Dry
    run" shows what would happen without writing anything.
  * Audit lists DDS files that break the rules.  Export CSV writes the same
    CSV as "dds_tool.py audit --csv".
  * Copy log copies the report exactly as the command line prints it.

The batch runs on a worker thread that only posts to a queue, which the
window drains every 50 ms, so the window stays responsive; Cancel takes
effect between files.  Files and folders can be dragged onto the input list
when the optional tkinterdnd2 package is installed.  Paths given on the
command line (or dropped onto the program's icon) are added at startup.

Usage: python dds_tool_gui.py [files or folders...]
"""

import ctypes
import os
import queue
import sys
import threading
import traceback
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import dds_tool

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

TITLE = "DDS Tool"
POLL_MS = 50
POLL_BATCH = 200  # most queue items handled per poll, so the window stays responsive

# (column id, heading, width in digit widths, anchor); text runs wider than digits
COLUMNS = [
    ("file", "File", 36, "w"),
    ("result", "Result", 16, "w"),
    ("format", "Format", 16, "w"),
    ("size", "Size", 10, "e"),
    ("mips", "Mips", 7, "e"),
    ("alpha", "Alpha", 8, "e"),
    ("details", "Details", 60, "w"),
]
NUMERIC_COLUMNS = {"size", "mips", "alpha"}
ROW_COLOURS = {"error": "#f6d5d5", "issue": "#fbeac4", "suggestion": "#d7e6fb"}

IMAGE_TYPES = [
    ("Images", " ".join("*" + ext for ext in sorted(dds_tool.IMAGE_EXTS))),
    ("All files", "*.*"),
]
DDS_TYPES = [("DDS files", "*.dds"), ("All files", "*.*")]


def set_dpi_awareness():
    """Stop Windows scaling the window up as a blurry bitmap on high-DPI screens."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def magick_version(magick):
    """Return (version, None), e.g. ("7.1.2-31", None), or (None, error)."""
    try:
        out = dds_tool.run([magick, "-version"])
    except dds_tool.ToolError as exc:
        return None, str(exc)
    words = out.split()  # "Version: ImageMagick 7.1.2-31 Q16-HDRI x64 ..."
    if len(words) >= 3 and words[1] == "ImageMagick":
        return words[2], None
    return None, "unexpected output from magick -version"


def common_folder(paths):
    """The deepest folder holding all of paths, or "" if they share none."""
    if not paths:
        return ""
    try:
        return os.path.commonpath([os.path.dirname(p) for p in paths])
    except ValueError:  # different drives
        return ""


def alpha_text(r):
    if r.has_alpha is None:
        return ""
    if not r.has_alpha:
        return "none"
    return "?" if r.min_alpha is None else str(r.min_alpha)


def convert_row(r, dry_run):
    """(result, details, tag) for one convert result."""
    if r.status == dds_tool.ERROR:
        return "error", r.message, "error"
    out = dds_tool.beside(r.path, r.output_path) if r.output_path else ""
    if r.status == dds_tool.SKIPPED:
        if r.skip_reason == dds_tool.SKIP_NOT_POW2:
            details = "size is not a power of 2"
        elif r.skip_reason == dds_tool.SKIP_EXISTS:
            details = f"{out} already exists (tick Overwrite to replace it)"
        else:
            details = f"{out} is already the output of {dds_tool.beside(r.path, r.first_input)}"
        return "skipped", details, "issue"
    notes = [f"-> {out}"]
    if r.frames > 1:
        notes.append(f"first of {r.frames} frames")
    if r.suggestion:
        notes.append(f"suggestion: {r.suggestion}")
    result = "would convert" if dry_run else "converted"
    return result, "; ".join(notes), "suggestion" if r.suggestion else "ok"


def audit_row(r):
    """(result, details, tag) for one audit result."""
    notes = [cat + (f" ({detail})" if detail else "") for cat, detail in r.issues]
    if r.suggestion:
        notes.append(f"suggestion: {r.suggestion}")
    if r.alpha_note:
        notes.append(f"alpha not read: {r.alpha_note}")
    if any(cat == "unreadable" for cat, _ in r.issues):
        tag = "error"
    elif r.issues:
        tag = "issue"
    else:
        tag = "suggestion" if r.suggestion else "ok"
    result = "issues" if r.issues else "suggestion" if r.suggestion else "ok"
    return result, "; ".join(notes), tag


def sort_key(column, index, r, values):
    """Sort key for one row; r is None for an input that matched nothing."""
    if column not in NUMERIC_COLUMNS:
        return values[index].casefold()
    if r is None:
        return -1
    if column == "size":
        return -1 if r.width is None else r.width * r.height
    if column == "mips":
        return -1 if r.mips is None else r.mips
    if r.has_alpha is None:
        return -1
    if not r.has_alpha:
        return 256  # no alpha channel sorts above fully opaque
    return 257 if r.min_alpha is None else r.min_alpha


def work(q, cancel, mode, inputs, recursive, options, magick, texconv):
    """Run one batch on the worker thread, reporting only through q."""
    try:
        exts = dds_tool.IMAGE_EXTS if mode == "convert" else dds_tool.DDS_EXTS
        files, unmatched = dds_tool.expand_inputs(inputs, exts, recursive)
        q.put(("scan", files, unmatched))

        def progress(n, total, path):
            q.put(("progress", n, total, path))

        batch = dds_tool.convert_files if mode == "convert" else dds_tool.audit_files
        for r in batch(files, magick, texconv, progress=progress, cancel=cancel, **options):
            q.put(("result", r))
        q.put(("done", None))
    except Exception:
        q.put(("done", traceback.format_exc()))


class App:
    """The DDS Tool window.  magick and texconv are tool paths, or None if missing."""

    def __init__(self, root, magick, texconv, inputs=(), dnd=False):
        self.root = root
        self.magick = magick
        self.texconv = texconv
        self.dnd = dnd
        self.magick_version, self.magick_error = (
            magick_version(magick) if magick else (None, None)
        )
        self.magick_ok = bool(magick) and not self.magick_error

        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.close_pending = False
        self.closed = False
        self.results = []  # FileResults of the last run, in order
        self.rows = {}  # tree item id -> (FileResult, or None for unmatched input, values)
        self.run_info = None  # settings and totals of the last run
        self.base_dir = ""
        self.sort_state = (None, False)  # (column, descending)
        self.last_dir = None

        self.mode = tk.StringVar(value="convert")
        self.recursive = tk.BooleanVar(value=False)
        self.bc7 = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        self.dry_run = tk.BooleanVar(value=False)
        self.out_dir = tk.StringVar()
        self.opaque_alpha = tk.StringVar(value=str(dds_tool.DEFAULT_OPAQUE_ALPHA))
        self.progress_text = tk.StringVar()
        self.summary_text = tk.StringVar()
        self.folder_text = tk.StringVar()
        self.detail_text = tk.StringVar(value="Select a row to see its details in full.")

        font = tkfont.nametofont("TkDefaultFont")
        self.char = font.measure("0")
        self.link_font = font.copy()  # kept here: Tk drops a Font once it is collected
        self.link_font.configure(underline=True)
        ttk.Style(root).configure("Treeview", rowheight=int(font.metrics("linespace") * 1.4))
        self.build()
        self.add_paths(inputs)
        self.update_controls()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- layout ---------------------------------------------------------

    def build(self):
        root = self.root
        root.title(TITLE)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        panes = ttk.PanedWindow(root, orient="vertical")
        panes.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        top = ttk.Frame(panes)
        bottom = ttk.Frame(panes)
        panes.add(top, weight=0)
        panes.add(bottom, weight=1)

        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        self.build_inputs(top).grid(row=0, column=0, sticky="nsew")
        self.build_options(top).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.build_run(top).grid(row=2, column=0, sticky="ew", pady=6)
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(0, weight=1)
        self.build_results(bottom).grid(row=0, column=0, sticky="nsew")
        self.build_summary(root).grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        self.build_status(root).grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))
        root.minsize(70 * self.char, 0)

    def build_inputs(self, parent):
        frame = ttk.LabelFrame(parent, text="Inputs", padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(frame, selectmode="extended", height=6, activestyle="none")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.bind("<Delete>", lambda e: self.remove_selected())

        if self.dnd:
            hint = "Drop files or folders here, or use the buttons on the right"
        else:
            hint = "Use Add Files or Add Folder to choose what to process"
        self.hint = tk.Label(
            self.listbox, text=hint, foreground="gray45", background=self.listbox["background"]
        )
        if self.dnd:
            for widget in (self.listbox, self.hint):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)

        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=2, sticky="n", padx=(6, 0))
        self.input_buttons = []
        for text, command in (
            ("Add Files…", self.add_files),
            ("Add Folder…", self.add_folder),
            ("Remove", self.remove_selected),
            ("Clear", self.clear_inputs),
        ):
            button = ttk.Button(buttons, text=text, command=command)
            button.pack(fill="x", pady=1)
            self.input_buttons.append(button)
        self.recursive_check = ttk.Checkbutton(
            buttons, text="Include subfolders", variable=self.recursive
        )
        self.recursive_check.pack(anchor="w", pady=(6, 0))
        return frame

    def build_options(self, parent):
        frame = ttk.LabelFrame(parent, text="Options", padding=6)
        frame.columnconfigure(0, weight=1)

        row = ttk.Frame(frame)
        row.grid(row=0, column=0, sticky="w")
        self.mode_buttons = []
        for text, value in (("Convert images to DDS", "convert"), ("Audit DDS files", "audit")):
            button = ttk.Radiobutton(
                row, text=text, value=value, variable=self.mode, command=self.update_controls
            )
            button.pack(side="left", padx=(0, 16))
            self.mode_buttons.append(button)

        row = ttk.Frame(frame)
        row.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.bc7_check = ttk.Checkbutton(row, text="BC7 instead of DXT5", variable=self.bc7)
        self.force_check = ttk.Checkbutton(
            row, text="Overwrite existing DDS", variable=self.force
        )
        self.dry_check = ttk.Checkbutton(
            row, text="Dry run (write nothing)", variable=self.dry_run
        )
        for widget in (self.bc7_check, self.force_check, self.dry_check):
            widget.pack(side="left", padx=(0, 16))
        ttk.Label(row, text="Suggest DXT1 when lowest alpha ≥").pack(side="left")
        self.alpha_spin = ttk.Spinbox(
            row, from_=0, to=255, width=4, textvariable=self.opaque_alpha
        )
        self.alpha_spin.pack(side="left", padx=(4, 0))

        row = ttk.Frame(frame)
        row.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text="Output folder:").grid(row=0, column=0, sticky="w")
        self.out_entry = ttk.Entry(row, textvariable=self.out_dir)
        self.out_entry.grid(row=0, column=1, sticky="ew", padx=4)
        self.out_browse = ttk.Button(row, text="Browse…", command=self.choose_out_dir)
        self.out_browse.grid(row=0, column=2)
        self.out_clear = ttk.Button(row, text="Clear", command=lambda: self.out_dir.set(""))
        self.out_clear.grid(row=0, column=3, padx=(4, 0))
        ttk.Label(row, text="blank: beside each image", foreground="gray45").grid(
            row=0, column=4, padx=(8, 0)
        )
        self.convert_only = [
            self.force_check, self.dry_check, self.out_entry, self.out_browse, self.out_clear,
        ]
        return frame

    def build_run(self, parent):
        frame = ttk.Frame(parent)
        frame.columnconfigure(2, weight=1)
        self.run_button = ttk.Button(frame, text="Run", command=self.start)
        self.run_button.grid(row=0, column=0)
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self.cancel_run)
        self.cancel_button.grid(row=0, column=1, padx=(4, 8))
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=0, column=2, sticky="ew")
        ttk.Label(frame, textvariable=self.progress_text, width=1).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0)
        )
        return frame

    def build_results(self, parent):
        frame = ttk.LabelFrame(parent, text="Results", padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, textvariable=self.folder_text, width=1).grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4)
        )
        self.tree = ttk.Treeview(
            frame, columns=[c[0] for c in COLUMNS], show="headings", height=12
        )
        for cid, heading, chars, anchor in COLUMNS:
            self.tree.heading(cid, text=heading, anchor=anchor,
                              command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, width=chars * self.char, minwidth=4 * self.char,
                             anchor=anchor, stretch=cid in ("file", "details"))
        for tag, colour in ROW_COLOURS.items():
            self.tree.tag_configure(tag, background=colour)
        vscroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        vscroll.grid(row=1, column=1, sticky="ns")
        hscroll.grid(row=2, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_selected())

        # the selected row in full, since long details get cut off in the table
        detail = ttk.Label(frame, textvariable=self.detail_text, justify="left")
        detail.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        frame.bind("<Configure>", lambda e: detail.configure(wraplength=max(e.width - 20, 100)))
        return frame

    def build_summary(self, parent):
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, textvariable=self.summary_text, width=1).grid(
            row=0, column=0, sticky="ew"
        )
        self.copy_button = ttk.Button(frame, text="Copy log", command=self.copy_log)
        self.copy_button.grid(row=0, column=1, padx=(8, 0))
        self.csv_button = ttk.Button(frame, text="Export CSV…", command=self.export_csv)
        self.csv_button.grid(row=0, column=2, padx=(4, 0))
        return frame

    def build_status(self, parent):
        frame = ttk.Frame(parent)
        if self.magick_ok:
            text = f"ImageMagick {self.magick_version}: {self.magick}"
        elif self.magick:
            text = f"ImageMagick cannot run ({self.magick_error}): {self.magick}"
        else:
            text = "ImageMagick: missing -- it is required"
        self.magick_status = self.status_label(frame, 0, text, self.magick_ok, dds_tool.MAGICK_URL)
        if self.texconv:
            text = f"texconv: {self.texconv}"
        else:
            text = "texconv: missing -- needed to write BC7 and to read BC7 alpha"
        self.texconv_status = self.status_label(
            frame, 1, text, bool(self.texconv), dds_tool.TEXCONV_URL
        )
        frame.columnconfigure(0, weight=1)
        return frame

    def status_label(self, frame, row, text, ok, url):
        label = ttk.Label(frame, text=text, width=1)
        if not ok:
            label.configure(foreground="#b00020")
        label.grid(row=row, column=0, sticky="ew")
        if not ok:
            link = ttk.Label(frame, text="Download", foreground="#1a55c4", cursor="hand2",
                             font=self.link_font)
            link.grid(row=row, column=1, sticky="e", padx=(8, 0))
            link.bind("<Button-1>", lambda e: webbrowser.open(url))
        return label

    # ---- inputs ---------------------------------------------------------

    def add_paths(self, paths):
        """Add files or folders to the input list, skipping ones already there."""
        existing = {os.path.normcase(p) for p in self.listbox.get(0, "end")}
        for path in paths:
            if not path:
                continue
            path = os.path.abspath(path)
            if os.path.normcase(path) not in existing:
                existing.add(os.path.normcase(path))
                self.listbox.insert("end", path)
        self.update_hint()

    def add_files(self):
        convert = self.mode.get() == "convert"
        chosen = filedialog.askopenfilenames(
            parent=self.root,
            title="Add images" if convert else "Add DDS files",
            initialdir=self.last_dir,
            filetypes=IMAGE_TYPES if convert else DDS_TYPES,
        )
        paths = self.root.tk.splitlist(chosen) if chosen else ()
        if paths:
            self.last_dir = os.path.dirname(paths[0])
            self.add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root, title="Add a folder", initialdir=self.last_dir, mustexist=True
        )
        if folder:
            self.last_dir = folder
            self.add_paths([folder])

    def on_drop(self, event):
        if self.worker is None:
            self.add_paths(self.root.tk.splitlist(event.data))
        return event.action

    def remove_selected(self):
        if self.worker is not None:
            return
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)
        self.update_hint()

    def clear_inputs(self):
        self.listbox.delete(0, "end")
        self.update_hint()

    def update_hint(self):
        if self.listbox.size():
            self.hint.place_forget()
        else:
            self.hint.place(relx=0.5, rely=0.5, anchor="center")

    def choose_out_dir(self):
        folder = filedialog.askdirectory(
            parent=self.root, title="Output folder",
            initialdir=self.out_dir.get() or self.last_dir,
        )
        if folder:
            self.out_dir.set(os.path.normpath(folder))

    # ---- running --------------------------------------------------------

    def update_controls(self):
        idle = self.worker is None
        convert = self.mode.get() == "convert"

        def enable(widget, on):
            widget.state(["!disabled"] if on else ["disabled"])

        for widget in self.input_buttons + self.mode_buttons + [
            self.recursive_check, self.bc7_check, self.alpha_spin,
        ]:
            enable(widget, idle)
        for widget in self.convert_only:
            enable(widget, idle and convert)
        self.listbox.configure(state="normal" if idle else "disabled")
        enable(self.run_button, idle and self.magick_ok)
        enable(self.cancel_button, not idle and not self.cancel.is_set())
        enable(self.copy_button, idle and self.run_info is not None)
        enable(self.csv_button, idle and self.run_info is not None
               and self.run_info["mode"] == "audit" and bool(self.results))

    def start(self):
        """The Run button: check the settings, then start the batch."""
        inputs = list(self.listbox.get(0, "end"))
        if not inputs:
            messagebox.showinfo(TITLE, "Add some files or folders first.", parent=self.root)
            return
        try:
            opaque_alpha = int(self.opaque_alpha.get())
            if not 0 <= opaque_alpha <= 255:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                TITLE, "The lowest-alpha threshold must be a whole number from 0 to 255.",
                parent=self.root,
            )
            return

        mode = self.mode.get()
        bc7 = self.bc7.get()
        options = {"bc7": bc7, "opaque_alpha": opaque_alpha}
        dry_run = False
        if mode == "convert":
            dry_run = self.dry_run.get()
            force = self.force.get()
            options.update(
                out_dir=self.out_dir.get().strip() or None, force=force, dry_run=dry_run
            )
            if bc7 and not self.texconv and not dry_run:
                messagebox.showerror(
                    TITLE,
                    "BC7 needs texconv.exe, which was not found.\n\n"
                    f"Download it from {dds_tool.TEXCONV_URL} and put it beside this "
                    "program, or untick BC7.",
                    parent=self.root,
                )
                return
            if force and not dry_run and not messagebox.askyesno(
                TITLE,
                "Overwrite existing DDS is ticked, so DDS files that are already "
                "there will be replaced.\n\nContinue?",
                parent=self.root,
            ):
                return

        self.clear_results()
        self.run_info = {
            "mode": mode, "dry_run": dry_run, "bc7": bc7, "total": 0,
            "unmatched": [], "texconv": bool(self.texconv), "cancelled": False,
            "error": None,
        }
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.progress.configure(value=0, maximum=1)
        self.progress_text.set("Scanning…")
        self.worker = threading.Thread(
            target=work, daemon=True,
            args=(self.queue, self.cancel, mode, inputs, self.recursive.get(), options,
                  self.magick, self.texconv),
        )
        self.worker.start()
        self.update_controls()
        self.root.after(POLL_MS, self.poll)

    def cancel_run(self):
        if self.worker is not None:
            self.cancel.set()
            self.progress_text.set("Cancelling after the current file…")
            self.update_controls()

    def poll(self):
        """Handle what the worker has posted, then check again shortly."""
        for _ in range(POLL_BATCH):
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            self.handle(message)
            if self.closed:
                return
        if self.worker is not None:
            self.root.after(POLL_MS, self.poll)

    def handle(self, message):
        kind, *args = message
        info = self.run_info
        if kind == "scan":
            files, unmatched = args
            info["total"] = len(files)
            info["unmatched"] = unmatched
            self.base_dir = common_folder(files)
            self.folder_text.set(f"In {self.base_dir}" if self.base_dir else "")
            self.progress.configure(maximum=max(len(files), 1))
            for arg in unmatched:
                self.insert_unmatched(arg)
        elif kind == "progress":
            n, total, path = args
            if not self.cancel.is_set():
                if info["mode"] == "audit":
                    verb = "Auditing"
                else:
                    verb = "Checking" if info["dry_run"] else "Converting"
                self.progress_text.set(f"{verb} {n} of {total}: {os.path.basename(path)}")
        elif kind == "result":
            self.results.append(args[0])
            self.insert_result(args[0])
            self.progress.configure(value=len(self.results))
        elif kind == "done":
            self.finish(args[0])

    def finish(self, error):
        self.worker = None
        info = self.run_info
        done, total = len(self.results), info["total"]
        info["error"] = error
        info["cancelled"] = error is None and done < total
        if error:
            self.progress_text.set("Stopped by an unexpected error.")
        elif info["cancelled"]:
            self.progress_text.set(f"Cancelled after {done} of {total} file(s).")
        else:
            self.progress_text.set(f"Done: {done} file(s).")
        self.summary_text.set(self.summary_line())
        if self.sort_state[0]:
            self.apply_sort()
        self.update_controls()
        if self.close_pending:
            self.closed = True
            self.root.destroy()
            return
        if error:
            messagebox.showerror(
                TITLE, "The batch stopped because of an unexpected error:\n\n"
                + error.strip().splitlines()[-1],
                detail=error, parent=self.root,
            )

    def on_close(self):
        if self.worker is None:
            self.closed = True
            self.root.destroy()
        elif messagebox.askyesno(
            TITLE, "A batch is still running.  Stop after the current file and close?",
            parent=self.root,
        ):
            self.close_pending = True
            self.cancel_run()

    # ---- results --------------------------------------------------------

    def clear_results(self):
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        self.results = []
        self.base_dir = ""
        self.summary_text.set("")
        self.folder_text.set("")
        self.detail_text.set("Select a row to see its details in full.")

    def display_path(self, path):
        return os.path.relpath(path, self.base_dir) if self.base_dir else path

    def insert_result(self, r):
        if self.run_info["mode"] == "convert":
            result, details, tag = convert_row(r, self.run_info["dry_run"])
        else:
            result, details, tag = audit_row(r)
        values = (
            self.display_path(r.path),
            result,
            r.format + (" cube" if r.cubemap else ""),
            "" if r.width is None else f"{r.width}x{r.height}",
            "" if r.mips is None else str(r.mips),
            alpha_text(r),
            details,
        )
        iid = self.tree.insert("", "end", values=values, tags=(tag,))
        self.rows[iid] = (r, values)

    def insert_unmatched(self, arg):
        convert = self.run_info["mode"] == "convert"
        values = (
            arg, "no files", "", "", "", "",
            "no matching image files" if convert else "no DDS files found here",
        )
        iid = self.tree.insert("", "end", values=values, tags=("error" if convert else "issue",))
        self.rows[iid] = (None, values)

    def show_selected(self):
        selected = self.tree.selection()
        if selected and selected[0] in self.rows:
            r, values = self.rows[selected[0]]
            path = r.path if r else values[0]
            self.detail_text.set(f"{path}\n{values[6]}" if values[6] else path)

    def sort_by(self, column):
        """Heading click: sort by column, or reverse the sort if it already is."""
        previous, descending = self.sort_state
        self.sort_state = (column, not descending if previous == column else False)
        self.apply_sort()

    def apply_sort(self):
        column, descending = self.sort_state
        index = [c[0] for c in COLUMNS].index(column)
        order = sorted(
            self.rows, key=lambda iid: sort_key(column, index, *self.rows[iid]),
            reverse=descending,
        )
        for position, iid in enumerate(order):
            self.tree.move(iid, "", position)
        for cid, heading, _, _ in COLUMNS:
            arrow = (" ▼" if descending else " ▲") if cid == column else ""
            self.tree.heading(cid, text=heading + arrow)

    def summary_line(self):
        """One line for the bar under the table, built on the CLI's summary."""
        info, results = self.run_info, self.results
        total, unmatched = info["total"], info["unmatched"]
        if info["mode"] == "convert":
            first = dds_tool.convert_summary(results, total, unmatched, info["dry_run"])[0]
            parts = [first.rstrip(".")]
            skipped = sum(r.status == dds_tool.SKIPPED for r in results)
            errors = len(unmatched) + sum(r.status == dds_tool.ERROR for r in results)
            suggestions = sum(bool(r.suggestion) for r in results)
            if skipped:
                parts.append(f"{skipped} skipped")
            if errors:
                parts.append(f"{errors} error(s)")
            if suggestions:
                parts.append(f"{suggestions} suggestion(s)")
        else:
            first = dds_tool.audit_summary(results, total, unmatched, info["texconv"])[0]
            parts = [first.rstrip(".")]
            unread = sum(bool(r.alpha_note) for r in results)
            if unread:
                parts.append(f"alpha not read for {unread}")
            if unmatched:
                parts.append(f"{len(unmatched)} input(s) with no DDS files")
        if info["cancelled"]:
            parts.append(f"cancelled after {len(results)} of {total}")
        if info["error"]:
            parts.append("stopped by an error")
        return "  ·  ".join(parts)

    def log_text(self):
        """The last run's report, exactly as the command line would print it."""
        info, results = self.run_info, self.results
        total, unmatched = info["total"], info["unmatched"]
        if info["mode"] == "convert":
            header = dds_tool.convert_header(total, info["dry_run"])
            lines = [dds_tool.convert_line(r) for r in results]
            summary = dds_tool.convert_summary(results, total, unmatched, info["dry_run"])
        else:
            header = dds_tool.audit_header(total, info["bc7"])
            lines = [dds_tool.audit_line(r) for r in results]
            summary = dds_tool.audit_summary(results, total, unmatched, info["texconv"])
        if info["cancelled"]:
            summary += ["", f"Cancelled after {len(results)} of {total} file(s)."]
        if info["error"]:
            summary += ["", "Stopped by an unexpected error:", info["error"].rstrip()]
        return dds_tool.report_text(header, lines, summary)

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text())
        self.progress_text.set("Log copied to the clipboard.")

    def export_csv(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export audit CSV", initialdir=self.last_dir,
            initialfile="dds_audit.csv", defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.save_csv(os.path.normpath(path))

    def save_csv(self, path):
        try:
            rows = dds_tool.write_audit_csv(path, self.results)
        except OSError as exc:
            messagebox.showerror(TITLE, f"Could not write {path}:\n\n{exc}", parent=self.root)
            return
        self.progress_text.set(f"Wrote {rows} row(s) to {path}")


def make_root():
    """A Tk root with drag-and-drop if tkinterdnd2 can provide it: (root, dnd)."""
    if TkinterDnD is not None:
        try:
            return TkinterDnD.Tk(), True
        except (RuntimeError, tk.TclError):
            pass  # e.g. its tkdnd library does not match this Tk
    return tk.Tk(), False


def main():
    set_dpi_awareness()
    root, dnd = make_root()
    App(root, dds_tool.find_magick(), dds_tool.find_texconv(), sys.argv[1:], dnd=dnd)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
