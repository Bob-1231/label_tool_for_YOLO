"""Traditional Chinese desktop interface for YOLO datasets."""
from __future__ import annotations

import copy
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageOps, UnidentifiedImageError

from .canvas import AnnotationCanvas
from .dataset import Annotation, Dataset, MAX_CLASS_ID, atomic_write

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "settings.json"
BG, PANEL, CARD = "#dfe7f1", "#ffffff", "#edf2f8"
TEXT, MUTED, ACCENT = "#17263b", "#52647a", "#245ee8"
HEADER, BORDER = "#172b46", "#afbed0"
SAVE_BG, SAVE_BORDER = "#eaf6ef", "#94c7b3"
EMPTY_LABEL_FILTER = "空標註（無物件）"
LIST_COLORS = ["#08785d", "#245dc1", "#9a5a06", "#823bb0", "#b82950", "#08758e", "#616d0b", "#7d583a"]


class YoloDesk(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YOLO Desk · 本機標註工作台")
        self.settings = self._read_settings()
        try:
            self.geometry(self.settings.get("window_geometry", "1440x900"))
        except tk.TclError:
            self.geometry("1440x900")
        self.minsize(1180, 800)
        self.configure(background=BG)
        self.dataset: Dataset | None = None
        self.current_index = -1
        self.visible_indices: list[int] = []
        self.saved_annotations: list[Annotation] = []
        self.label_hash: str | None = None
        self.history: list[list[Annotation]] = []
        self.future: list[list[Annotation]] = []
        self.names_dirty = False
        self.busy = False
        self._messages = queue.Queue()
        self._search_timer = None
        self._refreshing_objects = False
        self._hover_sync = None
        self._class_ids: list[int] = []
        self._style()
        self._build()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(80, self._poll_jobs)

    def _read_settings(self) -> dict:
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
            return settings if isinstance(settings, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        self.settings.update(window_geometry=self.geometry(), auto_save_on_navigation=self.autosave.get())
        if self.dataset:
            self.settings["last_dataset"] = str(self.dataset.source)
            self.settings["last_labels_dir"] = str(self.dataset.labels_dir) if self.dataset.labels_dir else ""
        atomic_write(SETTINGS_PATH, (json.dumps(self.settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def _style(self):
        self.option_add("*Font", ("Microsoft JhengHei UI", 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        def button(name, background, foreground, border, hover, pressed, bold=False, compact=False):
            style.configure(name, background=background, foreground=foreground,
                            bordercolor=border, lightcolor=border, darkcolor=border,
                            borderwidth=1, relief="solid", padding=(10, 7) if compact else (12, 8),
                            focusthickness=1, focuscolor=foreground,
                            font=("Microsoft JhengHei UI", 10, "bold" if bold else "normal"))
            style.map(name,
                      background=[("disabled", "#e5eaf0"), ("pressed", pressed), ("active", hover)],
                      foreground=[("disabled", "#8793a3")],
                      bordercolor=[("disabled", "#c7d1dc"), ("focus", ACCENT)],
                      lightcolor=[("disabled", "#c7d1dc")], darkcolor=[("disabled", "#c7d1dc")])

        button("TButton", CARD, TEXT, "#95a8c0", "#dce7f5", "#c6d7ee")
        button("Accent.TButton", ACCENT, "#ffffff", "#194bc2", "#194fc9", "#143ca2", bold=True)
        button("Save.TButton", "#13835c", "#ffffff", "#086747", "#0b704c", "#07583c", bold=True)
        button("Class.TButton", "#7043c1", "#ffffff", "#59319e", "#6036ab", "#4b278c", bold=True)
        button("Danger.TButton", "#ffe9ed", "#a62440", "#d9768b", "#ffd3dc", "#f7b8c6")
        button("Warning.TButton", "#fff1d3", "#805300", "#cba353", "#ffe4ac", "#f8d18b")
        button("Info.TButton", "#e8f0ff", "#204db1", "#89a7df", "#d5e4ff", "#bfd5fb")
        button("Tool.TButton", "#ffffff", TEXT, "#95a8c0", "#e0eafa", "#c6d7ee", compact=True)
        button("Active.Tool.TButton", "#f7bd42", "#382708", "#b88012", "#ffd16e", "#e8a82b", bold=True, compact=True)
        style.configure("Compact.TButton", padding=(9, 4))
        style.configure("Class.TButton", padding=(10, 5))
        style.configure("Danger.TButton", padding=(10, 5))
        style.configure("Warning.TButton", padding=(10, 5))
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, bordercolor="#8da2bc", insertcolor=TEXT, padding=7)
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=PANEL, background=CARD, foreground=TEXT,
                        arrowcolor=TEXT, bordercolor="#8da2bc", lightcolor="#8da2bc", darkcolor="#8da2bc", padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)], bordercolor=[("focus", ACCENT)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=32,
                        borderwidth=1, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Treeview.Heading", background="#e2eaf5", foreground="#2a405c", padding=6,
                        relief="flat", font=("Microsoft JhengHei UI", 10, "bold"))
        style.map("Treeview.Heading", background=[("active", "#ccdbee")])
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        self._object_font = tkfont.Font(self, family="Microsoft JhengHei UI", size=14)
        row_height = max(36, self._object_font.metrics("linespace") + 8)
        style.configure("Objects.Treeview", font=self._object_font, rowheight=row_height)
        self._grid_widths = ()
        self._grid_sync = None
        self._row_grid = tk.PhotoImage(width=256, height=row_height)
        style.element_create("ObjectRow.rules", "image", self._row_grid, width=1, height=1)
        style.layout("Objects.Treeview.Row", style.layout("Treeview.Row") + [
            ("ObjectRow.rules", {"sticky": "nsew"})
        ])
        self._heading_rule = tk.PhotoImage(width=64, height=64)
        self._heading_rule.put(BORDER, to=(0, 0, 64, 64))
        style.element_create("ObjectHeading.rule", "image", self._heading_rule, width=1, height=1)
        style.layout("Objects.Treeview.Heading", [
            ("Treeheading.cell", {"sticky": "nsew"}),
            ("ObjectHeading.rule", {"side": "bottom", "sticky": "ew"}),
            ("ObjectHeading.rule", {"side": "right", "sticky": "ns"}),
            ("Treeheading.padding", {"sticky": "nsew", "children": [("Treeheading.text", {"sticky": "we"})]})
        ])
        style.configure("Objects.Treeview.Heading", padding=(2, 5))
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT, padding=4, indicatorbackground=PANEL)
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("Save.TCheckbutton", background=SAVE_BG, foreground="#244f3e")
        style.map("Save.TCheckbutton", background=[("active", SAVE_BG)])
        style.configure("TSeparator", background=BORDER)
        style.configure("Horizontal.TProgressbar", background="#50d6ad", troughcolor="#314967", borderwidth=0)
        style.configure("Vertical.TScrollbar", background="#9caec4", troughcolor="#e6edf5", arrowcolor="#253d5a", borderwidth=0)
        style.map("Vertical.TScrollbar", background=[("active", "#7f97b5"), ("pressed", "#647f9f")])

    def _build(self):
        header = tk.Frame(self, background=HEADER, padx=20, pady=15)
        header.pack(fill="x")
        tk.Label(header, text="YOLO", background=HEADER, foreground="#6ce2c0", font=("Segoe UI", 22, "bold")).pack(side="left")
        tk.Label(header, text=" Desk", background=HEADER, foreground="#ffffff", font=("Segoe UI", 22)).pack(side="left")
        tk.Label(header, text="  /  本機標註工作台", background=HEADER, foreground="#c7d6e9", font=("Microsoft JhengHei UI", 10)).pack(side="left", padx=(6, 20))
        ttk.Button(header, text="匯入資料夾", style="Accent.TButton", command=self.choose_folder).pack(side="left", padx=4)
        ttk.Button(header, text="開啟 YAML", command=self.choose_yaml).pack(side="left", padx=4)
        ttk.Button(header, text="自訂配對", command=self.custom_import).pack(side="left", padx=4)
        ttk.Button(header, text="?  操作說明", command=self.show_help).pack(side="right")
        ttk.Button(header, text="匯出副本", command=self.export_dataset).pack(side="right", padx=8)

        project = tk.Frame(self, background=BG, padx=20, pady=10)
        project.pack(fill="x")
        self.project_text = tk.StringVar(value="還沒有資料集  ·  開啟資料夾即可載入既有 YOLO 標註")
        tk.Label(project, textvariable=self.project_text, background=BG, foreground=MUTED, anchor="w").pack(side="left", fill="x", expand=True)
        ttk.Button(project, text="上次的資料集", command=self.open_recent, style="Tool.TButton").pack(side="right")

        body = tk.PanedWindow(self, orient="horizontal", background=BG, sashwidth=10, borderwidth=0, sashrelief="flat")
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        left = tk.Frame(body, background=PANEL, width=260, highlightbackground=BORDER, highlightthickness=1)
        center = tk.Frame(body, background="#c8d5e6", highlightbackground="#8da2bd", highlightthickness=1)
        right = tk.Frame(body, background=PANEL, width=280, highlightbackground=BORDER, highlightthickness=1)
        body.add(left, minsize=210, width=260)
        body.add(center, minsize=400, stretch="always")
        body.add(right, minsize=250, width=280)

        self._heading(left, "資料集圖片", "DATASET")
        self.summary_text = tk.StringVar(value="0 張圖片")
        tk.Label(left, textvariable=self.summary_text, background=PANEL, foreground=MUTED, anchor="w").pack(fill="x", padx=14, pady=(0, 10))
        self.search = tk.StringVar()
        search = ttk.Entry(left, textvariable=self.search)
        search.pack(fill="x", padx=12)
        self.search.trace_add("write", self._search_changed)
        tk.Label(left, text="依檔名搜尋", background=PANEL, foreground=MUTED, anchor="w", font=("Microsoft JhengHei UI", 9)).pack(fill="x", padx=15, pady=(3, 8))
        filters = tk.Frame(left, background=PANEL)
        filters.pack(fill="x", padx=12, pady=(0, 10))
        self.split_filter = tk.StringVar(value="所有分組")
        self.split_combo = ttk.Combobox(filters, textvariable=self.split_filter, values=["所有分組"], state="readonly", width=9)
        self.split_combo.pack(fill="x", pady=(0, 4))
        self.split_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_images())
        self.state_filter = tk.StringVar(value="所有狀態")
        state = ttk.Combobox(filters, textvariable=self.state_filter,
                             values=["所有狀態", "有標註檔", EMPTY_LABEL_FILTER, "無標註檔", "格式錯誤"], state="readonly")
        state.pack(fill="x")
        state.bind("<<ComboboxSelected>>", lambda _e: self.refresh_images())
        listing = tk.Frame(left, background=PANEL)
        listing.pack(fill="both", expand=True, padx=8)
        self.image_tree = ttk.Treeview(listing, columns=("state",), show="tree headings", selectmode="browse")
        self.image_tree.heading("#0", text="圖片")
        self.image_tree.heading("state", text="標註")
        self.image_tree.column("#0", width=165, minwidth=90, stretch=True)
        self.image_tree.column("state", width=58, minwidth=50, stretch=False, anchor="center")
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.image_tree.yview)
        self.image_tree.configure(yscrollcommand=scrollbar.set)
        self.image_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.image_tree.bind("<<TreeviewSelect>>", self._image_selected)
        self.image_tree.tag_configure("row-even", background="#ffffff")
        self.image_tree.tag_configure("row-odd", background="#f0f5fc")
        self.image_tree.tag_configure("error", foreground="#b12343")
        self.image_tree.tag_configure("missing", foreground=MUTED)
        ttk.Button(left, text="檢查全部標註", style="Info.TButton", command=self.validate_dataset).pack(side="bottom", before=listing, fill="x", padx=12, pady=12)

        toolbar = tk.Frame(center, background="#eaf0f8", padx=8, pady=8)
        toolbar.pack(fill="x")
        self.select_button = ttk.Button(toolbar, text="● 選取  V", style="Active.Tool.TButton", command=lambda: self.set_tool("select"))
        self.select_button.pack(side="left", padx=3)
        self.box_button = ttk.Button(toolbar, text="矩形框  B", style="Tool.TButton", command=lambda: self.set_tool("box"))
        self.box_button.pack(side="left", padx=3)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=9, pady=3)
        ttk.Button(toolbar, text="復原", style="Tool.TButton", command=self.undo).pack(side="left", padx=3)
        ttk.Button(toolbar, text="重做", style="Tool.TButton", command=self.redo).pack(side="left", padx=3)
        ttk.Button(toolbar, text="適合畫面  F", style="Info.TButton", command=lambda: self.canvas.fit()).pack(side="right", padx=3)
        self.canvas = AnnotationCanvas(center, self._changed, self._selected, self.set_status, self._tool_changed)
        self.canvas.pack(fill="both", expand=True, padx=1, pady=1)
        navigation = tk.Frame(center, background="#eaf0f8", padx=10, pady=10)
        navigation.pack(side="bottom", before=self.canvas, fill="x")
        ttk.Button(navigation, text="← 上一張  A", style="Info.TButton", command=lambda: self.navigate(-1)).pack(side="left")
        self.position_text = tk.StringVar(value="— / —")
        tk.Label(navigation, textvariable=self.position_text, background="#eaf0f8", foreground=TEXT, font=("Segoe UI", 11, "bold")).pack(side="left", expand=True)
        ttk.Button(navigation, text="下一張  D →", style="Info.TButton", command=lambda: self.navigate(1)).pack(side="right")

        self._heading(right, "標註類別", "CLASSES", color="#7043c1", background="#f0e9fc")
        self.class_combo = ttk.Combobox(right, state="readonly")
        self.class_combo.pack(fill="x", padx=14, pady=(0, 6))
        self.class_combo.bind("<<ComboboxSelected>>", self._class_selected)
        class_buttons = tk.Frame(right, background=PANEL)
        class_buttons.pack(fill="x", padx=14)
        ttk.Button(class_buttons, text="＋ 新增", style="Compact.TButton", command=self.add_class).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(class_buttons, text="重新命名", style="Compact.TButton", command=self.rename_class).pack(side="left", fill="x", expand=True)
        ttk.Button(right, text="套用類別到選取框", style="Class.TButton", command=self.apply_class).pack(fill="x", padx=14, pady=(6, 8))
        self._heading(right, "本張標註", "OBJECTS", color="#147a69", background="#e2f3ee")
        object_frame = tk.Frame(right, background=PANEL)
        object_frame.pack(fill="both", expand=True, padx=10)
        self.object_tree = ttk.Treeview(object_frame, columns=("id", "name", "locked", "hidden"),
                                        show="headings", selectmode="browse", style="Objects.Treeview", height=6)
        self.object_tree.heading("id", text="#")
        self.object_tree.heading("name", text="類別")
        self.object_tree.column("id", width=28, minwidth=24, stretch=False, anchor="center")
        self.object_tree.column("name", width=110, minwidth=60, stretch=True)
        for column, title in (("locked", "固定"), ("hidden", "隱藏")):
            self.object_tree.heading(column, text=title)
            self.object_tree.column(column, width=46, minwidth=46, stretch=False, anchor="center")
        object_scroll = ttk.Scrollbar(object_frame, orient="vertical", command=self.object_tree.yview)
        def object_scrolled(first, last):
            object_scroll.set(first, last)
            self._queue_object_hover()

        self.object_tree.configure(yscrollcommand=object_scrolled)
        self.object_tree.pack(side="left", fill="both", expand=True)
        object_scroll.pack(side="right", fill="y")
        self.object_tree.bind("<<TreeviewSelect>>", self._object_selected)
        self.object_tree.bind("<Button-1>", self._object_control_clicked)
        self.object_tree.bind("<Motion>", lambda e: self._hover_at(e.x, e.y))
        self.object_tree.bind("<Leave>", lambda _e: self._set_object_hover(-1))
        self.object_tree.bind("<Configure>", self._object_table_resized)
        self.object_tree.bind("<B1-Motion>", self._queue_object_grid, add="+")
        self.object_tree.bind("<ButtonRelease-1>", self._queue_object_grid, add="+")
        self.object_tree.tag_configure("hover", background="#c6efe4")
        self.object_tree.tag_configure("row-even", background="#ffffff")
        self.object_tree.tag_configure("row-odd", background="#f0f5fc")
        object_actions = tk.Frame(right, background=PANEL)
        object_actions.pack(side="bottom", before=object_frame, fill="x")
        ttk.Button(object_actions, text="刪除選取框  Del", style="Danger.TButton", command=self.delete_selected).pack(fill="x", padx=14, pady=6)
        self.object_info = tk.StringVar(value="選取框後可拖曳位置與角點")
        tk.Label(object_actions, textvariable=self.object_info, background="#f0f4f9", foreground=MUTED,
                 wraplength=230, justify="left", anchor="w", height=2, padx=8, pady=2,
                 font=("Microsoft JhengHei UI", 9),
                 highlightbackground="#d1dce9", highlightthickness=1).pack(fill="x", padx=14, pady=(0, 6))
        save_panel = tk.Frame(object_actions, background=SAVE_BG, highlightbackground=SAVE_BORDER, highlightthickness=1)
        save_panel.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(save_panel, text="儲存與確認", background=SAVE_BG, foreground="#24614a",
                 font=("Microsoft JhengHei UI", 10, "bold"), anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        save_options = tk.Frame(save_panel, background=SAVE_BG)
        save_options.pack(fill="x", padx=4)
        self.autosave = tk.BooleanVar(value=self.settings.get("auto_save_on_navigation", True))
        ttk.Checkbutton(save_options, text="切圖自動存檔", style="Save.TCheckbutton", variable=self.autosave).pack(side="left")
        self.labels_visible = tk.BooleanVar(value=True)
        ttk.Checkbutton(save_options, text="顯示名稱", style="Save.TCheckbutton", variable=self.labels_visible, command=self._toggle_labels).pack(side="left")
        self.save_button = ttk.Button(save_panel, text="儲存標註  Ctrl+S", style="Save.TButton", command=self.save)
        self.save_button.pack(fill="x", padx=10, pady=(4, 6))
        ttk.Button(save_panel, text="標記為無物件", style="Warning.TButton", command=self.mark_negative).pack(fill="x", padx=10, pady=(0, 6))
        self.save_state = tk.StringVar(value="儲存為 YOLO .txt")
        self.save_state_label = tk.Label(save_panel, textvariable=self.save_state, background=SAVE_BG,
                                        foreground="#24614a", height=1, justify="left", font=("Microsoft JhengHei UI", 9))
        self.save_state_label.pack(fill="x", padx=10, pady=(0, 6))

        pathbar = tk.Frame(self, background=PANEL, padx=14, pady=6, highlightbackground=BORDER, highlightthickness=1)
        pathbar.pack(fill="x")
        self.path_text = tk.StringVar(value="圖片與標註配對路徑會顯示在這裡")
        tk.Label(pathbar, textvariable=self.path_text, background=PANEL, foreground=MUTED, anchor="w", justify="left", font=("Microsoft JhengHei UI", 9)).pack(side="left", fill="x", expand=True)
        ttk.Button(pathbar, text="所在資料夾", style="Tool.TButton", command=self.open_location).pack(side="right")
        ttk.Button(pathbar, text="重新載入", style="Tool.TButton", command=self.reload_image).pack(side="right", padx=5)
        footer = tk.Frame(self, background=HEADER, padx=18, pady=8)
        footer.pack(fill="x", pady=(8, 0))
        self.status_text = tk.StringVar(value="就緒  ·  滾輪縮放 / 右鍵拖曳平移 / B 畫框 / V 選取 / A、D 切圖")
        tk.Label(footer, textvariable=self.status_text, background=HEADER, foreground="#e0eaf7", anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=130)
        # Reserve the footer/path controls before assigning the expanding body.
        footer.pack_configure(side="bottom", before=body)
        pathbar.pack_configure(side="bottom", before=body)

    def _heading(self, parent, title, subtitle, color=ACCENT, background="#e5efff"):
        frame = tk.Frame(parent, background=background)
        frame.pack(fill="x", pady=(0, 8))
        tk.Frame(frame, background=color, width=4).pack(side="left", fill="y")
        tk.Label(frame, text=title, background=background, foreground=TEXT,
                 font=("Microsoft JhengHei UI", 11, "bold")).pack(side="left", padx=10, pady=6)
        tk.Label(frame, text=subtitle, background=background, foreground=color,
                 font=("Segoe UI", 8, "bold")).pack(side="right", padx=12)

    def _bind_keys(self):
        self.bind("<Control-s>", lambda e: self._shortcut(e, self.save))
        self.bind("<Control-o>", lambda e: self._shortcut(e, self.choose_folder))
        self.bind("<Control-z>", lambda e: self._shortcut(e, self.undo))
        self.bind("<Control-y>", lambda e: self._shortcut(e, self.redo))
        self.bind("<Control-Shift-Z>", lambda e: self._shortcut(e, self.redo))
        for key, action in {"a": lambda: self.navigate(-1), "d": lambda: self.navigate(1), "Left": lambda: self.navigate(-1), "Right": lambda: self.navigate(1), "b": lambda: self.set_tool("box"), "v": lambda: self.set_tool("select"), "f": lambda: self.canvas.fit(), "Delete": self.delete_selected, "Escape": self.canvas.cancel, "Return": self.canvas.finish_polygon, "h": self._toggle_annotations}.items():
            self.bind(f"<KeyPress-{key}>", lambda e, fn=action: self._shortcut(e, fn))
        for number in range(1, 10):
            self.bind(str(number), lambda e, n=number - 1: self._shortcut(e, lambda: self.choose_class_index(n)))

    def _shortcut(self, event, action):
        if self.busy or self.grab_current() is not None:
            return
        if event.widget.winfo_class() in {"Entry", "TEntry", "Text", "TCombobox", "Spinbox"}:
            if event.keysym.lower() not in {"s", "o"} or not event.state & 4:
                return
        action()
        return "break"

    def set_status(self, text):
        self.status_text.set(text)

    def _run_job(self, title, work, done):
        if self.busy:
            return
        self.busy = True
        self.canvas.readonly = True
        self.progress.pack(side="right")
        self.progress.start(12)
        self.set_status(title)
        def execute():
            try:
                self._messages.put(("done", work(), done))
            except Exception as exc:
                self._messages.put(("error", str(exc), None))
        threading.Thread(target=execute, daemon=True).start()

    def _poll_jobs(self):
        try:
            while True:
                kind, payload, callback = self._messages.get_nowait()
                if kind == "progress":
                    self.set_status(payload)
                    continue
                self.busy = False
                self.progress.stop()
                self.progress.pack_forget()
                self.canvas.readonly = bool(self.dataset and self.current_index >= 0 and self.dataset.records[self.current_index].error)
                if kind == "error":
                    self.set_status("作業未完成，請查看錯誤訊息")
                    messagebox.showerror("無法完成作業", payload, parent=self)
                else:
                    callback(payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_jobs)

    def choose_folder(self):
        if not self.busy:
            path = filedialog.askdirectory(title="選擇 YOLO 資料集根目錄（含 images / labels）", parent=self)
            if path:
                self.open_dataset(Path(path))

    def choose_yaml(self):
        if not self.busy:
            path = filedialog.askopenfilename(title="開啟 YOLO data.yaml", filetypes=[("YOLO YAML", "*.yaml *.yml")], parent=self)
            if path:
                self.open_dataset(Path(path))

    def custom_import(self):
        if self.busy:
            return
        dialog = tk.Toplevel(self)
        dialog.title("指定圖片 / 標註資料夾")
        dialog.configure(background=PANEL, padx=22, pady=20)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()
        values = [tk.StringVar(), tk.StringVar()]
        tk.Label(dialog, text="依相同相對路徑配對，例如 images/train/a.jpg → labels/train/a.txt", background=PANEL, foreground=MUTED).grid(row=0, column=0, columnspan=3, pady=(0, 16), sticky="w")
        def browse(variable):
            chosen = filedialog.askdirectory(parent=dialog)
            if chosen:
                variable.set(chosen)
        for row, title in enumerate(["圖片資料夾", "標註資料夾"], 1):
            ttk.Label(dialog, text=title, style="Panel.TLabel").grid(row=row, column=0, padx=(0, 12), pady=6)
            ttk.Entry(dialog, textvariable=values[row - 1], width=54).grid(row=row, column=1, pady=6)
            ttk.Button(dialog, text="瀏覽", command=lambda v=values[row - 1]: browse(v)).grid(row=row, column=2, padx=(8, 0))
        def submit():
            if not all(Path(v.get()).is_dir() and v.get().strip() for v in values):
                messagebox.showerror("路徑無效", "請選擇存在的圖片與標註資料夾", parent=dialog)
                return
            paths = [Path(v.get()) for v in values]
            dialog.destroy()
            self.open_dataset(paths[0], paths[1])
        ttk.Button(dialog, text="配對並匯入", style="Accent.TButton", command=submit).grid(row=3, column=1, sticky="e", pady=(18, 0))

    def open_recent(self):
        path = self.settings.get("last_dataset")
        if path:
            labels = self.settings.get("last_labels_dir")
            self.open_dataset(Path(path), Path(labels) if labels else None)
        else:
            self.set_status("尚無上次的資料集，請先匯入資料夾")

    def open_dataset(self, path: Path, labels_dir: Path | None = None):
        if self.busy or not self._ensure_saved():
            return
        self._run_job("正在掃描資料集與配對標註檔…", lambda: Dataset(path, labels_dir), self._dataset_opened)

    def _dataset_opened(self, dataset: Dataset):
        self.dataset = dataset
        self.current_index = -1
        self.names_dirty = False
        self.history, self.future = [], []
        self.search.set("")
        self.split_filter.set("所有分組")
        self.state_filter.set("所有狀態")
        self.split_combo.configure(values=["所有分組"] + sorted({r.split for r in dataset.records}))
        self.project_text.set(f"{dataset.root.name}  ·  {dataset.root}")
        self.refresh_classes()
        self.refresh_images()
        self._load_image(0)
        self.set_status(f"已匯入 {len(dataset.records):,} 張圖片；{sum(r.has_label for r in dataset.records):,} 份標註檔")
        try:
            self._save_settings()
        except OSError as exc:
            self.set_status(f"資料集已開啟，但無法儲存偏好設定：{exc}")
        if dataset.warnings:
            self._show_report("匯入提醒", "\n".join(dataset.warnings[:100]))

    def _search_changed(self, *_):
        if self._search_timer:
            self.after_cancel(self._search_timer)
        self._search_timer = self.after(180, self.refresh_images)

    def refresh_images(self):
        self._search_timer = None
        items = self.image_tree.get_children()
        if items:
            self.image_tree.delete(*items)
        self.visible_indices = []
        if not self.dataset:
            return
        query, split, state = self.search.get().casefold(), self.split_filter.get(), self.state_filter.get()
        for i, record in enumerate(self.dataset.records):
            if query not in record.display.casefold() or (split != "所有分組" and record.split != split):
                continue
            if (state == "有標註檔" and not record.has_label) or (state == "無標註檔" and record.has_label) or (state == "格式錯誤" and not record.error):
                continue
            if state == EMPTY_LABEL_FILTER and not record.has_empty_label:
                continue
            self.visible_indices.append(i)
            status = "錯誤" if record.error else (str(record.object_count) if record.object_count is not None and record.has_label else ("已有" if record.has_label else "—"))
            row_tag = "row-even" if len(self.visible_indices) % 2 else "row-odd"
            state_tag = "error" if record.error else ("missing" if not record.has_label else "normal")
            self.image_tree.insert("", "end", iid=str(i), text=record.image.name, values=(status,), tags=(row_tag, state_tag))
        self.summary_text.set(f"{len(self.visible_indices):,} / {len(self.dataset.records):,} 張  ·  {sum(r.has_label for r in self.dataset.records):,} 份標註")
        self._sync_image_selection()

    def _refresh_current_row(self):
        """Update a visited image without rebuilding a potentially huge file list."""
        if not self.dataset or self.current_index < 0:
            return
        if self.state_filter.get() != "所有狀態":
            self.refresh_images()
            return
        record = self.dataset.records[self.current_index]
        if self.image_tree.exists(str(self.current_index)):
            status = "錯誤" if record.error else (str(record.object_count) if record.has_label and record.object_count is not None else ("已有" if record.has_label else "—"))
            tag = "error" if record.error else ("missing" if not record.has_label else "normal")
            row_tag = self.image_tree.item(str(self.current_index), "tags")[0]
            self.image_tree.item(str(self.current_index), values=(status,), tags=(row_tag, tag))
        self.summary_text.set(f"{len(self.visible_indices):,} / {len(self.dataset.records):,} 張  ·  {sum(r.has_label for r in self.dataset.records):,} 份標註")
        self._sync_image_selection()

    def _sync_image_selection(self):
        if self.image_tree.exists(str(self.current_index)):
            self.image_tree.selection_set(str(self.current_index))
            self.image_tree.see(str(self.current_index))
        total = len(self.visible_indices)
        position = self.visible_indices.index(self.current_index) + 1 if self.current_index in self.visible_indices else "—"
        self.position_text.set(f"{position} / {total}")

    def _image_selected(self, _event):
        selected = self.image_tree.selection()
        if not selected or self.busy:
            return
        index = int(selected[0])
        if index != self.current_index:
            if self._ensure_saved():
                self._load_image(index)
            else:
                self._sync_image_selection()

    def _load_image(self, index):
        if not self.dataset or not 0 <= index < len(self.dataset.records):
            return
        record = self.dataset.records[index]
        self.current_index = index
        self.history, self.future = [], []
        try:
            with Image.open(record.image) as original:
                source = ImageOps.exif_transpose(original).convert("RGB")
            annotations, errors, self.label_hash = self.dataset.read(record)
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            record.error = str(exc)
            self.canvas.set_image(None, [])
            self.canvas.readonly = True
            self.saved_annotations = []
            self.set_status(f"讀取失敗：{record.image.name} — {exc}")
            self.path_text.set(f"圖片：{record.image}\n標註：{record.label}")
            self.refresh_objects()
            self._refresh_current_row()
            self._update_dirty()
            return
        self.canvas.names = self.dataset.names
        self.canvas.readonly = bool(errors)
        self.canvas.set_image(source, annotations)
        self.saved_annotations = copy.deepcopy(annotations)
        self.refresh_classes()
        self.refresh_objects()
        self._refresh_current_row()
        self.path_text.set(f"圖片：{record.image}\n標註：{record.label}")
        self.set_status(f"{record.image.name}  ·  {source.width} × {source.height}  ·  {len(annotations)} 個物件" if not errors else f"唯讀：標註格式錯誤，原始檔不會被覆蓋。{errors[0]}")
        self._update_dirty()

    def _dirty(self):
        return self.canvas.annotations != self.saved_annotations or self.names_dirty

    def _update_dirty(self):
        dirty = self._dirty()
        filename = self.dataset.records[self.current_index].image.name if self.dataset and self.current_index >= 0 else "本機標註工作台"
        self.title(f"{'● ' if dirty else ''}{filename} — YOLO Desk")
        readonly = self.canvas.readonly
        self.save_button.configure(state="disabled" if readonly or not self.dataset else "normal")
        self.save_state.set("格式錯誤，已鎖定標註" if readonly else ("● 尚未儲存的修改" if dirty else "已儲存 · 覆寫前自動備份"))
        self.save_state_label.configure(
            background="#ffe9ed" if readonly else ("#fff1d3" if dirty else SAVE_BG),
            foreground="#a62440" if readonly else ("#805300" if dirty else "#24614a"),
        )

    def _ensure_saved(self):
        if self.canvas._polygon or self.canvas._drag:
            self.set_status("請先完成目前的繪製，或按 Esc 取消，再切換圖片")
            return False
        if not self._dirty():
            return True
        if self.autosave.get():
            return self.save()
        result = messagebox.askyesnocancel("尚未儲存", "要儲存目前的修改嗎？\n選擇「否」會放棄修改。", parent=self)
        if result is None:
            return False
        if result:
            return self.save()
        self.canvas.annotations = copy.deepcopy(self.saved_annotations)
        self.canvas.selected = -1
        self.history, self.future = [], []
        if self.dataset and self.names_dirty:
            self.dataset.names = self.dataset._original_names.copy()
            for annotation in self.canvas.annotations:
                self.dataset.names.setdefault(annotation.class_id, f"class_{annotation.class_id}")
            self.names_dirty = False
        self.refresh_classes()
        self.refresh_objects()
        self.canvas.redraw()
        self._update_dirty()
        return True

    def save(self):
        if self.busy or not self.dataset or self.current_index < 0:
            return False
        if self.canvas._polygon or self.canvas._drag:
            self.set_status("請先完成繪製或按 Esc 取消，再儲存")
            return False
        if self.canvas.readonly and self.names_dirty and self.canvas.annotations == self.saved_annotations:
            try:
                self.dataset.save_names()
            except (OSError, ValueError) as exc:
                messagebox.showerror("類別儲存失敗", str(exc), parent=self)
                return False
            self.names_dirty = False
            self._update_dirty()
            self.canvas.fit()
            return True
        if self.canvas.readonly:
            self.set_status("此標註有錯誤，請先修正原始檔並重新載入")
            return False
        if not self.dataset.names and self.canvas.annotations:
            self.dataset.names[0] = "object"
        try:
            record = self.dataset.records[self.current_index]
            self.label_hash = self.dataset.save(record, self.canvas.annotations, self.label_hash)
        except (OSError, ValueError) as exc:
            messagebox.showerror("儲存失敗，修改仍保留在畫面", str(exc), parent=self)
            return False
        self.saved_annotations = copy.deepcopy(self.canvas.annotations)
        self.names_dirty = False
        self._refresh_current_row()
        self._update_dirty()
        self.canvas.fit()
        self.set_status(f"已儲存：{record.label}")
        return True

    def navigate(self, step):
        if self.busy or not self.visible_indices:
            return
        # Choose the destination before auto-save changes the filtered list.
        if self.current_index in self.visible_indices:
            position = self.visible_indices.index(self.current_index) + step
        else:
            position = 0 if step > 0 else len(self.visible_indices) - 1
        target = self.visible_indices[position] if 0 <= position < len(self.visible_indices) else None
        if not self._ensure_saved():
            return
        if target is not None:
            self._load_image(target)
        else:
            self.set_status("已到目前清單的第一張" if step < 0 else "已到目前清單的最後一張")

    def reload_image(self):
        if self.busy or self.current_index < 0:
            return
        if self._dirty() and not messagebox.askyesno("重新載入", "放棄目前這張圖片未儲存的標註修改，重新讀取磁碟內容？", parent=self):
            return
        self._load_image(self.current_index)

    def _changed(self, before):
        self.history.append(copy.deepcopy(before))
        self.history = self.history[-100:]
        self.future.clear()
        self.refresh_objects()
        self._update_dirty()

    def _editable(self):
        return bool(self.dataset and self.canvas.source and not self.busy and not self.canvas.readonly)

    def undo(self):
        if not self._editable() or not self.history:
            return
        self.canvas.cancel()
        self.future.append(copy.deepcopy(self.canvas.annotations))
        self.canvas.annotations = self.history.pop()
        self.canvas.selected = -1
        self._after_history()

    def redo(self):
        if not self._editable() or not self.future:
            return
        self.canvas.cancel()
        self.history.append(copy.deepcopy(self.canvas.annotations))
        self.canvas.annotations = self.future.pop()
        self.canvas.selected = -1
        self._after_history()

    def _after_history(self):
        self.canvas.redraw()
        self.refresh_objects()
        self._update_dirty()

    def delete_selected(self):
        if self._editable() and self.canvas.can_select(self.canvas.selected):
            self.canvas.cancel()
            before = copy.deepcopy(self.canvas.annotations)
            del self.canvas.annotations[self.canvas.selected]
            self.canvas.selected = -1
            self._changed(before)
            self.canvas.redraw()

    def mark_negative(self):
        if not self._editable():
            return
        if self.canvas.annotations and not messagebox.askyesno("標記為無物件", "清除本張所有框並儲存空的 YOLO 標註？\n原始標註會先備份，也可用 Ctrl+Z 復原。", parent=self):
            return
        self.canvas.cancel()
        before = copy.deepcopy(self.canvas.annotations)
        self.canvas.annotations = []
        self.canvas.selected = -1
        if before:
            self._changed(before)
        self.canvas.redraw()
        self.save()

    def set_tool(self, tool):
        if self.busy:
            return
        if tool == "box" and self.dataset and not self.dataset.names:
            self.add_class()
            if not self.dataset.names:
                return
        self.canvas.set_tool(tool)

    def _tool_changed(self, tool):
        self.select_button.configure(style="Active.Tool.TButton" if tool == "select" else "Tool.TButton",
                                     text="● 選取  V" if tool == "select" else "選取  V")
        self.box_button.configure(style="Active.Tool.TButton" if tool == "box" else "Tool.TButton",
                                  text="● 矩形框  B" if tool == "box" else "矩形框  B")
        self.canvas.focus_set()
        self.set_status("空白處拖曳建立矩形框；點擊既有框自動切換選取；A / D 切換圖片" if tool == "box" else "點選框後拖曳移動，拉動角點或邊點調整大小；1–9 改類別；Delete 刪除")

    def refresh_classes(self):
        if not self.dataset:
            return
        active = self.canvas.current_class
        self._class_ids = sorted(self.dataset.names)
        self.class_combo.configure(values=[f"{i}  ·  {self.dataset.names[i]}" for i in self._class_ids])
        if self._class_ids:
            index = self._class_ids.index(active) if active in self._class_ids else 0
            self.class_combo.current(index)
            self.canvas.current_class = self._class_ids[index]
        else:
            self.class_combo.set("")
        self.canvas.names = self.dataset.names
        self.canvas.redraw()

    def _class_selected(self, _event=None):
        index = self.class_combo.current()
        if 0 <= index < len(self._class_ids):
            self.canvas.current_class = self._class_ids[index]
            self.canvas.focus_set()

    def choose_class_index(self, index):
        if 0 <= index < len(self._class_ids):
            self.class_combo.current(index)
            self._class_selected()
            if (self._editable() and self.canvas.can_select(self.canvas.selected)
                    and not self.canvas._drag and not self.canvas._polygon):
                self.apply_class()
                self.set_status(f"已將選取框改為：{self.class_combo.get()}")
            else:
                self.set_status(f"目前繪製類別：{self.class_combo.get()}")

    def add_class(self):
        if self.busy or not self.dataset:
            return
        class_id = max(self.dataset.names, default=-1) + 1
        if class_id > MAX_CLASS_ID:
            messagebox.showerror("無法新增", "已達類別 ID 上限", parent=self)
            return
        name = simpledialog.askstring("新增類別", f"新類別 ID：{class_id}\n輸入類別名稱：", parent=self)
        if name is not None and name.strip():
            self.dataset.names[class_id] = name.strip()
            self.canvas.current_class = class_id
            self.names_dirty = True
            self.refresh_classes()
            self._update_dirty()

    def rename_class(self):
        if self.busy or not self.dataset or not self._class_ids:
            return
        class_id = self.canvas.current_class
        name = simpledialog.askstring("重新命名類別", f"修改 ID {class_id} 的顯示名稱（ID 不會變動）：", initialvalue=self.dataset.names[class_id], parent=self)
        if name is not None and name.strip() and name.strip() != self.dataset.names[class_id]:
            self.dataset.names[class_id] = name.strip()
            self.names_dirty = True
            self.refresh_classes()
            self.refresh_objects()
            self._update_dirty()

    def apply_class(self):
        if not self._editable() or not self._class_ids or not self.canvas.can_select(self.canvas.selected):
            return
        before = copy.deepcopy(self.canvas.annotations)
        self.canvas.annotations[self.canvas.selected].class_id = self.canvas.current_class
        if before != self.canvas.annotations:
            self._changed(before)
            self.canvas.redraw()

    def _object_table_resized(self, event):
        self._queue_object_grid(event)
        self._queue_object_hover()

    def _queue_object_grid(self, _event):
        # Read widths after Tk's native column-resize binding has run.
        if self._grid_sync is None:
            self._grid_sync = self.after_idle(self._sync_object_grid)

    def _sync_object_grid(self):
        self._grid_sync = None
        tree = self.object_tree
        widths = tuple(tree.column(column, "width") for column in tree["columns"])
        if widths == self._grid_widths:
            return
        self._grid_widths = widths
        width = sum(widths)
        height = self._row_grid.height()
        self._row_grid.configure(width=width)
        self._row_grid.blank()
        self._row_grid.put(BORDER, to=(0, height - 1, width, height))
        right = 0
        for column_width in widths:
            right += column_width
            self._row_grid.put(BORDER, to=(right - 1, 0, right, height))

    def _hover_at(self, x, y):
        tree = self.object_tree
        row = tree.identify_row(y) if tree.identify_region(x, y) == "cell" else ""
        self._set_object_hover(int(row) if row else -1)

    def _set_object_hover(self, index):
        previous = self.canvas.hovered
        if previous == index:
            return
        tree = self.object_tree
        if previous >= 0 and tree.exists(str(previous)):
            tags = tree.item(str(previous), "tags")
            tree.item(str(previous), tags=tuple(tag for tag in tags if tag != "hover"))
        if index >= 0:
            tags = tree.item(str(index), "tags")
            tree.item(str(index), tags=("hover", *tags))
        self.canvas.set_hovered(index)

    def _queue_object_hover(self):
        # Scrolling and list rebuilds can move another row under a stationary pointer.
        if self._hover_sync is None:
            self._hover_sync = self.after_idle(self._sync_object_hover)

    def _sync_object_hover(self):
        self._hover_sync = None
        tree = self.object_tree
        x, y = tree.winfo_pointerxy()
        if tree.winfo_containing(x, y) == tree:
            self._hover_at(x - tree.winfo_rootx(), y - tree.winfo_rooty())
        else:
            self._set_object_hover(-1)

    def _object_control_clicked(self, event):
        tree = self.object_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        column = tree.identify_column(event.x)
        row = tree.identify_row(event.y)
        if not row or column not in {"#3", "#4"}:
            return
        if self.busy or self.canvas._drag or self.canvas._polygon:
            return "break"
        index = int(row)
        edit_id = self.canvas.annotations[index].edit_id
        active_ids = self.canvas.locked_ids if column == "#3" else self.canvas.hidden_ids
        active_ids.symmetric_difference_update({edit_id})
        tree.set(row, column, "☑" if edit_id in active_ids else "☐")
        if index == self.canvas.selected and not self.canvas.can_select(index):
            self.canvas.selected = -1
            self._selected(-1)
        self.canvas.redraw()
        return "break"

    def refresh_objects(self):
        self._set_object_hover(-1)
        self._refreshing_objects = True
        items = self.object_tree.get_children()
        if items:
            self.object_tree.delete(*items)
        for i, annotation in enumerate(self.canvas.annotations):
            name = self.canvas.names.get(annotation.class_id, f"class_{annotation.class_id}")
            tag = f"class{annotation.class_id}"
            self.object_tree.tag_configure(tag, foreground=LIST_COLORS[annotation.class_id % len(LIST_COLORS)])
            row_tag = "row-even" if i % 2 == 0 else "row-odd"
            locked = "☑" if annotation.edit_id in self.canvas.locked_ids else "☐"
            hidden = "☑" if annotation.edit_id in self.canvas.hidden_ids else "☐"
            self.object_tree.insert("", "end", iid=str(i), values=(i + 1, f"{annotation.class_id} · {name}", locked, hidden),
                                    tags=(row_tag, tag))
        self._refreshing_objects = False
        self._selected(self.canvas.selected)
        self._queue_object_hover()

    def _selected(self, index):
        if 0 <= index < len(self.canvas.annotations):
            if self.object_tree.exists(str(index)):
                self.object_tree.selection_set(str(index))
                self.object_tree.see(str(index))
            annotation = self.canvas.annotations[index]
            x1, y1, x2, y2 = annotation.bounds()
            if self.canvas.source:
                width, height = self.canvas.source.size
                self.object_info.set(f"#{index + 1} · 類別 {annotation.class_id} · {(x2 - x1) * width:.0f} × {(y2 - y1) * height:.0f} px\n位置 {x1 * width:.0f}, {y1 * height:.0f} px")
        else:
            selected = self.object_tree.selection()
            if selected:
                self.object_tree.selection_remove(*selected)
            self.object_info.set(f"共 {len(self.canvas.annotations)} 個物件\n選取框後可拖曳位置與角點")

    def _object_selected(self, _event):
        if self._refreshing_objects or self.busy:
            return
        selected = self.object_tree.selection()
        if selected:
            index = int(selected[0])
            if not self.canvas.can_select(index):
                self._selected(self.canvas.selected)
                return
            if index != self.canvas.selected:
                self.canvas.selected = index
                self._selected(index)
                self.canvas.redraw()

    def _toggle_labels(self):
        self.canvas.show_labels = self.labels_visible.get()
        self.canvas.redraw()

    def _toggle_annotations(self):
        self.canvas.show_annotations = not self.canvas.show_annotations
        self.canvas.redraw()

    def validate_dataset(self):
        if self.busy or not self.dataset or not self._ensure_saved():
            return
        dataset = self.dataset
        def validate():
            problems, boxes, empty, missing = [], 0, 0, 0
            for i, record in enumerate(dataset.records):
                try:
                    annotations, errors, _ = dataset.read(record)
                    with Image.open(record.image) as image:
                        image.verify()
                    boxes += len(annotations)
                    empty += int(record.has_empty_label)
                    missing += int(not record.has_label)
                    problems.extend(f"{record.display} — {error}" for error in errors)
                except (OSError, ValueError) as exc:
                    record.error = str(exc)
                    problems.append(f"{record.display} — {exc}")
                if i % 50 == 0:
                    self._messages.put(("progress", f"檢查標註與影像… {i + 1:,} / {len(dataset.records):,}", None))
            return f"圖片：{len(dataset.records):,}\n物件：{boxes:,}\n空標註（無物件）：{empty:,}\n未建立標註檔：{missing:,}\n問題：{len(problems):,}\n\n" + ("\n".join(problems[:300]) if problems else "所有現有標註檔格式正確，影像可讀取。")
        def done(report):
            self.refresh_classes()
            self.refresh_images()
            self._update_dirty()
            self.set_status("資料集檢查完成")
            self._show_report("資料集檢查結果", report)
        self._run_job("正在檢查全部標註與影像…", validate, done)

    def export_dataset(self):
        if self.busy or not self.dataset or not self._ensure_saved():
            return
        path = filedialog.askdirectory(title="選擇空資料夾，匯出可攜的 YOLO 副本", parent=self)
        if not path:
            return
        dataset = self.dataset
        def export():
            dataset.export(Path(path), lambda count, total: self._messages.put(("progress", f"匯出圖片與標註… {count:,} / {total:,}", None)) if count % 25 == 0 or count == total else None)
            return path
        def done(output):
            self.refresh_classes()
            self.set_status(f"已匯出完整 YOLO 副本：{output}")
            messagebox.showinfo("匯出完成", f"已輸出 images、labels 與 data.yaml：\n{output}\n\n原有分組會保留；未分組圖片歸入 train。", parent=self)
        self._run_job("正在檢查並匯出 YOLO 副本…", export, done)

    def open_location(self):
        if self.dataset and self.current_index >= 0:
            path = self.dataset.records[self.current_index].label
            if sys.platform == "win32":
                if path.exists():
                    subprocess.Popen(["explorer.exe", "/select,", str(path)])
                else:
                    os.startfile(str(self.dataset.records[self.current_index].image.parent))

    def _show_report(self, title, report):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry("760x480")
        dialog.transient(self)
        dialog.configure(background=PANEL)
        text = tk.Text(dialog, background=BG, foreground=TEXT, insertbackground=TEXT, wrap="word", padx=18, pady=18, borderwidth=0)
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("1.0", report)
        text.configure(state="disabled")
        ttk.Button(dialog, text="關閉", command=dialog.destroy).pack(pady=(0, 12))

    def show_help(self):
        self._show_report("YOLO Desk 操作說明", "直接匯入已標記資料\n\n"
            "• 匯入資料夾：選擇包含 images / labels 的根目錄。\n"
            "• 開啟 YAML：讀取 train / val / test 路徑、影像清單與 names。\n"
            "• 自訂配對：選擇圖片和標註資料夾，依相同相對路徑配對。\n"
            "• 支援 images/train ↔ labels/train、train/images ↔ train/labels、圖片與 txt 同目錄。\n"
            "• 類別會由 data.yaml、dataset.yaml、classes.txt 或 obj.names 載入。\n\n"
            "標註操作\n\n"
            "B：畫框；V：選取；拖曳框：移動；拖曳角點/邊點：調整大小。\n"
            "矩形框模式下點擊既有框內部，會自動切換選取並可直接拖曳調整。\n"
            "在「本張標註」勾選「固定」可保留框線並穿過它畫框，勾選「隱藏」可收起框線和名稱。\n"
            "游標移到清單某一列時，圖片會以黃色外框和編號標示該物件。\n"
            "每列只控制該物件，再點一次即可解除。切換圖片或重新載入會重設。\n"
            "A / D 或左右鍵：上一張 / 下一張；F：圖片適合視窗。\n"
            "滾輪：以游標為中心縮放；右鍵 / 中鍵 / 空白鍵＋拖曳：平移。\n"
            "1–9：依列表順序選擇前九個類別；已有選取框時直接修改該框類別，可復原。\n"
            "未選取框時，數字鍵只切換後續繪製類別；也可用「套用類別到選取框」。\n"
            "Delete：刪除選取框；Ctrl+Z / Ctrl+Y：復原 / 重做。\n"
            "Ctrl+S：儲存，成功後自動適合畫面；H：隱藏 / 顯示框；Esc：取消目前的繪製。\n\n"
            "儲存與備份\n\n"
            "修改會寫回原本的 YOLO .txt；每次覆寫前在資料集 .yolo_desk/backups 備份。\n"
            "備份旁的 .origin.txt 記錄原始路徑。修改類別名稱時也會備份原始類別檔。\n"
            "預設在切換圖片時自動儲存，可取消勾選。未修改的圖片不會自動建立空標註。\n"
            "「標記為無物件」會明確儲存空標註；「—」代表還沒有標註檔。\n"
            "格式錯誤的標註僅供檢視，避免部分解析後遺失資料。外部變更會擋下儲存。\n"
            "匯出副本會保留原分組，未分組歸入 train，不會自動拆出驗證集。\n\n"
            "資料留在本機；無需帳號、伺服器或環境變數。")

    def close(self):
        if self.busy:
            self.set_status("目前作業仍在進行，完成後再關閉視窗")
            return
        if not self._ensure_saved():
            return
        try:
            self._save_settings()
        except OSError as exc:
            messagebox.showwarning("設定未儲存", str(exc), parent=self)
        self.destroy()
