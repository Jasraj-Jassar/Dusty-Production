"""
Splash screen for DustyBot (tkinter).

This is intentionally lightweight:
- No dependencies on project-specific theme/config modules
- If Pillow or the logo file is missing, it falls back to a simple text logo
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path

try:
    from PIL import Image, ImageTk  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]


DEFAULT_SPLASH_WIDTH = 520
DEFAULT_SPLASH_HEIGHT = 320
DEFAULT_SPLASH_DURATION_MS = 1100

COLORS = {
    "bg": "#0b1220",
    "fg": "#e5e7eb",
    "muted": "#94a3b8",
    "accent": "#34d399",
    "progress_bg": "#1f2937",
}


def _default_logo_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    # This repo stores GUI assets under gui/assets/.
    return repo_root / "gui" / "assets" / "logo.png"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


class SplashScreen:
    """Minimal splash screen with logo + progress animation."""

    def __init__(
        self,
        parent: tk.Tk,
        on_complete,
        *,
        width: int = DEFAULT_SPLASH_WIDTH,
        height: int = DEFAULT_SPLASH_HEIGHT,
        duration_ms: int = DEFAULT_SPLASH_DURATION_MS,
        logo_path: str | Path | None = None,
        title: str = "DustyBot",
    ) -> None:
        self.parent = parent
        self.on_complete = on_complete
        self.width = int(width)
        self.height = int(height)
        self.duration_ms = int(duration_ms)
        self.title = title

        self.logo_path = Path(logo_path) if logo_path else _default_logo_path()
        self.window: tk.Toplevel | None = None
        self.progress = 0
        self._logo_image = None  # keep ref to prevent GC

        self._progress_fill: tk.Frame | None = None

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.overrideredirect(True)
        self.window.configure(bg=COLORS["bg"])

        # Center
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        x = (screen_width - self.width) // 2
        y = (screen_height - self.height) // 2
        self.window.geometry(f"{self.width}x{self.height}+{x}+{y}")

        # Topmost
        try:
            self.window.attributes("-topmost", True)
        except Exception:
            pass

        container = tk.Frame(self.window, bg=COLORS["bg"])
        container.pack(fill=tk.BOTH, expand=True, padx=30, pady=30)

        logo_frame = tk.Frame(container, bg=COLORS["bg"])
        logo_frame.pack(expand=True)

        if not self._load_logo(logo_frame):
            self._create_text_logo(logo_frame)

        # Progress
        progress_frame = tk.Frame(container, bg=COLORS["bg"])
        progress_frame.pack(fill=tk.X, pady=(18, 10))

        progress_bg = tk.Frame(progress_frame, bg=COLORS["progress_bg"], height=6)
        progress_bg.pack(fill=tk.X)

        self._progress_fill = tk.Frame(progress_bg, bg=COLORS["accent"], height=6, width=0)
        self._progress_fill.place(x=0, y=0, relheight=1)

        hint = tk.Label(
            container,
            text="Loading...",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 10),
        )
        hint.pack(side=tk.BOTTOM, anchor="w")

        self._animate()

    def _load_logo(self, parent: tk.Widget) -> bool:
        if Image is None or ImageTk is None:
            return False
        try:
            if not self.logo_path.is_file():
                return False

            img = Image.open(self.logo_path)
            max_w = 360
            max_h = 180
            ratio = min(max_w / max(1, img.width), max_h / max(1, img.height))
            ratio = min(1.0, ratio)  # never upscale
            new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

            self._logo_image = ImageTk.PhotoImage(img)
            tk.Label(parent, image=self._logo_image, bg=COLORS["bg"]).pack(pady=10)
            return True
        except Exception:
            return False

    def _create_text_logo(self, parent: tk.Widget) -> None:
        tk.Label(
            parent,
            text=self.title,
            bg=COLORS["bg"],
            fg=COLORS["fg"],
            font=("Segoe UI", 40, "bold"),
        ).pack(pady=(18, 6))

        tk.Label(
            parent,
            text="Traveler Packaging",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Segoe UI", 12),
        ).pack()

    def _animate(self) -> None:
        steps = 50
        interval = max(10, self.duration_ms // steps)

        def tick() -> None:
            if self.window is None:
                return
            if self.progress >= steps:
                self.close()
                return

            self.progress += 1
            fill = self._progress_fill
            if fill is not None:
                usable_w = max(1, self.width - 60)
                fill.configure(width=int(usable_w * (self.progress / steps)))
            self.window.after(interval, tick)

        tick()

    def close(self) -> None:
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:
            pass
        self.window = None
        self.on_complete()


def is_splash_enabled() -> bool:
    return _bool_env("DUSTYBOT_SPLASH", True)


def logo_path_from_env() -> Path | None:
    raw = (os.environ.get("DUSTYBOT_LOGO_PATH") or "").strip()
    if not raw:
        return None
    return Path(raw)
