"""Image-space annotation editing with cursor-centered zoom and bounded dragging."""
from __future__ import annotations

import copy
import math
import tkinter as tk
from typing import Callable

from PIL import Image, ImageTk

from .dataset import Annotation

COLORS = ["#45d6b0", "#70a7ff", "#ffbe69", "#dd91ff", "#ff7f96", "#62d8ef", "#c9dd72", "#c2a590"]


class AnnotationCanvas(tk.Canvas):
    def __init__(self, parent, on_change: Callable, on_select: Callable, on_status: Callable,
                 on_tool_change: Callable[[str], None] | None = None):
        super().__init__(parent, background="#10151d", highlightthickness=0, cursor="crosshair", takefocus=True)
        self.on_change, self.on_select, self.on_status = on_change, on_select, on_status
        self.on_tool_change = on_tool_change
        self.source: Image.Image | None = None
        self.annotations: list[Annotation] = []
        self.names: dict[int, str] = {}
        self.current_class = 0
        self.selected = -1
        self.tool = "select"
        self.readonly = False
        self.scale, self.ox, self.oy = 1.0, 0.0, 0.0
        self.show_labels = True
        self.show_annotations = True
        self._photo = None
        self._image_key = None
        self._drag = None
        self._before: list[Annotation] | None = None
        self._polygon: list[tuple[float, float]] = []
        self._preview = None
        self._space = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self.zoom(1.15, e.x, e.y))
        self.bind("<Button-5>", lambda e: self.zoom(1 / 1.15, e.x, e.y))
        for button in (2, 3):
            self.bind(f"<ButtonPress-{button}>", self._pan_start)
            self.bind(f"<B{button}-Motion>", self._motion)
            self.bind(f"<ButtonRelease-{button}>", self._release)
        self.bind("<KeyPress-space>", self._space_down)
        self.bind("<KeyRelease-space>", self._space_up)
        self.bind("<FocusOut>", self._space_up)

    def _space_down(self, _event=None):
        self._space = True
        self.configure(cursor="fleur")
        return "break"

    def _space_up(self, _event=None):
        self._space = False
        self.configure(cursor="crosshair" if self.tool != "select" else "arrow")

    def set_tool(self, tool: str) -> None:
        self.cancel()
        self.tool = tool
        self.configure(cursor="arrow" if tool == "select" else "crosshair")
        if self.on_tool_change:
            self.on_tool_change(tool)

    def set_image(self, source: Image.Image | None, annotations: list[Annotation]) -> None:
        self.cancel()
        self.source = source
        self.annotations = annotations
        self.selected = -1
        self._image_key = None
        self.fit()

    def fit(self) -> None:
        if self.source:
            width, height = self.source.size
            self.scale = min(max(1, self.winfo_width() - 64) / width, max(1, self.winfo_height() - 64) / height)
            self.ox = (self.winfo_width() - width * self.scale) / 2
            self.oy = (self.winfo_height() - height * self.scale) / 2
        self.redraw()

    def zoom(self, factor: float, x: float | None = None, y: float | None = None) -> None:
        if not self.source:
            return
        x = self.winfo_width() / 2 if x is None else x
        y = self.winfo_height() / 2 if y is None else y
        old = self.scale
        self.scale = max(0.005, min(32, old * factor))
        self.ox = x - (x - self.ox) * self.scale / old
        self.oy = y - (y - self.oy) * self.scale / old
        self.redraw()

    def _wheel(self, event):
        self.zoom(1.15 if event.delta > 0 else 1 / 1.15, event.x, event.y)
        return "break"

    def screen(self, point: tuple[float, float]) -> tuple[float, float]:
        assert self.source
        return self.ox + point[0] * self.source.width * self.scale, self.oy + point[1] * self.source.height * self.scale

    def normalized(self, x: float, y: float, clamp: bool = True) -> tuple[float, float]:
        assert self.source
        point = ((x - self.ox) / (self.source.width * self.scale), (y - self.oy) / (self.source.height * self.scale))
        return tuple(max(0.0, min(1.0, v)) for v in point) if clamp else point

    def _handles(self, annotation: Annotation):
        if annotation.kind == "polygon":
            return annotation.points
        x1, y1, x2, y2 = annotation.bounds()
        xm, ym = (x1 + x2) / 2, (y1 + y2) / 2
        return [(x1, y1), (xm, y1), (x2, y1), (x2, ym), (x2, y2), (xm, y2), (x1, y2), (x1, ym)]

    def _hit_handle(self, x: float, y: float) -> int:
        if 0 <= self.selected < len(self.annotations):
            for i, point in enumerate(self._handles(self.annotations[self.selected])):
                px, py = self.screen(point)
                if abs(x - px) <= 8 and abs(y - py) <= 8:
                    return i
        return -1

    def _contains(self, annotation: Annotation, point: tuple[float, float]) -> bool:
        x, y = point
        if annotation.kind == "box":
            x1, y1, x2, y2 = annotation.bounds()
            return x1 <= x <= x2 and y1 <= y <= y2
        inside = False
        pts = annotation.points
        j = len(pts) - 1
        for i, (xi, yi) in enumerate(pts):
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    def _press(self, event):
        self.focus_set()
        if not self.source:
            return
        if self._space:
            self._pan_start(event)
            return
        if self.readonly or not self.show_annotations:
            return
        point = self.normalized(event.x, event.y)
        raw = self.normalized(event.x, event.y, False)
        if self.tool == "box" and (
            self._hit_handle(event.x, event.y) >= 0
            or any(self._contains(annotation, raw) for annotation in self.annotations)
        ):
            # Reuse selection/drag behavior for the same press, without adding a box.
            self.set_tool("select")
        if self.tool == "select":
            handle = self._hit_handle(event.x, event.y)
            if handle >= 0:
                self._before = copy.deepcopy(self.annotations)
                self._drag = ("handle", handle, point)
                return
            self.selected = next((i for i in reversed(range(len(self.annotations))) if self._contains(self.annotations[i], raw)), -1)
            self.on_select(self.selected)
            if self.selected >= 0:
                self._before = copy.deepcopy(self.annotations)
                self._drag = ("move", point)
        elif all(0 <= v <= 1 for v in raw):
            if self.tool == "box":
                self._before = copy.deepcopy(self.annotations)
                self._drag = ("box", point, point)
            elif self.tool == "polygon":
                if len(self._polygon) >= 3:
                    first = self.screen(self._polygon[0])
                    if math.dist(first, (event.x, event.y)) < 10:
                        self.finish_polygon()
                        return
                self._polygon.append(point)
                self._preview = point
        self.redraw()

    def _pan_start(self, event):
        self.focus_set()
        if self.source:
            self._drag = ("pan", event.x, event.y, self.ox, self.oy)

    def _motion(self, event):
        if not self.source or not self._drag:
            return
        mode = self._drag[0]
        if mode == "pan":
            _, x, y, ox, oy = self._drag
            self.ox, self.oy = ox + event.x - x, oy + event.y - y
        else:
            point = self.normalized(event.x, event.y)
            if mode == "box":
                self._drag = ("box", self._drag[1], point)
            elif mode == "move":
                start = self._drag[1]
                original = self._before[self.selected]
                x1, y1, x2, y2 = original.bounds()
                dx = max(-x1, min(1 - x2, point[0] - start[0]))
                dy = max(-y1, min(1 - y2, point[1] - start[1]))
                self.annotations[self.selected].points = [(x + dx, y + dy) for x, y in original.points]
            elif mode == "handle":
                annotation = self.annotations[self.selected]
                handle = self._drag[1]
                if annotation.kind == "polygon":
                    annotation.points[handle] = point
                else:
                    x1, y1, x2, y2 = self._before[self.selected].bounds()
                    if handle in {0, 6, 7}:
                        x1 = min(point[0], x2 - 1 / self.source.width)
                    if handle in {2, 3, 4}:
                        x2 = max(point[0], x1 + 1 / self.source.width)
                    if handle in {0, 1, 2}:
                        y1 = min(point[1], y2 - 1 / self.source.height)
                    if handle in {4, 5, 6}:
                        y2 = max(point[1], y1 + 1 / self.source.height)
                    annotation.points = [(max(0, x1), max(0, y1)), (min(1, x2), min(1, y2))]
        self.redraw()

    def _release(self, _event=None):
        if not self._drag:
            return
        mode = self._drag[0]
        if mode == "box":
            _, first, last = self._drag
            if self.source and abs(first[0] - last[0]) * self.source.width >= 2 and abs(first[1] - last[1]) * self.source.height >= 2:
                self.annotations.append(Annotation(self.current_class, "box", [
                    (min(first[0], last[0]), min(first[1], last[1])),
                    (max(first[0], last[0]), max(first[1], last[1])),
                ]))
                self.selected = len(self.annotations) - 1
        before = self._before
        self._drag, self._before = None, None
        if mode != "pan" and before is not None and before != self.annotations:
            # Invalid/degenerate polygon edits must not enter the undo/save history.
            from .dataset import serialize_labels
            try:
                serialize_labels(self.annotations)
            except ValueError:
                self.annotations = before
                self.on_status("頂點位置會形成無效多邊形，已還原這次移動")
            else:
                self.on_change(before)
        self.on_select(self.selected)
        self.redraw()

    def _hover(self, event):
        if self.source and self._polygon:
            self._preview = self.normalized(event.x, event.y)
            self.redraw()

    def finish_polygon(self):
        if len(self._polygon) < 3:
            if self._polygon:
                self.on_status("多邊形至少需要 3 個頂點，再按 Enter 完成")
            return
        before = copy.deepcopy(self.annotations)
        annotation = Annotation(self.current_class, "polygon", self._polygon.copy())
        from .dataset import serialize_labels
        try:
            serialize_labels([annotation])
        except ValueError:
            self.on_status("多邊形面積為 0，請重新選取頂點")
            return
        self.annotations.append(annotation)
        self.selected = len(self.annotations) - 1
        self._polygon, self._preview = [], None
        self.on_change(before)
        self.on_select(self.selected)
        self.redraw()

    def cancel(self):
        if self._before is not None:
            self.annotations = self._before
        self._polygon, self._preview, self._drag, self._before = [], None, None, None
        self.redraw()

    def redraw(self):
        width, height = self.winfo_width(), self.winfo_height()
        self.delete("annotation")
        self.delete("empty")
        if not self.source:
            self.delete("image")
            self.create_text(width / 2, height / 2 - 58, text="YOLO Desk", fill="#e7edf7", font=("Segoe UI", 32, "bold"), tags="empty")
            self.create_text(width / 2, height / 2, text="選擇資料夾，即可接續你的標註", fill="#9eafc5", font=("Microsoft JhengHei UI", 15), tags="empty")
            self.create_text(width / 2, height / 2 + 45, text="images + labels    /    data.yaml    /    自訂資料夾配對", fill="#64758b", font=("Microsoft JhengHei UI", 10), tags="empty")
            return
        key = (id(self.source), self.scale, self.ox, self.oy, width, height)
        if key != self._image_key:
            self.delete("image")
            left = max(0, math.floor(-self.ox / self.scale))
            top = max(0, math.floor(-self.oy / self.scale))
            right = min(self.source.width, math.ceil((width - self.ox) / self.scale))
            bottom = min(self.source.height, math.ceil((height - self.oy) / self.scale))
            if right > left and bottom > top:
                crop = self.source.crop((left, top, right, bottom))
                size = (max(1, round((right - left) * self.scale)), max(1, round((bottom - top) * self.scale)))
                resized = crop.resize(size, Image.Resampling.BILINEAR)
                self._photo = ImageTk.PhotoImage(resized)
                self.create_image(self.ox + left * self.scale, self.oy + top * self.scale, image=self._photo, anchor="nw", tags="image")
                self.tag_lower("image")
            self._image_key = key
        self.create_text(width - 16, height - 16, text=f"{self.scale * 100:.0f}%", anchor="se", fill="#a9bbd2", font=("Segoe UI", 10), tags="annotation")
        if not self.show_annotations:
            return
        for index, annotation in enumerate(self.annotations):
            color = COLORS[annotation.class_id % len(COLORS)]
            selected = index == self.selected
            points = [self.screen(point) for point in annotation.points]
            if annotation.kind == "box":
                self.create_rectangle(*points[0], *points[1], outline=color, width=3 if selected else 2, tags="annotation")
            else:
                self.create_polygon(*[v for p in points for v in p], outline=color, fill="", width=3 if selected else 2, tags="annotation")
            if self.show_labels:
                x, y = self.screen(annotation.bounds()[:2])
                text = f" {index + 1} · {self.names.get(annotation.class_id, f'class_{annotation.class_id}')} "
                label = self.create_text(x, y - 4, text=text, anchor="sw", fill="#101820", font=("Microsoft JhengHei UI", 10, "bold"), tags="annotation")
                bbox = self.bbox(label)
                background = self.create_rectangle(*bbox, fill=color, outline="", tags="annotation")
                self.tag_raise(label, background)
            if selected:
                for point in self._handles(annotation):
                    x, y = self.screen(point)
                    self.create_rectangle(x - 4, y - 4, x + 4, y + 4, fill="#f5f8ff", outline=color, width=2, tags="annotation")
        if self._drag and self._drag[0] == "box":
            self.create_rectangle(*self.screen(self._drag[1]), *self.screen(self._drag[2]), outline=COLORS[self.current_class % len(COLORS)], width=2, dash=(5, 3), tags="annotation")
        if self._polygon:
            points = self._polygon + ([self._preview] if self._preview else [])
            coords = [value for point in points for value in self.screen(point)]
            if len(coords) >= 4:
                self.create_line(*coords, fill=COLORS[self.current_class % len(COLORS)], width=2, dash=(5, 3), tags="annotation")
            for point in self._polygon:
                x, y = self.screen(point)
                self.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#45d6b0", outline="#ffffff", tags="annotation")
