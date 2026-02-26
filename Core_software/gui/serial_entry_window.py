from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import messagebox
from typing import Callable, Optional

import customtkinter as ctk


SERIAL_SUFFIX_PATTERN = re.compile(r"^.*\d+$")
PROGRESS_PATTERN = re.compile(r"^\[(\d+)/(\d+)\]")


class SerialEntryWindow(ctk.CTkToplevel):
    """Dusty-styled UI wrapper around serial_entry_automation.py."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        automation_script: Path,
        settings_file: Path,
        set_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(master)
        self.title("DustyBot | Serial Entry")
        self.geometry("980x660")
        self.minsize(860, 560)
        self.transient(master)

        self.automation_script = automation_script
        self.settings_file = settings_file
        self.set_status = set_status or (lambda _message: None)

        self.event_queue: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self.process: Optional[subprocess.Popen[str]] = None
        self.reader_thread: Optional[threading.Thread] = None

        self.serial_var = ctk.StringVar(value="")
        self.total_var = ctk.StringVar(value="10")
        self.url_var = ctk.StringVar(value="")
        self.status_var = ctk.StringVar(value="Ready")
        self.progress_var = ctk.StringVar(value="0 / 0")

        self._build_ui()
        self._load_settings()
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        title = ctk.CTkLabel(
            container,
            text="Serial Entry Automation",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        title.pack(anchor="w")

        subtitle = ctk.CTkLabel(
            container,
            text="Create sequential Epicor serials from one starting value.",
            text_color="gray70",
        )
        subtitle.pack(anchor="w", pady=(2, 14))

        form_card = ctk.CTkFrame(container)
        form_card.pack(fill="x")
        form_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form_card, text="Starting Serial").grid(
            row=0, column=0, sticky="w", padx=14, pady=(12, 6)
        )
        self.serial_entry = ctk.CTkEntry(form_card, textvariable=self.serial_var)
        self.serial_entry.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=(12, 6))

        ctk.CTkLabel(form_card, text="Quantity").grid(row=1, column=0, sticky="w", padx=14, pady=6)
        self.total_entry = ctk.CTkEntry(form_card, textvariable=self.total_var, width=120)
        self.total_entry.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=6)

        ctk.CTkLabel(form_card, text="Epicor URL").grid(row=2, column=0, sticky="w", padx=14, pady=(6, 12))
        self.url_entry = ctk.CTkEntry(form_card, textvariable=self.url_var)
        self.url_entry.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=(6, 12))

        actions = ctk.CTkFrame(container, fg_color="transparent")
        actions.pack(fill="x", pady=(12, 8))

        self.start_button = ctk.CTkButton(actions, text="Start", command=self._start_run)
        self.start_button.pack(side="left")

        self.stop_button = ctk.CTkButton(actions, text="Stop", command=self._stop_run, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))

        self.clear_button = ctk.CTkButton(actions, text="Clear Log", command=self._clear_log)
        self.clear_button.pack(side="left", padx=(10, 0))

        status_row = ctk.CTkFrame(container, fg_color="transparent")
        status_row.pack(fill="x", pady=(0, 8))
        status_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(status_row, text="Status:").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(status_row, textvariable=self.status_var, text_color="gray70").grid(
            row=0, column=1, sticky="w"
        )
        ctk.CTkLabel(status_row, text="Progress:").grid(row=0, column=2, sticky="e", padx=(10, 0))
        ctk.CTkLabel(status_row, textvariable=self.progress_var, text_color="gray70").grid(
            row=0, column=3, sticky="e"
        )

        self.log_text = ctk.CTkTextbox(container, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.insert("1.0", "Ready.\n")
        self.log_text.configure(state="disabled")

    def _load_settings(self) -> None:
        if not self.settings_file.exists():
            return
        try:
            payload = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except Exception:
            return

        self.serial_var.set(str(payload.get("serial", "")))
        self.total_var.set(str(payload.get("total", "10")))
        self.url_var.set(str(payload.get("url", "")))

    def _save_settings(self, serial: str, total: int, url: str) -> None:
        payload = {"serial": serial, "total": total, "url": url}
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _validate_inputs(self) -> Optional[tuple[str, int, str]]:
        serial = self.serial_var.get().strip()
        total_raw = self.total_var.get().strip()
        url = self.url_var.get().strip()

        if not serial:
            messagebox.showerror("Missing Value", "Starting Serial is required.", parent=self)
            return None
        if not SERIAL_SUFFIX_PATTERN.match(serial):
            messagebox.showerror(
                "Invalid Serial",
                "Starting Serial must end with digits, for example: AFPA-100-030",
                parent=self,
            )
            return None

        try:
            total = int(total_raw)
        except ValueError:
            messagebox.showerror("Invalid Quantity", "Quantity must be a whole number.", parent=self)
            return None

        if total <= 0:
            messagebox.showerror("Invalid Quantity", "Quantity must be greater than zero.", parent=self)
            return None

        if not url:
            messagebox.showerror("Missing Value", "Epicor URL is required.", parent=self)
            return None

        return serial, total, url

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        match = PROGRESS_PATTERN.match(message.strip())
        if match:
            self.progress_var.set(f"{match.group(1)} / {match.group(2)}")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress_var.set("0 / 0")

    def _set_running(self, running: bool) -> None:
        field_state = "disabled" if running else "normal"
        self.serial_entry.configure(state=field_state)
        self.total_entry.configure(state=field_state)
        self.url_entry.configure(state=field_state)
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _start_run(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if not self.automation_script.is_file():
            messagebox.showerror("Missing File", f"File not found:\n{self.automation_script}", parent=self)
            return

        validated = self._validate_inputs()
        if validated is None:
            return
        serial, total, url = validated
        self._save_settings(serial, total, url)

        command = [
            sys.executable,
            "-u",
            str(self.automation_script),
            "--serial",
            serial,
            "--count",
            str(total),
            "--url",
            url,
        ]

        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(self.automation_script.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("Launch Failed", f"Could not start automation:\n{exc}", parent=self)
            return

        self._set_running(True)
        self.status_var.set("Running")
        self.progress_var.set(f"0 / {total}")
        self.set_status("Serial entry automation started.")
        self._append_log("Starting automation...")

        self.reader_thread = threading.Thread(target=self._read_output_worker, daemon=True)
        self.reader_thread.start()

    def _read_output_worker(self) -> None:
        process = self.process
        if process is None:
            return

        try:
            if process.stdout is not None:
                for line in process.stdout:
                    self.event_queue.put(("log", line.rstrip("\r\n")))
        except Exception as exc:
            self.event_queue.put(("log", f"[GUI] Output read error: {exc}"))
        finally:
            try:
                exit_code = process.wait(timeout=2)
            except Exception:
                exit_code = -1
            self.event_queue.put(("done", exit_code))

    def _stop_run(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.status_var.set("Stopping...")
        self.set_status("Stopping serial entry automation...")
        self._append_log("Stopping process...")
        threading.Thread(target=self._terminate_process_worker, daemon=True).start()

    def _terminate_process_worker(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _poll_events(self) -> None:
        while True:
            try:
                event_type, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "log":
                self._append_log(str(payload))
            elif event_type == "done":
                self._on_process_done(int(payload))

        if self.winfo_exists():
            self.after(100, self._poll_events)

    def _on_process_done(self, exit_code: int) -> None:
        if exit_code == 0:
            self.status_var.set("Completed")
            self.set_status("Serial entry automation completed.")
        elif self.status_var.get() == "Stopping...":
            self.status_var.set("Stopped")
            self.set_status("Serial entry automation stopped.")
        else:
            self.status_var.set("Failed")
            self.set_status("Serial entry automation failed.")
        self._set_running(False)
        self.process = None
        self.reader_thread = None

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            should_close = messagebox.askyesno(
                "Exit",
                "Automation is running. Stop it and close this window?",
                parent=self,
            )
            if not should_close:
                return
            self._terminate_process_worker()
        self.destroy()
