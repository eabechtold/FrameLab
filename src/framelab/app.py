"""
FrameLab

Desktop application for frame-accurate video inspection, trimming,
slow-motion export, and frame extraction.

Requires:
    pip install opencv-python pillow sv-ttk

FrameLab uses the bundled FFmpeg executable when it is available and falls
back to an ffmpeg executable on PATH for development environments.

sv_ttk is optional. If it is not installed, the app falls back to the best
available built-in ttk theme.
"""

import csv
import ctypes
import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import zipfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
from framelab import spreadsheet
from PIL import Image, ImageDraw, ImageTk

try:
    import sv_ttk
except ImportError:  # App still works without sv_ttk.
    sv_ttk = None


# Video/export defaults are centralized here so UI code and worker code use
# the same encoding assumptions.
DEFAULT_OUTPUT_SPEED = 0.1
OUTPUT_FPS = 30.0
FLAG_STRIP_HEIGHT = 20
SLIDER_END_PAD = 10  # approximate ttk.Scale thumb half-width
# Flag colors cycle in this order as timestamps are added.
TIMESTAMP_COLORS = ("#ef4444", "#f59e0b", "#22c55e", "#06b6d4", "#3b82f6", "#a855f7", "#ec4899", "#eab308")
PROXY_CRF = "23"
PROXY_PRESET = "ultrafast"
PREVIEW_MAX_UPSCALE = 1.0      # 1.0 = never enlarge beyond source resolution
POLL_MS = 50
MAX_PIXEL_SCALE = 16.0  # most screen pixels per source pixel when zoomed
ZOOM_STEP = 1.25  # zoom multiplier per mouse-wheel notch


def open_video_capture(path):
    """Open a video with OpenCV's FFmpeg backend for responsive seeking."""
    return cv2.VideoCapture(path, cv2.CAP_FFMPEG)


def find_ffmpeg_executable():
    """Return FrameLab's bundled FFmpeg, falling back to the system PATH."""
    candidates = []
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        candidates.extend(
            (
                bundle_root / "ffmpeg" / "ffmpeg.exe",
                Path(sys.executable).resolve().parent / "ffmpeg" / "ffmpeg.exe",
            )
        )
    else:
        project_root = Path(__file__).resolve().parents[2]
        candidates.append(project_root / "vendor" / "ffmpeg" / "ffmpeg.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffmpeg")



class Tooltip:
    """Show delayed help text in a small borderless popup."""

    def __init__(self, widget, text_getter, delay_ms=350):
        self.widget = widget
        self.text_getter = text_getter
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        self._after_id = None
        text = self.text_getter() if callable(self.text_getter) else str(self.text_getter)
        if not text:
            return

        self._hide()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8

        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            self._tip,
            text=text,
            justify=tk.LEFT,
            background="#ffffe0",
            foreground="#111111",
            relief=tk.SOLID,
            borderwidth=1,
            padx=8,
            pady=5,
            wraplength=900,
        )
        label.pack()

    def _hide(self, event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class FrameLabApplication:
    """Own the FrameLab interface and coordinate video-processing jobs.

    Tkinter is not thread-safe, so proxy creation and exports run in worker
    threads. Workers communicate with the main thread exclusively through
    ``ui_queue``; ``_process_ui_events`` applies their updates to the UI.
    """
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FrameLab")
        self.root.geometry("1500x900")
        self.root.minsize(1120, 680)
        self.root.state("zoomed")

        # Video/source state
        self.source_path = None
        self.proxy_path = None
        self.folder = None
        self.filename = None
        self.name = None
        self.ext = None

        self.cap = None
        self.fps = 0.0
        self.frame_count = 0
        self.current_frame = 0
        self.start_frame = None
        self.stop_frame = None
        self.current_tk_image = None
        self.resize_job = None
        self.timestamps = []  # list of {"frame": int, "description": str}
        self._flag_icons = {}

        # Preview zoom/pan. The view is the crop of the frame starting at
        # (view_x, view_y) in normalized frame coordinates and spanning 1/zoom of
        # each axis. The decoded frame is cached so zooming never re-decodes.
        self.zoom = 1.0
        self.view_x = 0.0
        self.view_y = 0.0
        self._frame_bgr = None
        self._preview_box = None  # (x, y, width, height) of the drawn image on the canvas
        self._redraw_job = None
        self._pan_anchor = None
        self._scale = 1.0  # screen pixels per source pixel in the last draw
        self.timestamp_sort = ("num", False)  # (column id, descending)

        # UI/work state
        self.busy = False
        self.output_filename_user_edited = False
        self.ui_queue = queue.Queue()
        self.ffmpeg_exe = None

        # Variables
        self.delete_proxy_on_close_var = tk.BooleanVar(value=False)
        self.speed_var = tk.StringVar(value=str(DEFAULT_OUTPUT_SPEED))
        self.output_filename_var = tk.StringVar(value="")
        self.image_start_var = tk.StringVar(value="")
        self.image_stop_var = tk.StringVar(value="")
        self.image_step_var = tk.StringVar(value="1")
        self.image_monochrome_var = tk.BooleanVar(value=True)
        self.image_to_frames_subfolder_var = tk.BooleanVar(value=True)
        self.image_basename_var = tk.StringVar(value="")
        self.image_subfolder_var = tk.StringVar(value="Frames")
        self.current_frame_var = tk.StringVar(value="0")
        self.timestamp_description_var = tk.StringVar(value="")
        self._updating_slider = False

        self._configure_theme()
        self._create_widgets()
        self._bind_events()

        self._process_ui_events()

    # -- Interface construction -------------------------------------------------
    def _configure_theme(self):
        if sv_ttk is not None:
            sv_ttk.use_dark_theme()
            return

        style = ttk.Style(self.root)
        preferred = "clam" if "clam" in style.theme_names() else style.theme_use()
        style.theme_use(preferred)
        style.configure("TFrame", background="#1f1f1f")
        style.configure("TLabelframe", background="#1f1f1f", borderwidth=1)
        style.configure("TLabelframe.Label", background="#1f1f1f", foreground="#f3f3f3")
        style.configure("TLabel", background="#1f1f1f", foreground="#f3f3f3")
        style.configure("TButton", padding=(10, 6))
        style.configure("Accent.TButton", padding=(12, 7))

    def _create_widgets(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # ----- Top command bar -----
        self.toolbar = ttk.Frame(self.root, padding=(10, 8))
        self.toolbar.grid(row=0, column=0, sticky="ew")
        self.toolbar.columnconfigure(10, weight=1)

        self.browse_button = ttk.Button(self.toolbar, text="Browse Video", command=self.browse_video)
        self.browse_button.grid(row=0, column=0, padx=(0, 6))

        self.copy_button = ttk.Button(self.toolbar, text="Copy Frame", command=self.copy_current_frame_image)
        self.copy_button.grid(row=0, column=1, padx=6)

        self.save_frame_button = ttk.Button(self.toolbar, text="Save Frame BMP", command=self.save_current_frame_bitmap)
        self.save_frame_button.grid(row=0, column=2, padx=6)


        self.file_label = ttk.Label(self.toolbar, text="No video loaded", anchor="e")
        self.file_label.grid(row=0, column=10, sticky="e", padx=(20, 0))
        self.file_path_tooltip = Tooltip(self.file_label, lambda: self.source_path or "No video loaded")

        # ----- Main content: preview + right inspector -----
        # A vertical pane lets the user drag the sash to trade video size for
        # control-panel (e.g. timestamp table) height.
        self.vertical_pane = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        self.vertical_pane.grid(row=1, column=0, sticky="nsew")
        self.main_holder = ttk.Frame(self.vertical_pane, padding=(10, 0, 10, 6))
        self.main_holder.columnconfigure(0, weight=1)
        self.main_holder.rowconfigure(0, weight=1)
        self.main_pane = ttk.PanedWindow(self.main_holder, orient=tk.HORIZONTAL)
        self.main_pane.grid(row=0, column=0, sticky="nsew")

        self.preview_shell = ttk.Frame(self.main_pane, padding=0)
        self.preview_shell.columnconfigure(0, weight=1)
        self.preview_shell.rowconfigure(0, weight=1)

        self.video_canvas = tk.Canvas(self.preview_shell, bg="black", highlightthickness=0, bd=0)
        self.video_canvas.grid(row=0, column=0, sticky="nsew")
        self.video_canvas.create_text(
            0, 0,
            text="Browse to load a video",
            fill="#9ca3af",
            tags=("empty_text",),
            anchor="center",
            font=("Segoe UI", 16),
        )

        # Right inspector is scrollable so controls stay reachable when the
        # window height is reduced. The visible container is fixed-width-ish,
        # while the inner frame expands to the canvas width.
        self.inspector_container = ttk.Frame(self.main_pane, padding=(8, 0, 0, 0), width=300)
        self.inspector_container.grid_propagate(False)
        self.inspector_container.columnconfigure(0, weight=1)
        self.inspector_container.rowconfigure(0, weight=1)

        self.inspector_canvas = tk.Canvas(self.inspector_container, highlightthickness=0, bd=0)
        self.inspector_scrollbar = ttk.Scrollbar(
            self.inspector_container, orient="vertical", command=self.inspector_canvas.yview
        )
        self.inspector = ttk.Frame(self.inspector_canvas)
        self.inspector.columnconfigure(0, weight=1)

        self.inspector_window = self.inspector_canvas.create_window((0, 0), window=self.inspector, anchor="nw")
        self.inspector_canvas.configure(yscrollcommand=self.inspector_scrollbar.set)
        self.inspector_canvas.grid(row=0, column=0, sticky="nsew")
        self.inspector_scrollbar.grid(row=0, column=1, sticky="ns")

        self.main_pane.add(self.preview_shell, weight=5)
        self.main_pane.add(self.inspector_container, weight=0)

        # ----- Right inspector cards -----
        self.info_card = ttk.LabelFrame(self.inspector, text="Video", padding=10)
        self.info_card.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.info_label = ttk.Label(self.info_card, text="No video loaded", justify=tk.LEFT, wraplength=255)
        self.info_label.pack(fill=tk.X)

        self.marks_card = ttk.LabelFrame(self.inspector, text="Marked Range", padding=10)
        self.marks_card.grid(row=1, column=0, sticky="ew", pady=8)
        self.mark_label = ttk.Label(self.marks_card, text="Start: Not set\nStop:  Not set", justify=tk.LEFT)
        self.mark_label.pack(fill=tk.X)

        mark_buttons = ttk.Frame(self.marks_card)
        mark_buttons.pack(anchor="w", pady=(8, 0))
        self.set_start_button = ttk.Button(mark_buttons, text="Set START  (S)", command=self.set_start, width=14)
        self.set_start_button.grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.set_stop_button = ttk.Button(mark_buttons, text="Set STOP  (E)", command=self.set_stop, width=14)
        self.set_stop_button.grid(row=0, column=1, sticky="w")

        self.progress_card = ttk.LabelFrame(self.inspector, text="Progress", padding=10)
        self.progress_card.grid(row=2, column=0, sticky="ew", pady=8)
        self.progress_label = ttk.Label(self.progress_card, text="Idle", justify=tk.LEFT, wraplength=255)
        self.progress_label.pack(fill=tk.X)
        self.progress_bar = ttk.Progressbar(self.progress_card, mode="determinate", maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(8, 0))

        self.options_card = ttk.LabelFrame(self.inspector, text="Options", padding=10)
        self.options_card.grid(row=3, column=0, sticky="ew", pady=8)
        self.delete_proxy_checkbox = ttk.Checkbutton(
            self.options_card,
            text="Delete proxy on Browse or Close",
            variable=self.delete_proxy_on_close_var,
        )
        self.delete_proxy_checkbox.pack(anchor="w")

        self.inspector.rowconfigure(9, weight=1)

        # ----- Bottom modern control tabs -----
        self.bottom = ttk.Frame(self.vertical_pane, padding=(10, 0, 10, 10))
        self.bottom.columnconfigure(0, weight=1)
        self.bottom.rowconfigure(2, weight=1)
        self.vertical_pane.add(self.main_holder, weight=1)
        self.vertical_pane.add(self.bottom, weight=0)

        # Timestamp flags are drawn on a thin strip directly above the slider.
        self.flag_canvas = tk.Canvas(self.bottom, height=FLAG_STRIP_HEIGHT, highlightthickness=0, bd=0, bg="#1f1f1f")
        self.flag_canvas.grid(row=0, column=0, sticky="ew")
        self.flag_canvas.bind("<Configure>", lambda e: self.draw_flags())

        self.slider = ttk.Scale(self.bottom, from_=0, to=1, command=self.slider_changed)
        self.slider.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.slider.state(["disabled"])

        self.notebook = ttk.Notebook(self.bottom)
        self.notebook.grid(row=2, column=0, sticky="nsew")

        self.nav_tab = ttk.Frame(self.notebook, padding=10)
        self.timestamps_tab = ttk.Frame(self.notebook, padding=10)
        self.clip_tab = ttk.Frame(self.notebook, padding=10)
        self.images_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.nav_tab, text="Navigate")
        self.notebook.add(self.timestamps_tab, text="Timestamps")
        self.notebook.add(self.clip_tab, text="Clip Export")
        self.notebook.add(self.images_tab, text="Frame Images")

        self._create_nav_tab()
        self._create_timestamps_tab()
        self._create_clip_tab()
        self._create_images_tab()

        # ----- Context menu -----
        self.frame_context_menu = tk.Menu(self.root, tearoff=0)
        self.frame_context_menu.add_command(label="Copy image from current frame", command=self.copy_current_frame_image)
        self.frame_context_menu.add_command(label="Save bitmap image from current frame", command=self.save_current_frame_bitmap)

        self.inspector.bind("<Configure>", self._update_inspector_scrollregion)
        self.inspector_canvas.bind("<Configure>", self._resize_inspector_window)
        self.inspector_canvas.bind("<Enter>", self._bind_inspector_mousewheel)
        self.inspector_canvas.bind("<Leave>", self._unbind_inspector_mousewheel)
        self.root.after_idle(self._sync_inspector_scroll_state)

    def _create_nav_tab(self):
        self.nav_tab.columnconfigure(7, weight=1)

        ttk.Label(self.nav_tab, text="Frame step").grid(row=0, column=0, padx=(0, 8), sticky="w")
        for idx, (txt, delta) in enumerate((("−100", -100), ("−10", -10), ("−1", -1), ("+1", 1), ("+10", 10), ("+100", 100)), start=1):
            ttk.Button(self.nav_tab, text=txt, command=lambda d=delta: self.jump_frames(d), width=7).grid(row=0, column=idx, padx=3)

        ttk.Label(self.nav_tab, text="Time step").grid(row=1, column=0, padx=(0, 8), pady=(8, 0), sticky="w")
        for idx, (txt, delta) in enumerate((("−5s", -5.0), ("−1s", -1.0), ("−0.1s", -0.1), ("+0.1s", 0.1), ("+1s", 1.0), ("+5s", 5.0)), start=1):
            ttk.Button(self.nav_tab, text=txt, command=lambda d=delta: self.jump_seconds(d), width=7).grid(row=1, column=idx, padx=3, pady=(8, 0))

        ttk.Label(self.nav_tab, text="Jump to frame").grid(row=0, column=8, padx=(20, 6), sticky="e")
        self.frame_entry = ttk.Entry(self.nav_tab, width=10, textvariable=self.current_frame_var)
        self.frame_entry.grid(row=0, column=9, sticky="e")
        self.frame_entry.bind("<Return>", self.jump_to_frame_from_entry)
        self.frame_entry.bind("<Escape>", lambda e: self.root.focus_force())

    def _create_timestamps_tab(self):
        tab = self.timestamps_tab
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        # Left: entry panel for recording a timestamp at the current frame.
        entry_panel = ttk.Frame(tab)
        entry_panel.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        entry_panel.columnconfigure(0, weight=1)
        ttk.Label(entry_panel, text="Description", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.timestamp_entry = ttk.Entry(entry_panel, width=34, textvariable=self.timestamp_description_var)
        self.timestamp_entry.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        self.timestamp_entry.bind("<Return>", lambda e: self.add_timestamp())
        self.timestamp_entry.bind("<Escape>", lambda e: self.root.focus_force())
        self.add_timestamp_button = ttk.Button(
            entry_panel, text="Add Timestamp  (T)", command=self.add_timestamp, style="Accent.TButton",
        )
        self.add_timestamp_button.grid(row=2, column=0, sticky="ew")
        self.timestamp_position_label = ttk.Label(entry_panel, text="No video loaded", foreground="#9ca3af")
        self.timestamp_position_label.grid(row=3, column=0, sticky="w", pady=(8, 0))

        # Center: the timestamp table. Colored dots in the tree column match
        # the flags drawn above the slider.
        style = ttk.Style(self.root)
        style.configure("Timestamps.Treeview", rowheight=26)
        style.configure("Timestamps.Treeview.Heading", font=("Segoe UI", 9, "bold"), padding=(8, 5))
        table = ttk.Frame(tab)
        table.grid(row=0, column=1, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.timestamp_tree = ttk.Treeview(
            table, columns=("num", "time", "frame", "description"), show="tree headings",
            height=5, selectmode="browse", style="Timestamps.Treeview",
        )
        self.timestamp_tree.heading(
            "#0", text="Flag", anchor="center", command=lambda: self.sort_timestamps_by("#0"),
        )
        self.timestamp_tree.column("#0", width=56, minwidth=56, stretch=False, anchor="center")
        for column, heading, width, anchor, stretch in (
            ("num", "#", 50, "w", False),
            ("time", "Time", 110, "w", False),
            ("frame", "Frame", 90, "w", False),
            ("description", "Description", 360, "w", True),
        ):
            self.timestamp_tree.heading(
                column, text=heading, anchor=anchor, command=lambda c=column: self.sort_timestamps_by(c),
            )
            self.timestamp_tree.column(column, width=width, minwidth=width, stretch=stretch, anchor=anchor)
        tree_scroll = ttk.Scrollbar(table, orient="vertical", command=self.timestamp_tree.yview)
        self.timestamp_tree.configure(yscrollcommand=tree_scroll.set)
        self.timestamp_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.timestamp_tree.bind("<<TreeviewSelect>>", lambda e: self.jump_to_selected_timestamp())
        self.timestamp_tree.bind("<Delete>", lambda e: self.delete_timestamp())

        # Right: actions that operate on the whole table or selection.
        actions = ttk.Frame(tab)
        actions.grid(row=0, column=2, sticky="ns", padx=(16, 0))
        actions.columnconfigure(0, weight=1)
        transfer = ttk.Frame(actions)
        transfer.grid(row=0, column=0, sticky="ew")
        transfer.columnconfigure((0, 1), weight=1)
        self.import_timestamps_button = ttk.Button(
            transfer, text="Import\u2026", command=self.import_timestamps, width=8)
        self.import_timestamps_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.export_timestamps_button = ttk.Button(
            transfer, text="Export\u2026", command=self.export_timestamps, width=8)
        self.export_timestamps_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.copy_timestamps_button = ttk.Button(
            actions, text="Copy for Excel", command=self.copy_timestamps_to_clipboard, width=16)
        self.copy_timestamps_button.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.delete_timestamp_button = ttk.Button(
            actions, text="Delete Selected", command=self.delete_timestamp, width=16)
        self.delete_timestamp_button.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Separator(actions, orient="horizontal").grid(row=3, column=0, sticky="ew", pady=10)
        self.set_fps_button = ttk.Button(
            actions, text="Set Frame Rate…", command=self.set_frame_rate, width=16)
        self.set_fps_button.grid(row=4, column=0, sticky="ew")

    def _create_clip_tab(self):
        # Compact two-row layout:
        # Row 1: Output Speed, entry, inline help text.
        # Row 2: Save As, filename entry. The filename field expands.
        self.clip_tab.columnconfigure(2, weight=1)

        ttk.Label(self.clip_tab, text="Output Speed").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.speed_entry = ttk.Entry(self.clip_tab, width=8, textvariable=self.speed_var)
        self.speed_entry.grid(row=0, column=1, sticky="w", padx=(0, 14))
        self.speed_entry.bind("<FocusOut>", lambda e: self.update_default_output_filename_if_allowed())
        self.speed_entry.bind("<Return>", lambda e: self.update_default_output_filename_if_allowed())
        self.speed_entry.bind("<Escape>", lambda e: self.root.focus_force())

        self.clip_help_label = ttk.Label(
            self.clip_tab,
            text="1.0 = normal speed • 0.1 = 10% speed • Output = 30 FPS, no audio",
            anchor="w",
        )
        self.clip_help_label.grid(row=0, column=2, sticky="ew")

        ttk.Label(self.clip_tab, text="Save As").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        self.output_filename_entry = ttk.Entry(self.clip_tab, textvariable=self.output_filename_var)
        self.output_filename_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self.output_filename_entry.bind("<KeyRelease>", lambda e: self.mark_output_filename_edited())
        self.output_filename_entry.bind("<Escape>", lambda e: self.root.focus_force())

        # Keep Export Clip in this tab, but avoid adding another full-height settings row.
        # It is compactly aligned at the lower-right edge of the tab.
        self.export_button = ttk.Button(
            self.clip_tab,
            text="Export Clip",
            command=self.export_clip,
            takefocus=False,
            width=14,
        )
        self.export_button.grid(row=1, column=3, sticky="e", padx=(12, 0), pady=(10, 0))

    def _create_images_tab(self):
        self.images_tab.columnconfigure(0, weight=1)
        range_row = ttk.Frame(self.images_tab)
        range_row.grid(row=0, column=0, sticky="ew")
        range_row.columnconfigure(7, weight=1)
        naming_row = ttk.Frame(self.images_tab)
        naming_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        naming_row.columnconfigure(1, weight=3)
        naming_row.columnconfigure(3, weight=2)

        for column, (label, variable, attribute, width) in enumerate((
            ("Start", self.image_start_var, "image_start_entry", 10),
            ("Stop", self.image_stop_var, "image_stop_entry", 10),
            ("Step", self.image_step_var, "image_step_entry", 8),
        )):
            ttk.Label(range_row, text=label).grid(
                row=0, column=column * 2, sticky="w", padx=(0, 6),
            )
            entry = ttk.Entry(range_row, width=width, textvariable=variable)
            entry.grid(row=0, column=column * 2 + 1, sticky="w", padx=(0, 14))
            setattr(self, attribute, entry)

        self.use_marked_range_button = ttk.Button(
            range_row, text="Use START/STOP", command=self.populate_image_range_from_marks,
        )
        self.use_marked_range_button.grid(row=0, column=6, sticky="w")
        self.image_monochrome_checkbox = ttk.Checkbutton(
            range_row, text="Monochrome", variable=self.image_monochrome_var,
        )
        self.image_monochrome_checkbox.grid(row=0, column=8, padx=(16, 14), sticky="w")
        self.export_images_button = ttk.Button(
            range_row, text="Export Images", command=self.export_frame_images, takefocus=False,
        )
        self.export_images_button.grid(row=0, column=9, sticky="e")

        ttk.Label(naming_row, text="Basename").grid(
            row=0, column=0, sticky="w", padx=(0, 6),
        )
        self.image_basename_entry = ttk.Entry(
            naming_row, textvariable=self.image_basename_var, width=24,
        )
        self.image_basename_entry.grid(row=0, column=1, sticky="ew")
        self.image_to_frames_subfolder_checkbox = ttk.Checkbutton(
            naming_row, text="Save to subfolder", variable=self.image_to_frames_subfolder_var,
            command=self.update_image_subfolder_state,
        )
        self.image_to_frames_subfolder_checkbox.grid(
            row=0, column=2, sticky="w", padx=(16, 6),
        )
        ttk.Style(self.root).map(
            "Subfolder.TEntry", foreground=[("disabled", "#808080")],
        )
        self.image_subfolder_entry = ttk.Entry(
            naming_row, textvariable=self.image_subfolder_var, width=18,
            style="Subfolder.TEntry",
        )
        self.image_subfolder_entry.grid(row=0, column=3, sticky="ew")
        self.image_subfolder_entry.bind(
            "<<ThemeChanged>>",
            lambda event: self.image_subfolder_entry.after_idle(self.update_image_subfolder_state),
            add="+",
        )
        self.update_image_subfolder_state()

        for entry in (
            self.image_start_entry,
            self.image_stop_entry,
            self.image_step_entry,
            self.image_basename_entry,
            self.image_subfolder_entry,
        ):
            entry.bind(
                "<Escape>",
                lambda e: self.root.focus_force(),
            )

    def _update_inspector_scrollregion(self, event=None):
        self._sync_inspector_scroll_state()

    def _resize_inspector_window(self, event):
        self.inspector_canvas.itemconfigure(self.inspector_window, width=event.width)
        self._sync_inspector_scroll_state()

    def _sync_inspector_scroll_state(self):
        """Only allow right-pane scrolling when the controls do not fit."""
        self.inspector.update_idletasks()
        bbox = self.inspector_canvas.bbox("all")
        if bbox is None:
            self.inspector_scrollbar.grid_remove()
            self.inspector_canvas.configure(scrollregion=(0, 0, 0, 0))
            self._inspector_scroll_enabled = False
            return

        content_height = max(0, bbox[3] - bbox[1])
        viewport_height = max(1, self.inspector_canvas.winfo_height())
        needs_scroll = content_height > viewport_height + 1

        if needs_scroll:
            self.inspector_canvas.configure(scrollregion=bbox)
            if not self.inspector_scrollbar.winfo_ismapped():
                self.inspector_scrollbar.grid(row=0, column=1, sticky="ns")
            self._inspector_scroll_enabled = True
        else:
            self.inspector_scrollbar.grid_remove()
            self.inspector_canvas.yview_moveto(0)
            viewport_width = max(1, self.inspector_canvas.winfo_width())
            self.inspector_canvas.configure(scrollregion=(0, 0, viewport_width, viewport_height))
            self._inspector_scroll_enabled = False

    def _bind_inspector_mousewheel(self, event=None):
        if self._inspector_scroll_enabled:
            self.inspector_canvas.bind_all("<MouseWheel>", self._on_inspector_mousewheel)

    def _unbind_inspector_mousewheel(self, event=None):
        self.inspector_canvas.unbind_all("<MouseWheel>")

    def _on_inspector_mousewheel(self, event):
        if self.text_entry_has_focus() or not self._inspector_scroll_enabled:
            return "break"

        delta = -1 * int(event.delta / 120)
        if delta == 0:
            delta = -1 if event.delta > 0 else 1

        first, last = self.inspector_canvas.yview()
        if (delta < 0 and first <= 0.0) or (delta > 0 and last >= 1.0):
            return "break"

        self.inspector_canvas.yview_scroll(delta, "units")
        return "break"

    def _bind_events(self):
        """Connect mouse, keyboard, and window events to application actions."""
        self.video_canvas.bind("<Configure>", self.on_video_resize)
        self.video_canvas.bind("<Button-1>", lambda e: self.root.focus_force())
        self.video_canvas.bind("<Button-3>", self.show_frame_context_menu)
        self.video_canvas.bind("<MouseWheel>", self.on_video_mousewheel)
        self.video_canvas.bind("<ButtonPress-1>", self.on_pan_start, add="+")
        self.video_canvas.bind("<B1-Motion>", self.on_pan_drag)
        self.video_canvas.bind("<Double-Button-1>", lambda e: self.reset_zoom())

        self.root.bind("<Left>", lambda e: self.run_hotkey(lambda: self.jump_frames(-1)))
        self.root.bind("<Right>", lambda e: self.run_hotkey(lambda: self.jump_frames(1)))
        self.root.bind("<Shift-Left>", lambda e: self.run_hotkey(lambda: self.jump_frames(-10)))
        self.root.bind("<Shift-Right>", lambda e: self.run_hotkey(lambda: self.jump_frames(10)))
        self.root.bind("<Control-Left>", lambda e: self.run_hotkey(lambda: self.jump_frames(-100)))
        self.root.bind("<Control-Right>", lambda e: self.run_hotkey(lambda: self.jump_frames(100)))
        # Letter hotkeys are handled from keysym.lower(), so Caps Lock and
        # Shift do not change behavior. Entry widgets are ignored by run_hotkey.
        self.root.bind("<KeyPress>", self.handle_keypress)
        self.root.bind("<Control-c>", lambda e: self.copy_current_frame_image())
        self.root.bind("<Control-C>", lambda e: self.copy_current_frame_image())
        self.root.bind("<Control-f>", lambda e: self.save_current_frame_bitmap())
        self.root.bind("<Control-F>", lambda e: self.save_current_frame_bitmap())
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def handle_keypress(self, event):
        key = (event.keysym or "").lower()
        if key == "s":
            self.run_hotkey(self.set_start)
        elif key == "e":
            self.run_hotkey(self.set_stop)
        elif key == "q":
            self.run_hotkey(self.export_clip)
        elif key == "t":
            self.run_hotkey(self.add_timestamp)
        elif key == "b":
            self.run_hotkey(self.browse_video)

    # -- Interface state and worker messages -----------------------------------
    def text_entry_has_focus(self):
        focused = self.root.focus_get()
        return isinstance(focused, (tk.Entry, ttk.Entry))

    def run_hotkey(self, action):
        if self.text_entry_has_focus():
            return
        action()

    def set_busy(self, value, status_text=None):
        self.busy = value
        state = "disabled" if value else "!disabled"

        for widget in (
            self.browse_button,
            self.copy_button,
            self.save_frame_button,
            self.set_fps_button,
            self.add_timestamp_button,
            self.delete_timestamp_button,
            self.copy_timestamps_button,
            self.export_timestamps_button,
            self.import_timestamps_button,
            self.export_button,
            self.export_images_button,
            self.use_marked_range_button,
            self.set_start_button,
            self.set_stop_button,
            self.image_to_frames_subfolder_checkbox,
            self.image_monochrome_checkbox,
            self.image_basename_entry,
        ):
            widget.state([state])
        self.update_image_subfolder_state()

        if self.cap is not None and not value:
            self.slider.state(["!disabled"])
        else:
            self.slider.state(["disabled"])

        if status_text is not None:
            self.progress_label.config(text=status_text)

    def set_progress(self, label=None, percent=None):
        if label is not None:
            self.progress_label.config(text=label)
        if percent is not None:
            self.progress_bar["value"] = max(0, min(100, float(percent)))

    def _process_ui_events(self):
        """Apply messages from background workers without touching Tk off-thread."""
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "progress":
                _, label, percent = item
                self.set_progress(label, percent)
            elif kind == "error":
                _, title, message = item
                self.set_busy(False, "Error")
                messagebox.showerror(title, message)
            elif kind == "proxy_done":
                self.set_progress("Opening proxy...", 100)
                self._finish_loading_proxy(item[1])
            elif kind == "export_done":
                _, output_path = item
                self.set_busy(False, "Export complete")
                self.set_progress("Export complete", 100)
                messagebox.showinfo("Done", f"Saved:\n{output_path}")
            elif kind == "image_export_done":
                _, output_dir, saved_count = item
                self.set_busy(False, "Image export complete")
                self.set_progress("Image export complete", 100)
                messagebox.showinfo("Done", f"Saved {saved_count} image(s) to:\n{output_dir}")

        self.root.after(POLL_MS, self._process_ui_events)

    # -- Formatting, validation, and path helpers ------------------------------
    def frame_to_seconds(self, frame_num):
        return 0.0 if self.fps <= 0 else frame_num / self.fps

    def seconds_to_frames(self, seconds):
        return round(seconds * self.fps)

    @staticmethod
    def format_time(seconds):
        minutes = int(seconds // 60)
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:06.3f}"

    @staticmethod
    def sanitize_filename_part(text):
        invalid = '<>:"/\\|?*'
        cleaned = ''.join('_' if ch in invalid else ch for ch in text)
        cleaned = cleaned.strip().strip('.')
        return cleaned or 'frame'

    @staticmethod
    def get_unique_path(path):
        """Return an unused path by appending a numeric suffix when necessary."""
        base, extension = os.path.splitext(path)
        candidate = path
        idx = 1
        while os.path.exists(candidate):
            candidate = f"{base}_{idx:03d}{extension}"
            idx += 1
        return candidate

    def update_info(self):
        if self.cap is None:
            self.info_label.config(text="No video loaded")
            self.timestamp_position_label.config(text="No video loaded")
            self.current_frame_var.set("0")
            return

        current_time = self.format_time(self.frame_to_seconds(self.current_frame))
        total_time = self.format_time(self.frame_to_seconds(self.frame_count - 1))
        self.current_frame_var.set(str(self.current_frame))
        self.timestamp_position_label.config(text=f"Current position: {current_time}  •  frame {self.current_frame}")
        self.info_label.config(
            text=(
                f"File:\n{self.filename}\n\n"
                f"Current Frame:\n{self.current_frame} / {self.frame_count - 1}\n\n"
                f"Current Time:\n{current_time} / {total_time}\n\n"
                f"Source FPS:\n{self.fps:.3f}"
            )
        )

    def update_mark_status(self):
        start_text = (
            f"Frame {self.start_frame} @ {self.format_time(self.frame_to_seconds(self.start_frame))}"
            if self.start_frame is not None else "Not set"
        )
        stop_text = (
            f"Frame {self.stop_frame} @ {self.format_time(self.frame_to_seconds(self.stop_frame))}"
            if self.stop_frame is not None else "Not set"
        )
        self.mark_label.config(text=f"Start: {start_text}\nStop:  {stop_text}")

    def get_output_speed(self):
        try:
            speed = float(self.speed_var.get())
            if speed <= 0:
                raise ValueError
            return speed
        except ValueError:
            messagebox.showerror("Invalid Speed", "Output speed must be a positive number.")
            return None

    def default_output_filename(self, speed=None):
        if not self.name or not self.ext:
            return ""
        if speed is None:
            try:
                speed = float(self.speed_var.get())
            except ValueError:
                speed = DEFAULT_OUTPUT_SPEED
        if speed == 1.0:
            return f"{self.name}_trimmed_30fps_no_audio{self.ext}"
        return f"{self.name}_trimmed_{speed}x_30fps_no_audio{self.ext}"

    def mark_output_filename_edited(self):
        self.output_filename_user_edited = True

    def update_default_output_filename_if_allowed(self):
        if not self.output_filename_user_edited:
            speed = self.get_output_speed()
            if speed is not None:
                self.output_filename_var.set(self.default_output_filename(speed))

    def get_output_path(self):
        proposed = self.output_filename_var.get().strip() or self.default_output_filename(self.get_output_speed())
        proposed = os.path.basename(proposed)
        _, proposed_ext = os.path.splitext(proposed)
        if not proposed_ext:
            proposed += self.ext
        return os.path.join(self.folder, proposed)

    # -- Video lifecycle and all-intra-frame proxy generation ------------------
    def clear_current_video(self, delete_proxy=None):
        """Release the active video and restore the unloaded UI state."""
        if delete_proxy is None:
            delete_proxy = self.delete_proxy_on_close_var.get()

        if self.cap is not None:
            self.cap.release()
            self.cap = None

        if delete_proxy and self.proxy_path and os.path.exists(self.proxy_path):
            try:
                os.remove(self.proxy_path)
            except Exception as e:
                print(f"Could not delete proxy file: {e}")

        self.source_path = None
        self.proxy_path = None
        self.folder = None
        self.filename = None
        self.name = None
        self.ext = None
        self.fps = 0.0
        self.frame_count = 0
        self.current_frame = 0
        self.start_frame = None
        self.stop_frame = None
        self.current_tk_image = None
        self._frame_bgr = None
        self._preview_box = None
        self.zoom, self.view_x, self.view_y = 1.0, 0.0, 0.0
        self.timestamps = []
        self.output_filename_user_edited = False

        self.video_canvas.delete("all")
        self.video_canvas.create_text(
            max(1, self.video_canvas.winfo_width() // 2),
            max(1, self.video_canvas.winfo_height() // 2),
            text="Browse to load a video",
            fill="#9ca3af",
            tags=("empty_text",),
            anchor="center",
            font=("Segoe UI", 16),
        )
        self.file_label.config(text="No video loaded")
        self.output_filename_var.set("")
        self.image_basename_var.set("")
        self.image_start_var.set("")
        self.image_stop_var.set("")
        self.image_step_var.set("1")
        self.slider.configure(from_=0, to=1)
        self.slider.state(["disabled"])
        self.update_mark_status()
        self.update_info()
        self.refresh_timestamps()
        self.root.title("FrameLab")

    def browse_video(self):
        if self.busy:
            messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
            return

        path = filedialog.askopenfilename(
            title="Select video file",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.wmv"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.start_load_video(path)

    def start_load_video(self, path):
        if self.busy:
            return

        self.ffmpeg_exe = find_ffmpeg_executable()
        if self.ffmpeg_exe is None:
            messagebox.showerror(
                "FFmpeg Required",
                "FrameLab's bundled FFmpeg executable could not be found.\n\n"
                "Reinstall FrameLab or provide an FFmpeg installation with libx264 on PATH.",
            )
            return

        self.clear_current_video(delete_proxy=self.delete_proxy_on_close_var.get())
        self.source_path = path
        self.folder, self.filename = os.path.split(self.source_path)
        self.name, self.ext = os.path.splitext(self.filename)
        self.image_basename_var.set(self.name)
        self.proxy_path = os.path.join(self.folder, f"{self.name}_proxy_all_i.mp4")

        self.output_filename_user_edited = False
        self.output_filename_var.set(self.default_output_filename())
        self.file_label.config(text=f"Loading: {self.filename}")
        self.root.title(f"FrameLab - Loading {self.filename}")

        self.set_busy(True, "Importing video / creating proxy...")
        self.set_progress("Importing video / creating proxy...", 0)

        threading.Thread(
            target=self._create_proxy,
            args=(self.source_path, self.proxy_path),
            daemon=True,
        ).start()

    def _create_proxy(self, path, proxy):
        """Create a seek-friendly proxy and post progress to ``ui_queue``."""
        if os.path.exists(proxy):
            self.ui_queue.put(("progress", "Using existing proxy", 100))
            self.ui_queue.put(("proxy_done", path))
            return

        cmd_proxy = [
            self.ffmpeg_exe,
            "-y",
            "-progress", "pipe:2",
            "-nostats",
            "-i", path,
            "-an",
            "-c:v", "libx264",
            "-preset", PROXY_PRESET,
            "-crf", PROXY_CRF,
            "-x264-params", "keyint=1:min-keyint=1:scenecut=0",
            proxy,
        ]

        try:
            process = subprocess.Popen(
                cmd_proxy,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                universal_newlines=True,
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            timestamp_re = re.compile(r"(?:Duration: |time=)(\d+):(\d+):(\d+(?:\.\d+)?)")
            duration_seconds = None

            for line in process.stderr:
                match = timestamp_re.search(line)
                if match and line.lstrip().startswith("Duration:"):
                    hours = int(match.group(1))
                    minutes = int(match.group(2))
                    seconds = float(match.group(3))
                    duration_seconds = hours * 3600 + minutes * 60 + seconds
                    continue

                if duration_seconds and duration_seconds > 0:
                    match = timestamp_re.search(line)
                    if match:
                        hours = int(match.group(1))
                        minutes = int(match.group(2))
                        seconds = float(match.group(3))
                        current = hours * 3600 + minutes * 60 + seconds
                        percent = min(100, (current / duration_seconds) * 100)
                        self.ui_queue.put(("progress", f"Importing video / creating proxy... {percent:0.1f}%", percent))

            rc = process.wait()
            if rc != 0:
                raise subprocess.CalledProcessError(rc, cmd_proxy)

            self.ui_queue.put(("progress", "Import complete", 100))
            self.ui_queue.put(("proxy_done", path))

        except Exception as e:
            self.ui_queue.put(("error", "Proxy Error", f"Failed to create proxy:\n{e}"))

    def _finish_loading_proxy(self, path):
        if path != self.source_path:
            return
        if not self._open_proxy():
            self.clear_current_video(delete_proxy=self.delete_proxy_on_close_var.get())
            self.set_busy(False, "Idle")
            return

        self.file_label.config(text=self.source_path)
        self.root.title(f"FrameLab - {self.filename}")
        self.update_mark_status()
        self.show_frame(0)
        self.draw_flags()
        self.set_busy(False, "Import complete")
        self.set_progress("Import complete", 100)

    def _open_proxy(self):
        self.cap = open_video_capture(self.proxy_path)
        if not self.cap.isOpened():
            messagebox.showerror("Error", "Could not open proxy video.")
            return False

        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if self.fps <= 0 or self.frame_count <= 0:
            messagebox.showerror("Error", "Could not read video FPS or frame count.")
            return False

        self.slider.configure(from_=0, to=self.frame_count - 1)
        self.slider.state(["!disabled"])
        return True

    # -- Timestamps and slider flags -------------------------------------------
    @staticmethod
    def timestamp_color(index):
        return TIMESTAMP_COLORS[index % len(TIMESTAMP_COLORS)]

    def flag_icon(self, color):
        """Return a cached anti-aliased colored dot used in the timestamp table."""
        icon = self._flag_icons.get(color)
        if icon is None:
            size, scale = 14, 4
            big = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
            ImageDraw.Draw(big).ellipse((scale, scale, (size - 1) * scale, (size - 1) * scale), fill=color)
            icon = ImageTk.PhotoImage(big.resize((size, size), Image.LANCZOS))
            self._flag_icons[color] = icon
        return icon

    def add_timestamp(self):
        if self.cap is None or self.busy:
            return
        self.timestamps.append({
            "frame": self.current_frame,
            "description": self.timestamp_description_var.get().strip(),
        })
        self.timestamp_description_var.set("")
        self.refresh_timestamps()
        self.timestamp_tree.see(str(len(self.timestamps) - 1))
        self.root.focus_force()

    def delete_timestamp(self):
        selection = self.timestamp_tree.selection()
        if not selection:
            return
        del self.timestamps[int(selection[0])]
        self.refresh_timestamps()

    def jump_to_selected_timestamp(self):
        selection = self.timestamp_tree.selection()
        if not selection or self.cap is None or self.busy:
            return
        frame = self.timestamps[int(selection[0])]["frame"]
        if frame != self.current_frame:
            self.show_frame(frame)

    def timestamp_rows(self, include_fps=False):
        """Return a header row plus one [#, seconds, frame, description] row per timestamp.

        Seconds are numeric (rounded to milliseconds) so Excel can calculate with them.
        ``include_fps`` appends an FPS column so the frame rate travels with the file.
        """
        rows = [["#", "Time (s)", "Frame", "Description"] + (["FPS"] if include_fps else [])]
        fps = round(self.fps, 3)
        for index in self.timestamp_view_order():
            entry = self.timestamps[index]
            rows.append([
                index + 1,
                round(self.frame_to_seconds(entry["frame"]), 3),
                entry["frame"],
                entry["description"],
            ] + ([fps] if include_fps else []))
        return rows

    def timestamp_text_rows(self, include_fps=False):
        """Rows with the seconds column formatted to a fixed three decimals (text)."""
        return [
            [f"{value:.3f}" if column == 1 and row_index else value for column, value in enumerate(row)]
            for row_index, row in enumerate(self.timestamp_rows(include_fps))
        ]

    def copy_timestamps_to_clipboard(self):
        """Copy timestamps so they paste into Excel cells, seconds shown as 0.000."""
        if not self.timestamps:
            messagebox.showinfo("No Timestamps", "Add at least one timestamp first.")
            return
        try:
            spreadsheet.set_windows_clipboard_table(self.timestamp_rows(), seconds_column=1)
        except Exception:
            # Fall back to plain tab-separated text through Tk.
            buffer = io.StringIO()
            csv.writer(buffer, delimiter="\t", lineterminator="\n").writerows(self.timestamp_text_rows())
            self.root.clipboard_clear()
            self.root.clipboard_append(buffer.getvalue())
            self.root.update()  # keep the data on the clipboard after the call returns
        self.progress_label.config(text=f"Copied {len(self.timestamps)} timestamps to clipboard")

    def export_timestamps(self):
        """Save timestamps as an Excel workbook (.xlsx) or CSV, chosen by file extension."""
        if not self.timestamps:
            messagebox.showinfo("No Timestamps", "Add at least one timestamp first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export timestamps",
            defaultextension=".xlsx",
            initialdir=self.folder,
            initialfile=f"{self.name}_timestamps.xlsx",
            filetypes=[("Excel workbook", "*.xlsx"), ("CSV files", "*.csv")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                # utf-8-sig so Excel detects the encoding.
                with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                    csv.writer(handle).writerows(self.timestamp_text_rows(include_fps=True))
            else:
                spreadsheet.write_xlsx(
                    path, self.timestamp_rows(include_fps=True), seconds_column=1,
                    column_widths=[6, 12, 10, 50, 8],
                )
        except OSError as e:
            messagebox.showerror("Export Error", f"Could not write file:\n{e}")
            return
        self.progress_label.config(text=f"Exported timestamps: {os.path.basename(path)}")

    @staticmethod
    def parse_seconds(value):
        """Parse seconds from a number, ``1.234``, ``mm:ss.mmm`` or ``h:mm:ss.mmm``; else None."""
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", text)
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
        try:
            return float(text)
        except ValueError:
            return None

    def timestamps_from_rows(self, rows):
        """Convert spreadsheet rows to timestamp entries. Returns (entries, skipped_count, fps).

        Uses the header row to find Frame / Time / Description / FPS columns
        (falling back to FrameLab's export layout). A Frame value wins over
        Time. ``fps`` is the frame rate stored in the file, or None. Times are
        converted to frames at that rate when present, else at the current one.
        """
        rows = [row for row in rows if any(cell not in (None, "") for cell in row)]
        if not rows:
            raise ValueError("The file contains no rows.")
        header = [str(cell or "").strip().lower() for cell in rows[0]]

        def find(*prefixes):
            for index, name in enumerate(header):
                if name.startswith(prefixes):
                    return index
            return None

        frame_col, time_col, desc_col = find("frame"), find("time"), find("desc", "note", "comment", "label")
        fps_col = find("fps")
        if frame_col is None and time_col is None:
            if len(rows[0]) < 4:
                raise ValueError("Could not find Time or Frame columns.")
            time_col, frame_col, desc_col = 1, 2, 3  # headerless export layout
            data_rows = rows
        else:
            data_rows = rows[1:]

        def cell_of(row, index):
            return row[index] if index is not None and index < len(row) else None

        file_fps = None
        for row in data_rows:
            candidate = self.parse_seconds(cell_of(row, fps_col))
            if candidate is not None and candidate > 0:
                file_fps = candidate
                break
        fps = file_fps or self.fps

        entries, skipped = [], 0
        for row in data_rows:
            def cell(index):
                return cell_of(row, index)

            frame = None
            raw_frame = cell(frame_col)
            if raw_frame not in (None, ""):
                try:
                    frame = int(round(float(raw_frame)))
                except (TypeError, ValueError):
                    frame = None
            if frame is None:
                seconds = self.parse_seconds(cell(time_col))
                if seconds is not None:
                    frame = round(seconds * fps)
            if frame is None or not 0 <= frame < self.frame_count:
                skipped += 1
                continue
            description = cell(desc_col)
            entries.append({"frame": frame, "description": "" if description is None else str(description).strip()})
        return entries, skipped, file_fps

    def import_timestamps(self):
        """Load timestamps from an .xlsx or .csv file (such as one exported by FrameLab)."""
        if self.cap is None or self.busy:
            messagebox.showinfo("Import Timestamps", "Load a video first so times can be mapped to frames.")
            return
        path = filedialog.askopenfilename(
            title="Import timestamps",
            initialdir=self.folder,
            filetypes=[("Timestamp files", "*.xlsx *.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".xlsx"):
                rows = spreadsheet.read_xlsx(path)
            else:
                with open(path, newline="", encoding="utf-8-sig") as handle:
                    sample = handle.read(4096)
                    handle.seek(0)
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
                    rows = list(csv.reader(handle, dialect))
            entries, skipped, file_fps = self.timestamps_from_rows(rows)
        except (OSError, ValueError, csv.Error, KeyError, zipfile.BadZipFile) as e:
            messagebox.showerror("Import Error", f"Could not import timestamps:\n{e}")
            return
        if not entries:
            messagebox.showwarning("Import Timestamps", "No valid timestamps were found in the file.")
            return

        if self.timestamps:
            choice = messagebox.askyesnocancel(
                "Import Timestamps",
                f"Replace the {len(self.timestamps)} existing timestamps?\n\n"
                "Yes = replace    No = add to existing    Cancel = abort",
            )
            if choice is None:
                return
            if choice:
                self.timestamps = []
        self.timestamps.extend(entries)
        notes = []
        if file_fps is not None and abs(file_fps - self.fps) > 1e-6:
            self.fps = file_fps
            self.update_info()
            self.update_mark_status()
            notes.append(f"frame rate set to {file_fps:g} fps")
        if skipped:
            notes.append(f"{skipped} skipped")
        self.refresh_timestamps()
        note = f" ({', '.join(notes)})" if notes else ""
        self.progress_label.config(text=f"Imported {len(entries)} timestamps{note}")

    TIMESTAMP_HEADINGS = {"#0": "Flag", "num": "#", "time": "Time", "frame": "Frame", "description": "Description"}

    def timestamp_view_order(self):
        """Indices into ``self.timestamps`` in the order currently sorted for display."""
        column, descending = self.timestamp_sort
        keys = {
            "#0": lambda i: i,
            "num": lambda i: i,
            "time": lambda i: (self.timestamps[i]["frame"], i),
            "frame": lambda i: (self.timestamps[i]["frame"], i),
            "description": lambda i: (self.timestamps[i]["description"].lower(), i),
        }
        return sorted(range(len(self.timestamps)), key=keys[column], reverse=descending)

    def sort_timestamps_by(self, column):
        """Sort by ``column``; clicking the active column again reverses the order."""
        active, descending = self.timestamp_sort
        self.timestamp_sort = (column, (not descending) if column == active else False)
        self.refresh_timestamps()

    def refresh_timestamps(self):
        """Rebuild the timestamp table and slider flags from ``self.timestamps``."""
        self.timestamp_tree.delete(*self.timestamp_tree.get_children())
        for index in self.timestamp_view_order():
            entry = self.timestamps[index]
            self.timestamp_tree.insert(
                "", "end", iid=str(index),
                image=self.flag_icon(self.timestamp_color(index)),
                values=(
                    index + 1,
                    self.format_time(self.frame_to_seconds(entry["frame"])),
                    entry["frame"],
                    entry["description"],
                ),
            )
        active, descending = self.timestamp_sort
        for column, heading in self.TIMESTAMP_HEADINGS.items():
            arrow = (" ▼" if descending else " ▲") if column == active else ""
            self.timestamp_tree.heading(column, text=heading + arrow)
        self.draw_flags()

    def draw_flags(self):
        self.flag_canvas.delete("all")
        if self.cap is None or self.frame_count < 2:
            return
        width = self.flag_canvas.winfo_width()
        span = max(1, width - 2 * SLIDER_END_PAD)
        for index, entry in enumerate(self.timestamps):
            color = self.timestamp_color(index)
            x = SLIDER_END_PAD + span * entry["frame"] / (self.frame_count - 1)
            tag = f"flag{index}"
            self.flag_canvas.create_line(x, 2, x, FLAG_STRIP_HEIGHT, fill=color, width=2, tags=(tag,))
            self.flag_canvas.create_polygon(x, 2, x + 11, 6, x, 10, fill=color, outline=color, tags=(tag,))
            self.flag_canvas.tag_bind(tag, "<Button-1>", lambda e, f=entry["frame"]: self.show_frame(f))

    def set_frame_rate(self):
        """Override the source frame rate used for time display and export timing.

        Useful when container metadata (e.g. slow-motion capture rate) was lost.
        """
        if self.cap is None or self.busy:
            return
        value = simpledialog.askfloat(
            "Set Frame Rate",
            "Frame rate (FPS) of the source footage, e.g. 240 for iPhone slo-mo:",
            initialvalue=round(self.fps, 3),
            minvalue=0.1,
            maxvalue=10000.0,
            parent=self.root,
        )
        if value is None:
            return
        self.fps = float(value)
        self.update_info()
        self.update_mark_status()
        self.refresh_timestamps()

    # -- Frame reading, display, and navigation --------------------------------
    def read_frame(self, frame_num):
        """Seek to and decode one frame from the active proxy."""
        if self.cap is None:
            return None
        frame_num = max(0, min(frame_num, self.frame_count - 1))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = self.cap.read()
        return frame if ret else None

    def _view_geometry(self, frame_w, frame_h, zoom=None):
        """Return ``(scale, crop_w, crop_h)`` for the preview at ``zoom``.

        Zoom 1 fits the whole frame inside the canvas. Zooming in scales the
        picture continuously (square pixels, no stretching). Once the picture
        is bigger than the canvas on an axis, the view crops to the canvas
        shape on that axis, so past the point where both axes overflow the
        view fills the canvas edge to edge.
        """
        zoom = self.zoom if zoom is None else zoom
        available_w = max(self.video_canvas.winfo_width(), 1)
        available_h = max(self.video_canvas.winfo_height(), 1)
        fit = min(available_w / frame_w, available_h / frame_h, PREVIEW_MAX_UPSCALE)
        if zoom <= 1.0:
            return fit, frame_w, frame_h
        scale = fit * zoom
        crop_w = min(frame_w, max(1, round(available_w / scale)))
        crop_h = min(frame_h, max(1, round(available_h / scale)))
        return scale, crop_w, crop_h

    def _max_zoom(self, frame_w, frame_h):
        available_w = max(self.video_canvas.winfo_width(), 1)
        available_h = max(self.video_canvas.winfo_height(), 1)
        fit = min(available_w / frame_w, available_h / frame_h, PREVIEW_MAX_UPSCALE)
        return max(1.0, MAX_PIXEL_SCALE / fit)

    def resize_frame_for_preview(self, frame):
        """Crop ``frame`` to the zoomed view, then scale it for the canvas."""
        h, w = frame.shape[:2]
        scale, crop_w, crop_h = self._view_geometry(w, h)
        self._scale = scale
        if self.zoom > 1.0:
            x0 = min(max(int(round(self.view_x * w)), 0), w - crop_w)
            y0 = min(max(int(round(self.view_y * h)), 0), h - crop_h)
            frame = frame[y0:y0 + crop_h, x0:x0 + crop_w]
            new_w = min(max(1, round(crop_w * scale)), max(self.video_canvas.winfo_width(), 1))
            new_h = min(max(1, round(crop_h * scale)), max(self.video_canvas.winfo_height(), 1))
        else:
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
        # Nearest-neighbor when magnifying keeps individual pixels crisp instead of blurred.
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_NEAREST
        return cv2.resize(frame, (new_w, new_h), interpolation=interpolation)

    def show_frame(self, frame_num):
        if self.cap is None:
            return

        frame_num = max(0, min(frame_num, self.frame_count - 1))
        frame = self.read_frame(frame_num)
        if frame is None:
            return

        self.current_frame = frame_num
        self._frame_bgr = frame
        self._draw_preview()

        # Updating a ttk.Scale programmatically fires its command callback on some
        # Tk builds. Guard this update so changing frames from buttons/hotkeys
        # does not recursively call slider_changed() -> show_frame() -> set().
        self._updating_slider = True
        try:
            self.slider.set(float(self.current_frame))
        finally:
            self._updating_slider = False

        self.update_info()

    def _draw_preview(self):
        """Render the cached frame at the current zoom/pan onto the canvas."""
        if self._frame_bgr is None:
            return
        preview = self.resize_frame_for_preview(self._frame_bgr)
        preview = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(preview)
        self.current_tk_image = ImageTk.PhotoImage(image=img)

        self.video_canvas.delete("all")
        x = self.video_canvas.winfo_width() // 2
        y = self.video_canvas.winfo_height() // 2
        self.video_canvas.create_image(x, y, image=self.current_tk_image, anchor=tk.CENTER)
        height, width = preview.shape[:2]
        self._preview_box = (x - width / 2, y - height / 2, width, height)

        if self.zoom > 1.0:
            label = f"Zoom {self._scale * 100:.0f}%  •  drag to pan, double-click to reset"
            self.video_canvas.create_text(13, 13, text=label, anchor="nw", fill="black", font=("Segoe UI", 10))
            self.video_canvas.create_text(12, 12, text=label, anchor="nw", fill="#f3f3f3", font=("Segoe UI", 10))

    # -- Preview zoom and pan ---------------------------------------------------
    def _clamp_view(self):
        """Keep the zoomed crop inside the frame."""
        if self._frame_bgr is None:
            return
        h, w = self._frame_bgr.shape[:2]
        _, crop_w, crop_h = self._view_geometry(w, h)
        self.view_x = min(max(self.view_x, 0.0), 1.0 - crop_w / w)
        self.view_y = min(max(self.view_y, 0.0), 1.0 - crop_h / h)

    def _schedule_redraw(self):
        """Coalesce rapid wheel/drag events into one redraw per idle cycle."""
        if self._redraw_job is None:
            self._redraw_job = self.root.after_idle(self._run_scheduled_redraw)

    def _run_scheduled_redraw(self):
        self._redraw_job = None
        self._draw_preview()

    def on_video_mousewheel(self, event):
        if self._frame_bgr is None or self._preview_box is None:
            return
        notches = event.delta / 120.0
        self.zoom_at(ZOOM_STEP ** notches, event.x, event.y)

    def zoom_at(self, factor, canvas_x, canvas_y):
        """Zoom by ``factor`` keeping the frame point under the cursor fixed."""
        h, w = self._frame_bgr.shape[:2]
        box_x, box_y, _, _ = self._preview_box
        old = self.zoom
        old_scale, _, _ = self._view_geometry(w, h, old)
        # Source-pixel coordinates under the cursor before zooming.
        point_x = self.view_x * w + (canvas_x - box_x) / old_scale
        point_y = self.view_y * h + (canvas_y - box_y) / old_scale

        new = min(max(old * factor, 1.0), self._max_zoom(w, h))
        if new == old:
            return
        if new == 1.0:
            self.reset_zoom()
            return

        new_scale, crop_w, crop_h = self._view_geometry(w, h, new)
        available_w = max(self.video_canvas.winfo_width(), 1)
        available_h = max(self.video_canvas.winfo_height(), 1)
        # Where the image will start on the canvas (centered while it is smaller than the canvas).
        new_box_x = max(0.0, (available_w - crop_w * new_scale) / 2)
        new_box_y = max(0.0, (available_h - crop_h * new_scale) / 2)
        self.zoom = new
        self.view_x = (point_x - (canvas_x - new_box_x) / new_scale) / w
        self.view_y = (point_y - (canvas_y - new_box_y) / new_scale) / h
        self._clamp_view()
        self._schedule_redraw()

    def reset_zoom(self):
        if self.zoom == 1.0 and self.view_x == 0.0 and self.view_y == 0.0:
            return
        self.zoom, self.view_x, self.view_y = 1.0, 0.0, 0.0
        self._schedule_redraw()

    def on_pan_start(self, event):
        self._pan_anchor = (event.x, event.y)

    def on_pan_drag(self, event):
        if self.zoom <= 1.0 or self._pan_anchor is None or self._frame_bgr is None:
            return
        h, w = self._frame_bgr.shape[:2]
        dx, dy = event.x - self._pan_anchor[0], event.y - self._pan_anchor[1]
        self._pan_anchor = (event.x, event.y)
        self.view_x -= dx / self._scale / w
        self.view_y -= dy / self._scale / h
        self._clamp_view()
        self._schedule_redraw()

    def slider_changed(self, value):
        if self._updating_slider or self.cap is None or self.busy:
            return

        try:
            frame_num = int(round(float(value)))
        except (TypeError, ValueError, tk.TclError):
            return

        # Avoid rereading/redrawing the same frame when the slider is being
        # synchronized to the current frame.
        if frame_num == self.current_frame:
            return

        self.show_frame(frame_num)

    def on_video_resize(self, event):
        if self.cap is None:
            self.video_canvas.coords("empty_text", event.width // 2, event.height // 2)
            return
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(100, lambda: self.show_frame(self.current_frame))

    def jump_frames(self, delta_frames):
        if self.cap is None or self.busy:
            return
        self.show_frame(self.current_frame + delta_frames)

    def jump_seconds(self, delta_seconds):
        if self.cap is None or self.busy:
            return
        self.jump_frames(self.seconds_to_frames(delta_seconds))

    def jump_to_frame_from_entry(self, event=None):
        if self.cap is None or self.busy:
            return
        try:
            frame_num = int(self.current_frame_var.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid Frame", "Enter a whole frame number.")
            self.current_frame_var.set(str(self.current_frame))
            return
        self.show_frame(frame_num)
        self.root.focus_force()

    def set_start(self):
        if self.cap is None or self.busy:
            return
        self.start_frame = self.current_frame
        self.update_mark_status()

    def set_stop(self):
        if self.cap is None or self.busy:
            return
        self.stop_frame = self.current_frame
        self.update_mark_status()

    # -- Single-frame copy and save actions ------------------------------------
    def get_current_frame_pil_image(self):
        if self.cap is None:
            return None
        frame = self.read_frame(self.current_frame)
        if frame is None:
            return None
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb_frame)

    @staticmethod
    def copy_pil_image_to_windows_clipboard(pil_image):
        if os.name != "nt":
            raise RuntimeError("Image clipboard copy is currently implemented for Windows only.")

        with io.BytesIO() as output:
            pil_image.convert("RGB").save(output, "BMP")
            dib_data = output.getvalue()[14:]

        CF_DIB = 8
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        kernel32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
        user32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
        user32.SetClipboardData.restype = ctypes.wintypes.HANDLE

        h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib_data))
        if not h_global:
            raise RuntimeError("GlobalAlloc failed while copying image to clipboard.")

        locked_ptr = kernel32.GlobalLock(h_global)
        if not locked_ptr:
            raise RuntimeError("GlobalLock failed while copying image to clipboard.")

        ctypes.memmove(locked_ptr, dib_data, len(dib_data))
        kernel32.GlobalUnlock(h_global)

        if not user32.OpenClipboard(None):
            raise RuntimeError("Could not open Windows clipboard.")

        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_DIB, h_global):
                raise RuntimeError("SetClipboardData failed while copying image to clipboard.")
            h_global = None
        finally:
            user32.CloseClipboard()

    def copy_current_frame_image(self):
        if self.cap is None:
            messagebox.showwarning("No Video", "Load a video first.")
            return
        pil_image = self.get_current_frame_pil_image()
        if pil_image is None:
            messagebox.showerror("Copy Frame", "Could not read the current frame.")
            return
        try:
            self.copy_pil_image_to_windows_clipboard(pil_image)
            self.set_progress(f"Copied frame {self.current_frame} image to clipboard.", None)
        except Exception as e:
            messagebox.showerror("Copy Frame", f"Could not copy frame image:\n{e}")

    def flash_video_border(self, color="#00ff00", thickness=4, duration_ms=150):
        # Draw the confirmation border directly on the canvas, then remove it.
        w = self.video_canvas.winfo_width()
        h = self.video_canvas.winfo_height()
        rect = self.video_canvas.create_rectangle(2, 2, w - 2, h - 2, outline=color, width=thickness)
        self.root.after(duration_ms, lambda: self.video_canvas.delete(rect))

    def update_image_subfolder_state(self):
        enabled = self.image_to_frames_subfolder_var.get() and not self.busy
        self.image_subfolder_entry.state(["!disabled" if enabled else "disabled"])
        # Sun Valley's tk_setPalette can set a widget foreground that overrides
        # the style map. Update that option as well, including after theme changes.
        foreground = ttk.Style(self.root).lookup("TEntry", "foreground", ("!disabled",))
        self.image_subfolder_entry.configure(foreground=foreground if enabled else "#808080")
        if not enabled:
            self.image_subfolder_entry.selection_clear()

    def get_image_output_settings(self):
        base_name = self.image_basename_var.get().strip() or self.name or "video"
        output_dir = self.folder
        if self.image_to_frames_subfolder_var.get():
            subfolder = self.image_subfolder_var.get().strip()
            reserved = {"CON", "PRN", "AUX", "NUL"}
            reserved.update(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³")
            if (
                not subfolder or subfolder.endswith((".", " "))
                or any(ch in '<>:"/\\|?*' or ord(ch) < 32 for ch in subfolder)
                or subfolder.split(".")[0].upper() in reserved
            ):
                messagebox.showerror(
                    "Invalid Subfolder", "Enter a valid single subfolder name, such as Frames."
                )
                return None
            output_dir = os.path.join(output_dir, subfolder)
        return base_name, output_dir

    def get_frame_filename(self, base_name, frame_num):
        safe_name = self.sanitize_filename_part(base_name or "video")
        milliseconds = int(round(self.frame_to_seconds(frame_num) * 1000))
        output_name = f"{safe_name}_frame{frame_num:06d}_{milliseconds:08d}ms.bmp"
        return output_name

    def save_current_frame_bitmap(self):
        """
        Save the currently displayed frame as a BMP image.

        The "Save to subfolder" checkbox controls whether the image
        is saved beside the source video or in a Frames subfolder.

        The "Monochrome" checkbox controls whether the image is saved as
        grayscale or full color.
        """
        if self.cap is None:
            messagebox.showwarning("No Video", "Load a video first.")
            return

        pil_image = self.get_current_frame_pil_image()
        if pil_image is None:
            messagebox.showerror("Save Frame", "Could not read the current frame.")
            return

        settings = self.get_image_output_settings()
        if settings is None:
            return
        base_name, output_dir = settings
        output_name = self.get_frame_filename(base_name, self.current_frame)

        try:
            os.makedirs(output_dir, exist_ok=True)
            output_path = self.get_unique_path(os.path.join(output_dir, output_name))
            # Select monochrome or color using the checkbox.
            if self.image_monochrome_var.get():
                pil_image.convert("L").save(output_path, "BMP")
            else:
                pil_image.convert("RGB").save(output_path, "BMP")

            self.set_progress(f"Saved frame bitmap: {output_path}", None)
            self.flash_video_border()

        except Exception as e:
            messagebox.showerror("Save Frame", f"Could not save frame bitmap:\n{e}")

    def show_frame_context_menu(self, event):
        if self.cap is None or self.busy:
            return
        self.frame_context_menu.tk_popup(event.x_root, event.y_root)

    # -- Multi-frame image export ----------------------------------------------
    def populate_image_range_from_marks(self):
        if self.start_frame is None or self.stop_frame is None:
            messagebox.showerror("Missing Points", "Set both START and STOP frames first.")
            return
        self.image_start_var.set(str(self.start_frame))
        self.image_stop_var.set(str(self.stop_frame))

    def get_image_export_range(self):
        if self.cap is None:
            messagebox.showerror("No Video", "Load a video first.")
            return None

        try:
            local_start_frame = int(self.image_start_var.get().strip())
            local_stop_frame = int(self.image_stop_var.get().strip())
            local_step = int(self.image_step_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Frame Range", "Start, Stop, and Step must be whole numbers.")
            return None

        if local_step <= 0:
            messagebox.showerror("Invalid Step", "Step must be 1 or greater.")
            return None
        if local_start_frame < 0 or local_stop_frame < 0:
            messagebox.showerror("Invalid Frame Range", "Start and Stop frames cannot be negative.")
            return None
        if local_start_frame >= self.frame_count or local_stop_frame >= self.frame_count:
            messagebox.showerror("Invalid Frame Range", f"Start and Stop must be between 0 and {self.frame_count - 1}.")
            return None
        if local_stop_frame < local_start_frame:
            messagebox.showerror("Invalid Frame Range", "Stop frame must be greater than or equal to Start frame.")
            return None
        return local_start_frame, local_stop_frame, local_step

    def export_frame_images(self):
        if self.busy:
            messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
            return
        if self.cap is None:
            messagebox.showerror("No Video", "Load a video first.")
            return

        image_range = self.get_image_export_range()
        if image_range is None:
            return
        settings = self.get_image_output_settings()
        if settings is None:
            return
        base_name, output_dir = settings

        local_start_frame, local_stop_frame, local_step = image_range
        total = len(range(local_start_frame, local_stop_frame + 1, local_step))
        if total <= 0:
            messagebox.showerror("Invalid Frame Range", "No frames are included in this export range.")
            return

        self.set_busy(True, "Saving frame images...")
        self.set_progress("Saving frame images...", 0)
        threading.Thread(
            target=self._export_frame_images,
            args=(
                self.proxy_path,
                local_start_frame,
                local_stop_frame,
                local_step,
                output_dir,
                base_name,
                self.image_monochrome_var.get(),
            ),
            daemon=True,
        ).start()

    def _export_frame_images(
        self,
        local_proxy_path,
        local_start_frame,
        local_stop_frame,
        local_step,
        local_folder,
        local_name,
        export_monochrome,
    ):
        """Save the selected proxy frames and report progress to the UI thread."""
        export_cap = open_video_capture(local_proxy_path)
        if not export_cap.isOpened():
            self.ui_queue.put(("error", "Image Export Error", "Could not open proxy video for image export."))
            return

        frame_numbers = list(range(local_start_frame, local_stop_frame + 1, local_step))
        total = len(frame_numbers)
        saved_count = 0
        frames_folder = local_folder

        try:
            os.makedirs(frames_folder, exist_ok=True)
            for idx, frame_num in enumerate(frame_numbers, start=1):
                export_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                ret, frame = export_cap.read()
                if not ret:
                    raise RuntimeError(f"Could not read frame {frame_num}.")

                output_name = self.get_frame_filename(local_name, frame_num)
                output_path = self.get_unique_path(os.path.join(frames_folder, output_name))

                if export_monochrome:
                    gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    Image.fromarray(gray_frame).save(output_path, "BMP")
                else:
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    Image.fromarray(rgb_frame).save(output_path, "BMP")

                saved_count += 1
                percent = (idx / total) * 100 if total else 100
                self.ui_queue.put(("progress", f"Saving frame images... {idx}/{total} ({percent:0.1f}%)", percent))

        except Exception as e:
            self.ui_queue.put(("error", "Image Export Error", f"Failed during image export:\n{e}"))
            return
        finally:
            export_cap.release()

        self.ui_queue.put(("image_export_done", frames_folder, saved_count))

    # -- Clip export ------------------------------------------------------------
    def export_clip(self):
        if self.busy:
            messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
            return
        if self.cap is None:
            messagebox.showerror("No Video", "Load a video first.")
            return
        if self.start_frame is None or self.stop_frame is None:
            messagebox.showerror("Missing Points", "Set both START and STOP frames.")
            return
        if self.stop_frame <= self.start_frame:
            messagebox.showerror("Invalid Range", "STOP frame must be after START frame.")
            return

        output_speed = self.get_output_speed()
        if output_speed is None:
            return

        output_path = self.get_output_path()
        if os.path.exists(output_path):
            overwrite = messagebox.askyesno("Overwrite File", f"This file already exists:\n{output_path}\n\nOverwrite it?")
            if not overwrite:
                return

        self.set_busy(True, "Saving video...")
        self.set_progress("Saving video...", 0)
        threading.Thread(
            target=self._export_clip,
            args=(output_path, output_speed, self.proxy_path, self.fps, self.start_frame, self.stop_frame),
            daemon=True,
        ).start()

    def _export_clip(self, output_path, output_speed, local_proxy_path, local_fps, local_start_frame, local_stop_frame):
        """Render a silent clip at the requested playback speed."""
        source_duration = (local_stop_frame - local_start_frame + 1) / local_fps
        output_duration = source_duration / output_speed
        output_frame_count = max(1, int(round(output_duration * OUTPUT_FPS)))

        export_cap = open_video_capture(local_proxy_path)
        if not export_cap.isOpened():
            self.ui_queue.put(("error", "Export Error", "Could not open proxy video for export."))
            return

        width = int(export_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(export_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(output_path, fourcc, OUTPUT_FPS, (width, height))

        if not writer.isOpened():
            export_cap.release()
            self.ui_queue.put(("error", "Export Error", "Could not open output video writer."))
            return

        export_cap.set(cv2.CAP_PROP_POS_FRAMES, local_start_frame)
        source_idx = local_start_frame
        ret, current_source_frame = export_cap.read()

        if not ret:
            writer.release()
            export_cap.release()
            self.ui_queue.put(("error", "Export Error", "Could not read first source frame."))
            return

        try:
            last_percent_int = -1
            for out_idx in range(output_frame_count):
                output_time = out_idx / OUTPUT_FPS
                desired_source_frame = local_start_frame + int(round(output_time * output_speed * local_fps))
                desired_source_frame = max(local_start_frame, min(desired_source_frame, local_stop_frame))

                while source_idx < desired_source_frame:
                    ret, current_source_frame = export_cap.read()
                    if not ret:
                        break
                    source_idx += 1

                writer.write(current_source_frame)
                percent = ((out_idx + 1) / output_frame_count) * 100
                percent_int = int(percent)
                if percent_int != last_percent_int:
                    last_percent_int = percent_int
                    self.ui_queue.put(("progress", f"Saving video... {percent:0.1f}%", percent))

        except Exception as e:
            self.ui_queue.put(("error", "Export Error", f"Failed during export:\n{e}"))
            return
        finally:
            writer.release()
            export_cap.release()

        self.ui_queue.put(("export_done", output_path))

    # -- Shutdown ---------------------------------------------------------------
    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("Operation Running", "An operation is still running. Close anyway?"):
                return
        self.clear_current_video(delete_proxy=self.delete_proxy_on_close_var.get())
        self.root.destroy()


def main():
    """Launch the FrameLab desktop application."""
    root = tk.Tk()
    FrameLabApplication(root)
    root.mainloop()


if __name__ == "__main__":
    main()
