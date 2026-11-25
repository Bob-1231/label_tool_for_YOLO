"""YOLO discovery, loss-aware parsing, conflict detection and atomic persistence."""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Callable

import yaml

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
SPLITS = {"train", "val", "valid", "validation", "test"}
MAX_CLASS_ID = 9999
_annotation_ids = count()


@dataclass(eq=True)
class Annotation:
    class_id: int
    kind: str
    points: list[tuple[float, float]]
    # Keep the same identity when an undo snapshot copies the geometry.
    edit_id: int = field(default_factory=_annotation_ids.__next__, init=False, compare=False, repr=False)

    def bounds(self) -> tuple[float, float, float, float]:
        xs, ys = zip(*self.points)
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class ImageRecord:
    image: Path
    label: Path
    split: str
    display: str
    has_label: bool
    object_count: int | None = None
    error: str = ""

    @property
    def has_empty_label(self) -> bool:
        return self.has_label and self.object_count == 0 and not self.error


def fingerprint(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def atomic_write(path: Path, content: bytes, backup_root: Path | None = None) -> None:
    """Keep previous bytes before replacement; temporary file is on the same volume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    if backup_root is not None and path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
        destination = backup_root / f"{stamp}_{key}_{path.name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        # Keep the original path beside each backup, including external label folders.
        destination.with_name(destination.name + ".origin.txt").write_text(str(path), encoding="utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".yolo-desk-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_labels(text: str) -> tuple[list[Annotation], list[str]]:
    annotations, errors = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            fields = line.split()
            if not re.fullmatch(r"\d+", fields[0]):
                raise ValueError("類別 ID 必須是非負整數")
            class_id = int(fields[0])
            if class_id > MAX_CLASS_ID:
                raise ValueError(f"類別 ID 超過 {MAX_CLASS_ID}")
            coords = [float(value) for value in fields[1:]]
            if not all(math.isfinite(v) and 0 <= v <= 1 for v in coords):
                raise ValueError("座標必須是 0 到 1 之間的有限數值")
            if len(coords) == 4:
                x, y, width, height = coords
                if width <= 0 or height <= 0:
                    raise ValueError("框寬與框高必須大於 0")
                edges = (x - width / 2, y - height / 2, x + width / 2, y + height / 2)
                if min(edges) < -0.000001 or max(edges) > 1.000001:
                    raise ValueError("矩形超出影像邊界")
                x1, y1, x2, y2 = (max(0.0, min(1.0, v)) for v in edges)
                annotations.append(Annotation(class_id, "box", [(x1, y1), (x2, y2)]))
            elif len(coords) >= 6 and len(coords) % 2 == 0:
                points = list(zip(coords[::2], coords[1::2]))
                area = sum(points[i][0] * points[(i + 1) % len(points)][1]
                           - points[(i + 1) % len(points)][0] * points[i][1]
                           for i in range(len(points)))
                if abs(area) < 1e-12:
                    raise ValueError("多邊形面積為 0")
                annotations.append(Annotation(class_id, "polygon", points))
            else:
                raise ValueError("需要 5 欄的偵測框，或至少 3 個頂點的分割多邊形")
        except (ValueError, IndexError) as exc:
            errors.append(f"第 {number} 行：{exc}")
    return annotations, errors


def serialize_labels(annotations: list[Annotation]) -> str:
    lines = []
    for annotation in annotations:
        if annotation.kind == "box":
            x1, y1, x2, y2 = annotation.bounds()
            coords = [(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1]
        else:
            coords = [v for point in annotation.points for v in point]
        lines.append(f"{annotation.class_id} " + " ".join(f"{v:.8f}" for v in coords))
    result = "\n".join(lines) + ("\n" if lines else "")
    _, errors = parse_labels(result)
    if errors:
        raise ValueError("無法儲存不合法的標註：\n" + "\n".join(errors))
    return result


def _image_files(folder: Path):
    if not folder.is_dir():
        return
    for parent, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in {"__pycache__", "node_modules"})
        for name in sorted(files):
            path = Path(parent) / name
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                yield path.resolve()


class Dataset:
    def __init__(self, source: Path, labels_dir: Path | None = None):
        self.source = source.expanduser().resolve()
        if not self.source.exists():
            raise ValueError(f"找不到資料集：{self.source}")
        self.root = self.source.parent if self.source.is_file() else self.source
        self.labels_dir = labels_dir.expanduser().resolve() if labels_dir else None
        self.mapping_root = self.root
        self.yaml_path: Path | None = None
        self.names_path: Path | None = None
        self.yaml_data: dict = {}
        self.names: dict[int, str] = {}
        self.records: list[ImageRecord] = []
        self.warnings: list[str] = []
        self._metadata_hash: str | None = None
        self._original_names: dict[int, str] = {}
        self._discover()

    @property
    def backup_root(self) -> Path:
        return self.root / ".yolo_desk" / "backups"

    def _discover(self) -> None:
        if self.source.is_file():
            if self.source.suffix.lower() not in {".yaml", ".yml"}:
                raise ValueError("請選擇資料夾或 YOLO YAML 設定檔")
            self.yaml_path = self.source
        # Check each dataset level before considering metadata from its parent.
        text_names_path = None
        for folder in (self.root, self.root.parent, self.root.parent.parent):
            if self.yaml_path is None:
                candidates = [folder / "data.yaml", folder / "dataset.yaml", folder / "data.yml"]
                if folder == self.root:
                    candidates += sorted(folder.glob("*.yaml")) + sorted(folder.glob("*.yml"))
                self.yaml_path = next((p for p in candidates if p.is_file()), None)
            names_folders = [folder, folder / "labels"]
            if folder == self.root and self.labels_dir:
                names_folders.insert(0, self.labels_dir)
            text_names_path = next((directory / name for directory in names_folders
                                    for name in ("classes.txt", "obj.names") if (directory / name).is_file()), None)
            if text_names_path:
                break
        if self.yaml_path:
            loaded = yaml.safe_load(self.yaml_path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                raise ValueError("YAML 內容必須是物件，內含 names、train、val 等欄位")
            self.yaml_data = loaded
            if loaded.get("kpt_shape") or loaded.get("task") in {"pose", "obb", "classify"}:
                raise ValueError("目前支援偵測框與分割多邊形，不支援 pose、OBB 或分類資料集")
            raw_names = loaded.get("names", {})
            if isinstance(raw_names, list):
                raw_names = dict(enumerate(raw_names))
            if not isinstance(raw_names, dict):
                raise ValueError("YAML 的 names 必須是列表或 ID 對應名稱的字典")
            try:
                for key, value in raw_names.items():
                    if not re.fullmatch(r"\d+", str(key)) or not 0 <= int(key) <= MAX_CLASS_ID:
                        raise ValueError("不合法的類別 ID")
                    self.names[int(key)] = str(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"YAML 類別格式錯誤：{exc}") from exc
            self.names_path = self.yaml_path
        if not self.names and text_names_path:
            lines = text_names_path.read_text(encoding="utf-8-sig").splitlines()
            self.names = {i: name.strip() or f"class_{i}" for i, name in enumerate(lines)}
            if not self.yaml_path:
                self.names_path = text_names_path
        if self.names_path:
            self._metadata_hash = fingerprint(self.names_path)
        self._original_names = self.names.copy()
        # A selected child image folder should not unexpectedly import other splits.
        use_yaml = self.yaml_path and (self.source.is_file() or self.yaml_path.parent == self.root)
        images: dict[Path, str] = {}
        if use_yaml:
            for split in ("train", "val", "test"):
                entries = self.yaml_data.get(split, [])
                if isinstance(entries, str):
                    entries = [entries]
                if entries is None:
                    entries = []
                if not isinstance(entries, list) or not all(isinstance(v, str) for v in entries):
                    raise ValueError(f"YAML 的 {split} 必須是路徑字串或路徑列表")
                for entry in entries:
                    target = self._resolve_entry(entry)
                    if not target:
                        self.warnings.append(f"YAML {split} 路徑不存在：{entry}")
                        continue
                    if target.is_dir():
                        candidates = _image_files(target)
                    elif target.suffix.lower() == ".txt":
                        candidates = self._read_image_list(target)
                    elif target.suffix.lower() in IMAGE_EXTENSIONS:
                        candidates = [target]
                    else:
                        candidates = []
                        self.warnings.append(f"不支援的影像來源：{target}")
                    for image in candidates:
                        images.setdefault(image, split)
        if not images:
            images = {p: self._guess_split(p) for p in _image_files(self.root)}
        if not images:
            raise ValueError("找不到圖片。支援 JPG、PNG、BMP、WEBP、TIFF；請選擇資料集根目錄或 images 資料夾。")
        label_owners: dict[Path, Path] = {}
        for image, split in sorted(images.items(), key=lambda item: (item[1], str(item[0]).lower())):
            label = self._label_for(image)
            if label in label_owners:
                raise ValueError(f"兩張圖片對應到同一份標註，請先解決同名衝突：\n{label_owners[label]}\n{image}\n→ {label}")
            label_owners[label] = image
            try:
                display = image.relative_to(self.root).as_posix()
            except ValueError:
                display = str(image)
            record = ImageRecord(image, label, split, display, label.is_file())
            if record.has_label:
                try:
                    with label.open(encoding="utf-8-sig") as lines:
                        if not any(line.strip() for line in lines):
                            record.object_count = 0
                except FileNotFoundError:
                    record.has_label = False
                except (OSError, UnicodeError) as exc:
                    record.error = str(exc)
            self.records.append(record)
        if not any(record.has_label for record in self.records):
            self.warnings.append("未找到對應的標註檔。若 labels 不在標準位置，請使用「指定圖片 / 標註資料夾」。")

    def _resolve_entry(self, entry: str) -> Path | None:
        assert self.yaml_path
        entry = entry.replace("\\", "/")
        base = self.yaml_path.parent
        declared = self.yaml_data.get("path")
        candidates = []
        if isinstance(declared, str) and declared:
            declared_path = Path(declared.replace("\\", "/"))
            if not declared_path.is_absolute():
                declared_path = base / declared_path
            candidates.append(declared_path / entry)
        candidates.extend([base / entry, self.root / entry])
        # Roboflow exports may retain ../train/images after moving the ZIP contents.
        portable = re.sub(r"^(?:\.\./)+", "", entry)
        candidates.append(base / portable)
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return None

    def _read_image_list(self, path: Path) -> list[Path]:
        images = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            entry = line.strip().replace("\\", "/")
            if not entry:
                continue
            candidates = [path.parent / entry, self.root / entry]
            resolved = self._resolve_entry(entry)
            if resolved:
                candidates.insert(0, resolved)
            target = next((p.resolve() for p in candidates if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), None)
            if target:
                images.append(target)
            else:
                self.warnings.append(f"影像清單找不到：{entry}")
        return images

    def _guess_split(self, image: Path) -> str:
        for part in reversed(image.parent.parts):
            if part.lower() in SPLITS:
                return "val" if part.lower() in {"valid", "validation"} else part.lower()
        return "未分組"

    def _label_for(self, image: Path) -> Path:
        if self.labels_dir:
            try:
                relative = image.relative_to(self.mapping_root)
            except ValueError as exc:
                raise ValueError("自訂配對時請選擇圖片資料夾，而非含外部路徑的 YAML") from exc
            return (self.labels_dir / relative).with_suffix(".txt")
        parts = list(image.parts)
        for i in range(len(parts) - 2, 0, -1):
            if parts[i].lower() == "images":
                parts[i] = "labels"
                return Path(*parts).with_suffix(".txt")
        # Common sibling folders, including a user selecting only a train folder.
        for candidate in [image.parent / "labels" / (image.stem + ".txt"),
                          self.root / "labels" / image.relative_to(self.root).with_suffix(".txt") if image.is_relative_to(self.root) else image.with_suffix(".txt")]:
            if candidate.is_file():
                return candidate
        return image.with_suffix(".txt")

    def read(self, record: ImageRecord) -> tuple[list[Annotation], list[str], str | None]:
        if not record.label.exists():
            record.has_label, record.object_count, record.error = False, 0, ""
            return [], [], None
        raw = record.label.read_bytes()
        annotations, errors = parse_labels(raw.decode("utf-8-sig"))
        record.has_label, record.object_count, record.error = True, len(annotations), "\n".join(errors)
        for annotation in annotations:
            self.names.setdefault(annotation.class_id, f"class_{annotation.class_id}")
        return annotations, errors, hashlib.sha256(raw).hexdigest()

    def save_names(self) -> None:
        if self.names == self._original_names:
            return
        path = self.names_path or self.root / "classes.txt"
        if fingerprint(path) != self._metadata_hash:
            raise ValueError(f"類別檔已被其他程式修改。請重新匯入，避免覆蓋：\n{path}")
        if path.suffix.lower() in {".yaml", ".yml"}:
            data = self.yaml_data.copy()
            data["names"] = {i: self.names.get(i, f"class_{i}") for i in range(max(self.names, default=-1) + 1)}
            data["nc"] = max(self.names, default=-1) + 1
            content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        else:
            content = "\n".join(self.names.get(i, f"class_{i}") for i in range(max(self.names, default=-1) + 1)) + "\n"
        atomic_write(path, content.encode("utf-8"), self.backup_root)
        self.names_path = path
        self._metadata_hash = fingerprint(path)
        self._original_names = self.names.copy()

    def save(self, record: ImageRecord, annotations: list[Annotation], expected_hash: str | None) -> str:
        if record.error:
            raise ValueError("此標註檔有格式錯誤，已禁止覆寫。請先修正原始檔並重新載入。")
        if fingerprint(record.label) != expected_hash:
            raise ValueError(f"標註已被其他程式修改。請重新載入，避免覆蓋：\n{record.label}")
        content = serialize_labels(annotations).encode("utf-8")
        self.save_names()
        atomic_write(record.label, content, self.backup_root)
        record.has_label, record.object_count = True, len(annotations)
        return hashlib.sha256(content).hexdigest()

    def export(self, destination: Path, progress: Callable[[int, int], None] | None = None) -> None:
        """Export a portable copy. Never replace a non-empty destination."""
        destination = destination.resolve()
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("匯出目的地必須是空資料夾，避免覆蓋既有資料")
        if any(record.image.is_relative_to(destination) or record.label.is_relative_to(destination) for record in self.records):
            raise ValueError("匯出目的地不能包含原始資料集")
        # Validate every source before copying; no silent loss of malformed lines.
        for record in self.records:
            _, errors, _ = self.read(record)
            if errors:
                raise ValueError(f"匯出已停止，請先修正：{record.label}\n" + "\n".join(errors[:8]))
        destination.mkdir(parents=True, exist_ok=True)
        split_files: dict[str, list[str]] = {}
        for index, record in enumerate(self.records):
            split = "train" if record.split == "未分組" else record.split
            # Stable suffix avoids collisions across nested directories and image types.
            key = hashlib.sha256(str(record.image).encode()).hexdigest()[:10]
            stem = f"{record.image.stem}_{key}"
            image_rel = Path("images") / split / (stem + record.image.suffix.lower())
            image_out = destination / image_rel
            image_out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(record.image, image_out)
            label_out = destination / "labels" / split / (stem + ".txt")
            label_out.parent.mkdir(parents=True, exist_ok=True)
            # Preserve missing vs explicit empty labels and original precision.
            if record.label.exists():
                shutil.copy2(record.label, label_out)
            split_files.setdefault(split, []).append("./" + image_rel.as_posix())
            if progress:
                progress(index + 1, len(self.records))
        config: dict = {}
        for split in split_files:
            config[split] = f"images/{split}"
        if "val" not in config:
            config["val"] = None
        config["names"] = {i: self.names.get(i, f"class_{i}") for i in range(max(self.names, default=0) + 1)}
        config["nc"] = len(config["names"])
        (destination / "data.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
