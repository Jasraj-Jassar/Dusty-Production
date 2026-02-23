from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog

# Allow running either:
# 1) via launcher: `DustyBot.cmd` (runs this file as a script)
# 2) as a module from Core_software: `python -m gui.gui_app` (package import works)
# 3) from this folder: `python gui_app.py` (package import fails, local import works)
try:
    from gui.preview_package import PreviewPackageWindow
except ModuleNotFoundError:
    from preview_package import PreviewPackageWindow  # type: ignore
try:
    from gui.credits_window import CreditsWindow
except ModuleNotFoundError:
    from credits_window import CreditsWindow  # type: ignore


APP_TITLE = "DustyBot"
GUI_DIR = Path(__file__).resolve().parent

def _find_core_root(start: Path) -> Path:
    """Locate the directory containing Core_software content (actions, requirements, etc.)."""
    for p in [start, *start.parents]:
        if (p / "actions").is_dir() and (p / "requirements.txt").is_file():
            return p
    # Fallback: assume the GUI folder lives inside the core root.
    return start.parent


CORE_ROOT = _find_core_root(GUI_DIR)
# Repo/distribution root: usually the parent of Core_software/ in this project.
REPO_ROOT = CORE_ROOT.parent if CORE_ROOT.name.lower() == "core_software" else CORE_ROOT

_DATA_ROOT_ENV = (os.environ.get("DUSTYBOT_DATA_ROOT") or "").strip()
DATA_ROOT = Path(_DATA_ROOT_ENV).resolve() if _DATA_ROOT_ENV else REPO_ROOT
TARGET_NAME = "JobTraveller.pdf"
ASSETS_DIR = GUI_DIR / "assets"
ACTIONS_DIR = CORE_ROOT / "actions"
# Default to P:\ (Pdrive). Override anytime via DUSTYBOT_SEARCH_ROOT.
REV_SEARCH_ROOT = os.environ.get("DUSTYBOT_SEARCH_ROOT", "P:\\")
DEFAULT_PRINT_PRINTER = "Kyocera TASKalfa 3501i"

# Add Core_software/ to sys.path so `actions.*` imports work reliably.
try:
    core_root_str = str(CORE_ROOT)
    if core_root_str not in sys.path:
        sys.path.append(core_root_str)
except Exception:
    pass

def _pick_workspace_root(data_root: Path) -> Path:
    """Use History root as the base; per-job folder is created on upload."""
    ws = (data_root / "History").resolve()
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return ws

class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x780")
        self.minsize(900, 620)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.status_var = ctk.StringVar(value="Click Browse to select a PDF Or check out History.")
        self.path_var = ctk.StringVar(value="")
        self._success_cache: tuple[list[ctk.CTkImage], int] | None = None
        self._upload_logo_image: ctk.CTkImage | None = None
        self._final_pdf_path: Path | None = None
        self.repo_root = REPO_ROOT
        self.data_root = DATA_ROOT
        self.workspace_root = _pick_workspace_root(self.data_root)
        self.target_dir = self.workspace_root / "insert-traveler"
        self.ops_root = self.workspace_root / "ops_grouped"

        self._build_ui()
        self._init_steps()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.destroy()

    def _apply_workspace(self, ws_root: Path) -> None:
        self.workspace_root = ws_root
        self.target_dir = self.workspace_root / "insert-traveler"
        self.ops_root = self.workspace_root / "ops_grouped"

    def _rotate_workspace(self) -> None:
        """Return to History root; a new job folder is created on next upload."""
        self._apply_workspace(_pick_workspace_root(self.data_root))

    def _safe_folder_name(self, name: str) -> str:
        cleaned = re.sub(r"[<>:\"/\\\\|?*]", "_", (name or "").strip()).strip().rstrip(".")
        return cleaned or "Unknown"

    def _extract_job_from_pdf(self, pdf_path: Path) -> str | None:
        try:
            from pypdf import PdfReader
        except Exception:
            return None

        try:
            reader = PdfReader(str(pdf_path))
        except Exception:
            return None

        for page in reader.pages[:5]:
            text = page.extract_text() or ""
            m = re.search(r"(?i)\bJob\s*:\s*([^\r\n]+)", text)
            if not m:
                continue
            value = (m.group(1) or "").strip()
            if value:
                return value
        return None

    def _create_job_workspace_for_pdf(self, pdf_path: Path) -> Path:
        history_root = _pick_workspace_root(self.data_root)
        job = self._safe_folder_name(self._extract_job_from_pdf(pdf_path) or "Unknown")

        # Keep the folder pattern fixed as: Job - <job> - <timestamp>
        for _ in range(10):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            ws = (history_root / self._safe_folder_name(f"Job - {job} - {stamp}")).resolve()
            if not ws.exists():
                ws.mkdir(parents=True, exist_ok=False)
                return ws
            time.sleep(1)

        # Extremely unlikely collision fallback.
        i = 1
        while True:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            ws = (history_root / self._safe_folder_name(f"Job - {job} - {stamp} ({i})")).resolve()
            if not ws.exists():
                ws.mkdir(parents=True, exist_ok=False)
                return ws
            i += 1

    def _init_steps(self) -> None:
        self._steps = [
            "Extract Parts",
            "Split by Asm",
            "Group by Ops",
            "Check Pages (grouped)",
            "Parts by Ops",
            "Combine Asms by Part",
            "Pull Latest Revs",
        ]
        self._step_state: dict[str, str] = {s: "PENDING" for s in self._steps}
        self._render_steps()

    def _run_popen(self, cmd: list[str], cwd: str, on_done, on_output=None) -> None:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                bufsize=1,
            )
        except OSError:
            on_done(127, "", "Could not start process")
            return

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _reader(pipe, target: list[str], stream: str) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    target.append(line)
                    if callable(on_output):
                        try:
                            self.after(0, on_output, line, stream)
                        except Exception:
                            pass
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        threads = []
        if proc.stdout is not None:
            t = threading.Thread(target=_reader, args=(proc.stdout, stdout_lines, "stdout"), daemon=True)
            t.start()
            threads.append(t)
        if proc.stderr is not None:
            t = threading.Thread(target=_reader, args=(proc.stderr, stderr_lines, "stderr"), daemon=True)
            t.start()
            threads.append(t)

        def poll() -> None:
            if proc.poll() is None:
                self.after(150, poll)
                return
            for t in threads:
                t.join(timeout=0.2)
            out = "".join(stdout_lines).strip()
            err = "".join(stderr_lines).strip()
            msg = (out or err or "").strip()
            on_done(proc.returncode or 0, msg, err or "")

        poll()

    def _render_steps(self) -> None:
        symbol = {
            "PENDING": "...",
            "RUN": "...",
            "OK": "\u2713",
            "FAIL": "\u2717",
        }
        lines = [f"{s} {symbol.get(self._step_state.get(s, 'PENDING'), '...')}" for s in self._steps]
        self.steps_box.configure(state="normal")
        self.steps_box.delete("1.0", "end")
        self.steps_box.insert("1.0", "\n".join(lines) + "\n")
        self.steps_box.configure(state="disabled")

    def _set_step(self, step: str, state: str) -> None:
        if step in self._step_state:
            self._step_state[step] = state
            self._render_steps()

    def _append_detail(self, message: str) -> None:
        self.detail_box.configure(state="normal")
        self.detail_box.insert("end", message.strip() + "\n")
        self.detail_box.see("end")
        self.detail_box.configure(state="disabled")

    def _resolve_ops_root_for_preview(self, selected_dir: Path) -> Path | None:
        selected_dir = selected_dir.resolve()
        if selected_dir.name.lower() == "ops_grouped" and selected_dir.is_dir():
            return selected_dir
        candidate = selected_dir / "ops_grouped"
        if candidate.is_dir():
            return candidate
        return None

    def _open_preview_for_ops_root(
        self,
        ops_root: Path,
        *,
        read_only: bool = False,
        status_message: str = "Preview opened.",
    ) -> None:
        if not ops_root.is_dir():
            self.status_var.set(f"ops_grouped not found: {ops_root}")
            return

        # VS Code-like side view: launch the Qt preview app in a separate process (smooth drag/drop).
        qt_script = GUI_DIR / "preview_package_qt.py"
        if qt_script.is_file():
            try:
                import PySide6  # noqa: F401

                cmd = [sys.executable, str(qt_script), "--ops-root", str(ops_root)]
                if read_only:
                    cmd.append("--readonly")
                subprocess.Popen(cmd, cwd=str(self.repo_root))
                self.status_var.set(status_message)
                return
            except Exception:
                # Fall back below.
                pass

        # Fallback: in-app preview window.
        if getattr(self, "_preview_win", None) and self._preview_win.winfo_exists():
            current_root = None
            current_root_attr = getattr(self._preview_win, "ops_root", None)
            if current_root_attr:
                try:
                    current_root = Path(current_root_attr).resolve()
                except Exception:
                    current_root = None
            current_read_only = bool(getattr(self._preview_win, "read_only", False))
            if current_root == ops_root.resolve() and current_read_only == read_only:
                try:
                    self._preview_win.focus()
                    self._preview_win.lift()
                    self.status_var.set(status_message)
                    return
                except Exception:
                    pass
            try:
                self._preview_win.destroy()
            except Exception:
                pass

        self._preview_win = PreviewPackageWindow(
            self,
            ops_root=ops_root,
            set_status=self.status_var.set,
            read_only=read_only,
        )
        self.status_var.set(status_message)

    def preview_package(self) -> None:
        self._open_preview_for_ops_root(self.ops_root, read_only=False, status_message="Preview opened.")

    def preview_history(self) -> None:
        history_root = self.data_root / "History"
        if not history_root.is_dir():
            self.status_var.set(f"History not found: {history_root}")
            return

        selected = filedialog.askdirectory(
            title="Select History job folder (or ops_grouped)",
            initialdir=str(history_root),
            mustexist=True,
        )
        if not selected:
            return

        selected_dir = Path(selected)
        ops_root = self._resolve_ops_root_for_preview(selected_dir)
        if not ops_root:
            self.status_var.set("Selected folder does not contain ops_grouped.")
            return

        self._open_preview_for_ops_root(
            ops_root,
            read_only=True,
            status_message=f"History preview opened: {ops_root.parent.name}",
        )

    def open_credits(self) -> None:
        if getattr(self, "_credits_win", None) and self._credits_win.winfo_exists():
            try:
                self._credits_win.focus()
                self._credits_win.lift()
            except Exception:
                pass
            self.status_var.set("Credits opened.")
            return

        try:
            self._credits_win = CreditsWindow(self)
            self.status_var.set("Credits opened.")
        except Exception:
            self.status_var.set("Failed to open credits.")

    def print_package(self) -> None:
        if not self.ops_root.is_dir():
            self.status_var.set(f"ops_grouped not found: {self.ops_root}")
            return

        print_script = ACTIONS_DIR / "print_ops_grouped.py"
        if not print_script.is_file():
            self.status_var.set("print_ops_grouped.py not found.")
            return

        try:
            only = (self.print_bucket_var.get() or "").strip()
            printer = (self.print_printer_var.get() or "").strip()
            cmd = [sys.executable, str(print_script), "--ops-root", str(self.ops_root)]
            if printer:
                cmd.extend(["--asm-printer", printer, "--drawing-printer", printer])
            if only and only != "All":
                cmd.extend(["--only", only])

            # Ensure the detail box is visible to show print output.
            self.show_ops_parts(show_detail=True, show_steps=False)
            self._append_detail("Print output:")
            self.status_var.set("Printing...")
            self.print_btn.configure(state="disabled")

            state = {"had_output": False}

            def on_output(line: str, _stream: str) -> None:
                if line.strip():
                    state["had_output"] = True
                    self._append_detail(line)

            def on_done(code: int, msg: str, _err: str) -> None:
                if not state["had_output"] and msg:
                    self._append_detail(msg)
                if code == 0:
                    self.status_var.set("Print complete.")
                else:
                    self.status_var.set("Print failed.")
                self.print_btn.configure(state="normal")

            self._run_popen(cmd, cwd=str(self.workspace_root), on_done=on_done, on_output=on_output)
        except Exception:
            self.status_var.set("Failed to start print job.")
            self.print_btn.configure(state="normal")

    def _run_finalize_package_auto(self) -> None:
        # Run post-processing packaging automatically: append inspection sheets and build final combined PDFs.
        if not self.ops_root.is_dir():
            self.status_var.set(f"ops_grouped not found: {self.ops_root}")
            return

        self._set_action_visibility(show_print=False, show_new_package=False, show_preview=False, show_open_final=False)
        self.preview_btn.configure(state="disabled")
        self.new_pkg_btn.configure(state="disabled")
        self.print_btn.configure(state="disabled")
        self.open_final_btn.configure(state="disabled")

        self.results_title.configure(text="Finalizing")
        self.results_sub.configure(text="Appending inspection sheets, creating ops summaries, and building final package")
        self.status_var.set("Finalizing package...")

        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.configure(state="disabled")
        self._layout_results(show_success=False, show_steps=False, show_detail=True, show_ops=False, show_actions=False)

        repo_root = str(self.repo_root)

        append_cmd = [
            sys.executable,
            str(ACTIONS_DIR / "append_inspection_sheets.py"),
            "--ops-root",
            str(self.ops_root),
            "--no-verify",
        ]

        def on_append_output(line: str, _stream: str) -> None:
            # Keep the detail viewer quiet; append summary after completion.
            return

        def on_append_done(code: int, msg: str, _err: str) -> None:
            if code != 0:
                self._append_detail("Append Inspection Sheets: FAILED")
                if msg:
                    self._append_detail(self.compact_log_detail(msg, limit=3000))
                self.results_title.configure(text="Finalization Failed")
                self.results_sub.configure(text="Append inspection sheets failed")
                self._layout_results(show_success=False, show_steps=False, show_detail=True, show_ops=False, show_actions=True)
                self._set_action_visibility(show_print=False, show_new_package=True, show_preview=True, show_open_final=False)
                self.preview_btn.configure(state="normal")
                self.new_pkg_btn.configure(state="normal")
                self.status_var.set("Finalization failed.")
                return

            self._append_detail("Append Inspection Sheets:")
            summary_lines = self._summarize_append_output(msg or "")
            if summary_lines:
                for line in summary_lines:
                    self._append_detail(line)
            else:
                self._append_detail("Append complete.")

            def run_build_final_package() -> None:
                build_cmd = [
                    sys.executable,
                    str(ACTIONS_DIR / "build_final_package.py"),
                    "--ops-root",
                    str(self.ops_root),
                    "--out-root",
                    str(self.workspace_root),
                ]

                def on_build_output(line: str, _stream: str) -> None:
                    # Keep the detail viewer quiet; append summary after completion.
                    return

                def on_build_done(bcode: int, bmsg: str, _berr: str) -> None:
                    if bcode != 0:
                        self._append_detail("Build Final Package: FAILED")
                        if bmsg:
                            self._append_detail(self.compact_log_detail(bmsg, limit=3500))
                        self.results_title.configure(text="Finalization Failed")
                        self.results_sub.configure(text="Final package build failed")
                        self._layout_results(show_success=False, show_steps=False, show_detail=True, show_ops=False, show_actions=True)
                        self._set_action_visibility(show_print=False, show_new_package=True, show_preview=True, show_open_final=False)
                        self.preview_btn.configure(state="normal")
                        self.new_pkg_btn.configure(state="normal")
                        self.status_var.set("Finalization failed.")
                        return

                    self._append_detail("Build Final Package:")
                    build_summary = self._summarize_build_output(bmsg or "")
                    if build_summary:
                        for line in build_summary:
                            self._append_detail(line)
                    else:
                        self._append_detail("Build complete.")

                    final_pdf = None
                    out_dir = None
                    for line in (bmsg or "").splitlines():
                        line = line.strip()
                        if line.startswith("FINAL_PDF:"):
                            final_pdf = line.split(":", 1)[1].strip()
                        elif line.startswith("OUT_DIR:"):
                            out_dir = line.split(":", 1)[1].strip()

                    self._set_action_visibility(show_print=False, show_new_package=False, show_preview=False, show_open_final=False)

                    def open_final_preview() -> None:
                        self.results_title.configure(text="Complete")
                        if out_dir:
                            self.results_sub.configure(text=f"Final package ready: {out_dir}")
                        else:
                            self.results_sub.configure(text="Final package ready")
                        self._final_pdf_path = Path(final_pdf) if final_pdf else None
                        self.show_ops_parts(show_detail=True, show_steps=False)
                        self._set_action_visibility(
                            show_print=True,
                            show_new_package=True,
                            show_preview=True,
                            show_open_final=self._final_pdf_path is not None,
                        )
                        self.preview_btn.configure(state="normal")
                        self.print_btn.configure(state="normal")
                        self.new_pkg_btn.configure(state="normal")
                        if self._final_pdf_path is not None:
                            self.open_final_btn.configure(state="normal")
                        self.status_var.set("Final package built.")

                    self.show_success_inline(on_done=open_final_preview)

                self._run_popen(build_cmd, cwd=str(self.workspace_root), on_done=on_build_done, on_output=on_build_output)

            section_cmd = [
                sys.executable,
                str(ACTIONS_DIR / "build_ops_parts_section_pages.py"),
                "--ops-root",
                str(self.ops_root),
            ]

            def on_section_output(line: str, _stream: str) -> None:
                # Keep the detail viewer quiet; append summary after completion.
                return

            def on_section_done(scode: int, smsg: str, _serr: str) -> None:
                if scode != 0:
                    self._append_detail("Build Ops Parts Summary Pages: FAILED")
                    if smsg:
                        self._append_detail(self.compact_log_detail(smsg, limit=3500))
                    self.results_title.configure(text="Finalization Failed")
                    self.results_sub.configure(text="Ops summary page generation failed")
                    self._layout_results(show_success=False, show_steps=False, show_detail=True, show_ops=False, show_actions=True)
                    self._set_action_visibility(show_print=False, show_new_package=True, show_preview=True, show_open_final=False)
                    self.preview_btn.configure(state="normal")
                    self.new_pkg_btn.configure(state="normal")
                    self.status_var.set("Finalization failed.")
                    return

                self._append_detail("Build Ops Parts Summary Pages:")
                summary_lines = self._summarize_ops_parts_pages_output(smsg or "")
                if summary_lines:
                    for line in summary_lines:
                        self._append_detail(line)
                else:
                    self._append_detail("Ops summary pages created.")

                run_build_final_package()

            self._run_popen(section_cmd, cwd=str(self.workspace_root), on_done=on_section_done, on_output=on_section_output)

        self._run_popen(append_cmd, cwd=str(self.workspace_root), on_done=on_append_done, on_output=on_append_output)

    def _layout_results(
        self,
        *,
        show_success: bool,
        show_steps: bool,
        show_detail: bool,
        show_ops: bool,
        show_actions: bool = True,
        show_note: bool = False,
    ) -> None:
        """Centralize packing order to avoid Tk `pack(after=...)` / `pack(before=...)` edge cases."""
        for w in (
            self.success_label,
            self.success_anim,
            self.steps_box,
            self.detail_box,
            self.ops_box,
            self.note_label,
            self.actions_row,
        ):
            try:
                w.pack_forget()
            except Exception:
                pass

        if show_success:
            self.success_label.pack(anchor="w")
            self.success_anim.pack(anchor="w", pady=(0, 6))
        if show_steps:
            self.steps_box.pack(fill="x")
        if show_detail:
            self.detail_box.pack(fill="x", pady=(10, 0))
        if show_ops:
            self.ops_box.pack(fill="both", expand=True, pady=(10, 0))
        if show_note:
            self.note_label.pack(anchor="w", pady=(8, 0))

        if show_actions:
            self.actions_row.pack(fill="x", pady=(12, 0))

    def _set_action_visibility(
        self,
        *,
        show_print: bool,
        show_new_package: bool = True,
        show_preview: bool = True,
        show_open_final: bool = False,
    ) -> None:
        for w in (
            self.preview_btn,
            self.print_btn,
            self.print_printer_menu,
            self.print_bucket_menu,
            self.open_final_btn,
            self.new_pkg_btn,
        ):
            try:
                w.pack_forget()
            except Exception:
                pass

        if show_print:
            self._refresh_printer_choices()
            self.print_printer_menu.pack(side="left", padx=(10, 0))
            self.print_bucket_menu.pack(side="left", padx=(10, 0))
            self.print_btn.pack(side="left", padx=(10, 0))
            if show_open_final:
                self.open_final_btn.pack(side="left", padx=(10, 0))
        if show_preview:
            self.preview_btn.pack(side="left", padx=(10, 0))
        if show_new_package:
            self.new_pkg_btn.pack(side="left", padx=(10, 0))

    def _list_printer_choices(self) -> list[str]:
        names: list[str] = []
        try:
            r = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-Printer | Select-Object -ExpandProperty Name",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if r.returncode == 0 and (r.stdout or "").strip():
                for raw in (r.stdout or "").splitlines():
                    name = raw.strip()
                    if name and name not in names:
                        names.append(name)
        except Exception:
            pass

        ordered: list[str] = [DEFAULT_PRINT_PRINTER]
        for n in sorted(names, key=str.casefold):
            if n not in ordered:
                ordered.append(n)
        return ordered

    def _refresh_printer_choices(self) -> None:
        printers = self._list_printer_choices()
        current = (self.print_printer_var.get() or "").strip()
        self.print_printer_menu.configure(values=printers)
        if current and current in printers:
            self.print_printer_var.set(current)
            return
        if DEFAULT_PRINT_PRINTER in printers:
            self.print_printer_var.set(DEFAULT_PRINT_PRINTER)
            return
        if printers:
            self.print_printer_var.set(printers[0])

    def _set_upload_history_visibility(self, show: bool) -> None:
        try:
            self.preview_history_upload_btn.pack_forget()
        except Exception:
            pass
        try:
            self.credits_upload_btn.pack_forget()
        except Exception:
            pass

        if not show:
            return

        try:
            self.preview_history_upload_btn.pack(side="left", padx=(10, 0), before=self.upload_path_label)
            self.credits_upload_btn.pack(side="left", padx=(10, 0), before=self.upload_path_label)
        except Exception:
            # Fallback if upload_path_label is not ready yet.
            self.preview_history_upload_btn.pack(side="left", padx=(10, 0))
            self.credits_upload_btn.pack(side="left", padx=(10, 0))

    def _show_detail_box(self) -> None:
        self._layout_results(show_success=False, show_steps=True, show_detail=True, show_ops=False)

    def _resolve_upload_logo_path(self) -> Path:
        raw = (os.environ.get("DUSTYBOT_LOGO_PATH") or "").strip()
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
        return ASSETS_DIR / "logo.png"

    def _render_upload_logo(self) -> None:
        logo_path = self._resolve_upload_logo_path()
        fallback_text = "DustyBot"

        if not logo_path.is_file():
            self.upload_logo_label.configure(text=fallback_text, image=None)
            return

        try:
            from PIL import Image  # type: ignore
        except Exception:
            self.upload_logo_label.configure(text=fallback_text, image=None)
            return

        try:
            img = Image.open(logo_path)
            max_w = 560
            max_h = 230
            ratio = min(max_w / max(1, img.width), max_h / max(1, img.height), 1.0)
            new_w = max(1, int(img.width * ratio))
            new_h = max(1, int(img.height * ratio))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            self._upload_logo_image = ctk.CTkImage(light_image=img, dark_image=img, size=(new_w, new_h))
            self.upload_logo_label.configure(text="", image=self._upload_logo_image)
        except Exception:
            self.upload_logo_label.configure(text=fallback_text, image=None)

    def show_ops_parts(self, *, show_detail: bool = False, show_steps: bool = True) -> None:
        ops_path = self.ops_root / "ops_parts.txt"
        if ops_path.exists():
            text = ops_path.read_text(encoding="utf-8", errors="replace").rstrip() + "\n"
        else:
            text = "ops_parts.txt not found.\n"

        # If we're rendering output while still on the "Processing" header, update it.
        try:
            if self.results_frame.winfo_manager() and self.results_title.cget("text") == "Processing":
                self.results_title.configure(text="Output")
                self.results_sub.configure(text="ops_grouped package preview")
        except Exception:
            pass

        self._layout_results(show_success=False, show_steps=show_steps, show_detail=show_detail, show_ops=True, show_actions=True)
        self.ops_box.configure(state="normal")
        self.ops_box.delete("1.0", "end")
        self.ops_box.insert("1.0", text)
        self.ops_box.configure(state="disabled")

    def show_success_inline(self, on_done=None) -> None:
        self.success_label.configure(text=f"Success")
        frames, delay_ms = self._load_success_frames()
        if not frames:
            if callable(on_done):
                on_done()
            return

        self.results_title.configure(text="Complete")
        self.results_sub.configure(text="All steps succeeded")
        self._layout_results(show_success=True, show_steps=True, show_detail=False, show_ops=False, show_actions=True)
        self._play_success_frames(frames, delay_ms, on_done=on_done)

    def _play_success_frames(self, frames: list[object], delay_ms: int, on_done=None) -> None:
        self.success_anim._frames = frames  # type: ignore[attr-defined]
        self._success_anim_index = 0

        def step() -> None:
            if not self.success_anim.winfo_exists():
                return
            idx = getattr(self, "_success_anim_index", 0)
            if idx >= len(frames):
                if callable(on_done):
                    self.after(150, on_done)
                return
            self.success_anim.configure(image=frames[idx])
            idx += 1
            setattr(self, "_success_anim_index", idx)
            if idx < len(frames):
                self.after(delay_ms, step)
            else:
                if callable(on_done):
                    self.after(max(150, delay_ms), on_done)

        step()

    def _build_ui(self) -> None:
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=24, pady=24)

        self.upload_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.upload_frame.pack(fill="both", expand=True)

        self.results_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.results_frame.pack(fill="both", expand=True)
        self.results_frame.pack_forget()

        title = ctk.CTkLabel(
            self.upload_frame,
            text="Drawing Packages made easy",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title.pack(anchor="w")

        self.logo_card = ctk.CTkFrame(self.upload_frame, corner_radius=14)
        self.logo_card.pack(fill="both", expand=True, pady=(16, 0))
        self.upload_logo_label = ctk.CTkLabel(
            self.logo_card,
            text="DustyBot",
            font=ctk.CTkFont(size=36, weight="bold"),
        )
        self.upload_logo_label.pack(expand=True, padx=24, pady=24)
        self._render_upload_logo()
        actions = ctk.CTkFrame(self.upload_frame, fg_color="transparent")
        actions.pack(fill="x", pady=(16, 6))

        self.browse_btn = ctk.CTkButton(actions, text="Browse PDF", command=self.on_browse)
        self.browse_btn.pack(side="left")

        self.preview_history_upload_btn = ctk.CTkButton(actions, text="History", command=self.preview_history)
        self.credits_upload_btn = ctk.CTkButton(actions, text="Credits", command=self.open_credits)

        self.upload_path_label = ctk.CTkLabel(actions, textvariable=self.path_var, text_color="gray70")
        self.upload_path_label.pack(side="left", padx=12, fill="x", expand=True)
        self._set_upload_history_visibility(True)

        status_label = ctk.CTkLabel(self.upload_frame, textvariable=self.status_var, text_color="gray70")
        status_label.pack(anchor="w", pady=(6, 0))

        # Results view
        self.results_title = ctk.CTkLabel(
            self.results_frame,
            text="Processing",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.results_title.pack(anchor="w")

        self.results_sub = ctk.CTkLabel(
            self.results_frame,
            text="Status updates and output package",
            text_color="gray70",
        )
        self.results_sub.pack(anchor="w", pady=(4, 12))

        self.success_label = ctk.CTkLabel(
            self.results_frame,
            text="",
            font=ctk.CTkFont(size=44, weight="bold"),
            text_color="#34d399",
        )
        self.success_label.pack(anchor="w")
        self.success_label.pack_forget()

        self.success_anim = ctk.CTkLabel(self.results_frame, text="")
        self.success_anim.pack_forget()

        self.steps_box = ctk.CTkTextbox(self.results_frame, height=140, wrap="none")
        self.steps_box.configure(state="disabled")

        self.detail_box = ctk.CTkTextbox(self.results_frame, height=120, wrap="word")
        self.detail_box.configure(state="disabled")

        self.ops_box = ctk.CTkTextbox(self.results_frame, wrap="word")
        self.ops_box.configure(state="disabled")

        self.note_label = ctk.CTkLabel(self.results_frame, text="", text_color="gray70")
        self.note_label.pack_forget()

        self.actions_row = ctk.CTkFrame(self.results_frame, fg_color="transparent")

        self.preview_btn = ctk.CTkButton(self.actions_row, text="Preview Package", command=self.preview_package)

        self.print_btn = ctk.CTkButton(self.actions_row, text="Print", command=self.print_package)
        self.print_btn.configure(state="disabled")

        self.open_final_btn = ctk.CTkButton(self.actions_row, text="Open Final PDF", command=self.open_final_pdf)
        self.open_final_btn.configure(state="disabled")

        self.print_bucket_var = ctk.StringVar(value="All")
        self.print_bucket_menu = ctk.CTkOptionMenu(
            self.actions_row,
            values=["All", "Assembly", "Machining", "Welding"],
            variable=self.print_bucket_var,
            width=140,
        )
        self.print_printer_var = ctk.StringVar(value=DEFAULT_PRINT_PRINTER)
        self.print_printer_menu = ctk.CTkOptionMenu(
            self.actions_row,
            values=[DEFAULT_PRINT_PRINTER],
            variable=self.print_printer_var,
            width=260,
        )

        self.new_pkg_btn = ctk.CTkButton(self.actions_row, text="New Job", command=self.run_new_package)

        self._set_action_visibility(show_print=False, show_open_final=False)

        self._layout_results(show_success=False, show_steps=True, show_detail=False, show_ops=False)

    def on_browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Job Traveller PDF",
            filetypes=[("PDF files", "*.pdf")],
        )
        if path:
            self.handle_pdf(path)

    def handle_pdf(self, path: str) -> None:
        source = Path(path)
        if not source.is_file() or source.suffix.lower() != ".pdf":
            self.status_var.set("Please select a valid PDF file.")
            return

        try:
            ws_root = self._create_job_workspace_for_pdf(source)
        except Exception:
            self.status_var.set("Failed to create job folder.")
            return

        self._apply_workspace(ws_root)
        self.target_dir.mkdir(parents=True, exist_ok=True)
        destination = self.target_dir / TARGET_NAME
        try:
            shutil.copy2(source, destination)
        except OSError:
            self.status_var.set("Failed to copy file.")
            return

        self.path_var.set(str(destination))
        self.status_var.set(f"Uploaded. Processing in {self.workspace_root.name}...")
        self.lock_upload_ui()
        self.show_processing_view()
        self.after(250, self.run_extract_and_show)

    def show_processing_view(self) -> None:
        self.upload_frame.pack_forget()
        self.results_frame.pack(fill="both", expand=True)
        self.results_title.configure(text="Processing")
        self.results_sub.configure(text="Status updates and output package")
        # Hide buttons while processing to avoid accidental clicks mid-run.
        self._layout_results(show_success=False, show_steps=True, show_detail=False, show_ops=False, show_actions=False)
        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.configure(state="disabled")
        self.ops_box.configure(state="normal")
        self.ops_box.delete("1.0", "end")
        self.ops_box.configure(state="disabled")
        self._init_steps()

    def lock_upload_ui(self) -> None:
        self.browse_btn.configure(state="disabled")
        self._set_upload_history_visibility(False)

    def run_extract_and_show(self) -> None:
        self._set_step("Extract Parts", "RUN")
        self.update_idletasks()

        command = [sys.executable, str(ACTIONS_DIR / "extract_parts.py"), str(self.target_dir)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=str(self.workspace_root))
        except OSError:
            self._set_step("Extract Parts", "FAIL")
            self._show_detail_box()
            self._append_detail("Extract Parts failed: could not start extract_parts.py")
            self.show_ops_parts(show_detail=True)
            self.new_pkg_btn.configure(state="normal")
            return

        parts_path = self.target_dir / "parts.txt"
        if result.returncode == 0 and parts_path.exists():
            self._set_step("Extract Parts", "OK")
            self.after(150, self.run_split_check_auto)
            return

        self._set_step("Extract Parts", "FAIL")
        self._show_detail_box()
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        msg = "\n".join([s for s in [out, err] if s]) or "parts.txt not generated."
        self._append_detail(self.compact_log_detail(msg, limit=2000))
        self.show_ops_parts(show_detail=True)
        self.new_pkg_btn.configure(state="normal")

    def run_split_check_auto(self) -> None:
        self.new_pkg_btn.configure(state="disabled")
        state = {"all_ok": True}
        repo_root = str(self.repo_root)
        self._set_step("Split by Asm", "RUN")
        self.update_idletasks()
        split_cmd = [sys.executable, str(ACTIONS_DIR / "split_by_asm.py")]
        try:
            split_result = subprocess.run(split_cmd, capture_output=True, text=True, check=False, cwd=str(self.workspace_root))
        except OSError:
            self._set_step("Split by Asm", "FAIL")
            self._show_detail_box()
            self._append_detail("Split by Asm failed: could not start split_by_asm.py")
            self.new_pkg_btn.configure(state="normal")
            self.show_ops_parts(show_detail=True)
            return

        split_out = (split_result.stdout or split_result.stderr or "").strip()
        if split_result.returncode == 0:
            self._set_step("Split by Asm", "OK")
        else:
            self._set_step("Split by Asm", "FAIL")
            self._show_detail_box()
            if split_out:
                self._append_detail(self.compact_log_detail(split_out, limit=2000))
            self.new_pkg_btn.configure(state="normal")
            self.show_ops_parts(show_detail=True)
            return

        self._set_step("Group by Ops", "RUN")
        self.update_idletasks()
        group_cmd = [
            sys.executable,
            str(ACTIONS_DIR / "group_by_operation.py"),
            "--move",
            "--clean",
        ]
        try:
            group_result = subprocess.run(group_cmd, capture_output=True, text=True, check=False, cwd=str(self.workspace_root))
        except OSError:
            self._set_step("Group by Ops", "FAIL")
            self._show_detail_box()
            self._append_detail("Group by Ops failed: could not start group_by_operation.py")
            self.new_pkg_btn.configure(state="normal")
            self.show_ops_parts(show_detail=True)
            return

        group_out = (group_result.stdout or group_result.stderr or "").strip()
        if group_result.returncode == 0:
            self._set_step("Group by Ops", "OK")
        else:
            self._set_step("Group by Ops", "FAIL")
            self._show_detail_box()
            if group_out:
                self._append_detail(self.compact_log_detail(group_out, limit=2000))
            self.new_pkg_btn.configure(state="normal")
            self.show_ops_parts(show_detail=True)
            return

        self._set_step("Check Pages (grouped)", "RUN")
        self.update_idletasks()
        check_cmd = [
            sys.executable,
            str(ACTIONS_DIR / "split_check.py"),
            "--split",
            str(self.ops_root),
            "--recursive",
        ]
        try:
            check_result = subprocess.run(check_cmd, capture_output=True, text=True, check=False, cwd=str(self.workspace_root))
        except OSError:
            state["all_ok"] = False
            self._set_step("Check Pages (grouped)", "FAIL")
            self._show_detail_box()
            self._append_detail("Check Pages failed: could not start split_check.py")
            self.new_pkg_btn.configure(state="normal")
            self.show_ops_parts(show_detail=True)
            return

        check_out = (check_result.stdout or check_result.stderr or "").strip()
        if check_result.returncode == 0:
            self._set_step("Check Pages (grouped)", "OK")
        else:
            state["all_ok"] = False
            self._set_step("Check Pages (grouped)", "FAIL")
            self._show_detail_box()
            if check_out:
                self._append_detail(self.compact_log_detail(check_out, limit=2000))

        self._set_step("Parts by Ops", "RUN")
        self.update_idletasks()
        parts_cmd = [sys.executable, str(ACTIONS_DIR / "split_parts_by_operation.py")]
        try:
            parts_result = subprocess.run(parts_cmd, capture_output=True, text=True, check=False, cwd=str(self.workspace_root))
        except OSError:
            state["all_ok"] = False
            self._set_step("Parts by Ops", "FAIL")
            self._show_detail_box()
            self._append_detail("Parts by Ops failed: could not start split_parts_by_operation.py")
            self.new_pkg_btn.configure(state="normal")
            self.show_ops_parts(show_detail=True)
            return

        parts_out = (parts_result.stdout or parts_result.stderr or "").strip()
        if parts_result.returncode == 0:
            self._set_step("Parts by Ops", "OK")
        else:
            state["all_ok"] = False
            self._set_step("Parts by Ops", "FAIL")
            self._show_detail_box()
            if parts_out:
                self._append_detail(self.compact_log_detail(parts_out, limit=2000))

        def run_pull() -> None:
            self._set_step("Pull Latest Revs", "RUN")
            self.update_idletasks()
            self.status_var.set(f"Pulling latest revs from {REV_SEARCH_ROOT} (may take a while)...")

            pull_cmd = [
                sys.executable,
                str(ACTIONS_DIR / "pull_latest_revs.py"),
                "--ops-root",
                str(self.ops_root),
                "--search-root",
                str(REV_SEARCH_ROOT),
                "--ext",
                ".pdf",
                "--append-missing-to-ops-parts",
            ]

            def finish() -> None:
                if state["all_ok"]:
                    self._run_finalize_package_auto()
                    return
                else:
                    self.results_title.configure(text="Failed")
                    self.results_sub.configure(text="See details below")
                    self._show_detail_box()
                    self.show_ops_parts(show_detail=True)
                self.new_pkg_btn.configure(state="normal")

            def on_pull_done(code: int, msg: str, _err: str) -> None:
                if code == 0:
                    self._set_step("Pull Latest Revs", "OK")
                else:
                    state["all_ok"] = False
                    self._set_step("Pull Latest Revs", "FAIL")
                    self._show_detail_box()
                    if msg:
                        self._append_detail(self.compact_log_detail(msg, limit=2000))
                self.status_var.set("Done.")
                finish()

            self._run_popen(pull_cmd, cwd=str(self.workspace_root), on_done=on_pull_done)

        self._set_step("Combine Asms by Part", "RUN")
        self.update_idletasks()
        self.status_var.set("Combining duplicate Asm PDFs by Part...")

        combine_cmd = [
            sys.executable,
            str(ACTIONS_DIR / "combine_asms_by_part.py"),
            "--ops-root",
            str(self.ops_root),
        ]

        def on_combine_done(code: int, msg: str, _err: str) -> None:
            if code == 0:
                self._set_step("Combine Asms by Part", "OK")
            else:
                state["all_ok"] = False
                self._set_step("Combine Asms by Part", "FAIL")
                self._show_detail_box()
                if msg:
                    self._append_detail(self.compact_log_detail(msg, limit=2000))
            run_pull()

        self._run_popen(combine_cmd, cwd=str(self.workspace_root), on_done=on_combine_done)

    def run_new_package(self) -> None:
        self.new_pkg_btn.configure(state="disabled")
        self._rotate_workspace()
        self.reset_upload_ui()

    def reset_upload_ui(self) -> None:
        self.results_frame.pack_forget()
        self.upload_frame.pack(fill="both", expand=True)
        self.path_var.set("")
        self.status_var.set("Click Browse to select a PDF. Or check out History.")
        self.browse_btn.configure(state="normal")
        self._set_upload_history_visibility(True)
        self.print_btn.configure(state="disabled")
        self.open_final_btn.configure(state="disabled")
        self._final_pdf_path = None
        self._set_action_visibility(show_print=False, show_open_final=False)
        self.success_label.pack_forget()
        self.success_anim.pack_forget()
        self.detail_box.pack_forget()
        self.ops_box.pack_forget()
        self._init_steps()
        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.configure(state="disabled")
        self.ops_box.configure(state="normal")
        self.ops_box.delete("1.0", "end")
        self.ops_box.configure(state="disabled")

    def compact_log_detail(self, message: str, limit: int = 500) -> str:
        # Keep logs readable in a single line.
        s = " ".join((message or "").split())
        if len(s) <= limit:
            return s
        return s[:limit] + "..."

    def _summarize_append_output(self, message: str) -> list[str]:
        lines: list[str] = []
        for raw in (message or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("Summary:"):
                lines.append(line)
                continue
            if re.match(r"^APPENDED:\s*\d+\b", line):
                lines.append(line)
                continue
            if re.match(r"^SKIP_ALREADY:\s*\d+\b", line):
                lines.append(line)
                continue
            if re.match(r"^DRY_RUN:\s*\d+\b", line):
                lines.append(line)
                continue
            if re.match(r"^ERROR:\s*\d+\b", line):
                lines.append(line)
                continue
        return lines

    def _summarize_build_output(self, message: str) -> list[str]:
        lines: list[str] = []
        for raw in (message or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("FINAL_PDF:"):
                lines.append(line)
                continue
            if line.startswith("OUT_DIR:"):
                lines.append(line)
                continue
        return lines

    def _summarize_ops_parts_pages_output(self, message: str) -> list[str]:
        lines: list[str] = []
        for raw in (message or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("Wrote:"):
                lines.append(line)
                continue
            if line.startswith("Generated section cover PDFs:"):
                lines.append(line)
                continue
        return lines

    def open_final_pdf(self) -> None:
        if not self._final_pdf_path:
            self.status_var.set("Final PDF not available.")
            return
        try:
            subprocess.Popen(
                [sys.executable, str(GUI_DIR / "preview_pdf_qt.py"), "--pdf", str(self._final_pdf_path)],
                cwd=str(self.workspace_root),
            )
        except Exception:
            self.status_var.set("Failed to open final PDF.")

    def _load_success_frames(self) -> tuple[list[ctk.CTkImage], int]:
        # Return (frames, delay_ms). Frames are CTkImage objects for HiDPI-safe rendering.
        if hasattr(self, "_success_cache"):
            cache = getattr(self, "_success_cache")
            if cache and isinstance(cache, tuple) and len(cache) == 2:
                return cache  # type: ignore[return-value]

        try:
            from PIL import Image  # type: ignore
        except Exception:
            return [], 0

        size = 180

        json_path = ASSETS_DIR / "success.json"
        if not json_path.is_file():
            return [], 0

        try:
            import rlottie_python as rl
        except Exception:
            return [], 0

        try:
            anim = rl.LottieAnimation.from_file(str(json_path))
            total = int(anim.lottie_animation_get_totalframe())
            fps = float(anim.lottie_animation_get_framerate() or 30.0)
        except Exception:
            return [], 0

        delay_ms = max(10, int(1000 / max(1.0, fps)))
        frames: list[ctk.CTkImage] = []
        for i in range(max(1, total)):
            try:
                fr = anim.render_pillow_frame(i).resize((size, size), Image.Resampling.LANCZOS)
                frames.append(ctk.CTkImage(light_image=fr, dark_image=fr, size=(size, size)))
            except Exception:
                break

        if not frames:
            return [], 0

        setattr(self, "_success_cache", (frames, delay_ms))
        return frames, delay_ms


def main() -> int:
    app = App()

    # Splash screen (optional)
    try:
        try:
            from gui.splash import SplashScreen, is_splash_enabled, logo_path_from_env
        except ModuleNotFoundError:
            from splash import SplashScreen, is_splash_enabled, logo_path_from_env  # type: ignore

        if is_splash_enabled():
            # Hide the main window until splash completes.
            try:
                app.withdraw()
            except Exception:
                pass

            def _on_splash_done() -> None:
                try:
                    app.deiconify()
                    app.lift()
                    app.focus_force()
                except Exception:
                    pass

            logo = logo_path_from_env()
            splash = SplashScreen(app, _on_splash_done, logo_path=logo)
            splash.show()
    except Exception as e:
        # If splash can't load (missing deps, etc.), just run normally.
        try:
            print(f"Splash skipped: {e}")
        except Exception:
            pass
        try:
            app.deiconify()
        except Exception:
            pass

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
