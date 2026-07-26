from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


FONT_FAMILY = "Inter"
FALLBACK_FONT_FAMILY = "Segoe UI"
_FR_PRIVATE = 0x10
_LOADED_FONT_PATHS: list[Path] = []


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def load_bundled_fonts() -> str:
    if os.name != "nt":
        return FALLBACK_FONT_FAMILY
    loaded = 0
    for filename in ("Inter-Regular.ttf", "Inter-Bold.ttf"):
        path = resource_path("assets", "fonts", filename)
        if not path.is_file():
            continue
        if ctypes.windll.gdi32.AddFontResourceExW(str(path), _FR_PRIVATE, 0):
            loaded += 1
            _LOADED_FONT_PATHS.append(path)
    return FONT_FAMILY if loaded == 2 else FALLBACK_FONT_FAMILY
