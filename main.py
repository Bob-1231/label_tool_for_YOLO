"""Run with an environment containing Pillow and PyYAML."""
from __future__ import annotations
import argparse
import ctypes
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="YOLO Desk — local YOLO annotation editor")
    parser.add_argument("dataset", nargs="?", type=Path, help="Dataset folder or data.yaml")
    args = parser.parse_args()
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    try:
        from yolo_desk.app import YoloDesk
    except ImportError as exc:
        raise SystemExit(
            f"Missing dependency: {exc}\n"
            'Use your Anaconda Python: python -m pip install -r requirements.txt'
        ) from exc
    app = YoloDesk()
    if args.dataset:
        app.after(150, lambda: app.open_dataset(args.dataset))
    app.mainloop()


if __name__ == "__main__":
    main()
