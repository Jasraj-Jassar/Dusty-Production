from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import customtkinter as ctk


@dataclass(frozen=True)
class TreeNode:
    path: Path
    is_dir: bool


def unique_dest_path(dest_path: Path) -> Path:
    if not dest_path.exists():
        return dest_path
    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


class PreviewPackageWindow(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        ops_root: Path,
        set_status: Callable[[str], None] | None = None,
        read_only: bool = False,
    ) -> None:
        super().__init__(master)
        self.title("Preview Package (Read Only)" if read_only else "Preview Package")
        self.geometry("1100x720")
        self.minsize(900, 600)

        self.ops_root = ops_root
        self._set_status = set_status
        self.read_only = read_only

        self._tree_nodes: dict[str, TreeNode] = {}
        self._drag_item: str | None = None
        self._current_pdf: Path | None = None
        self._doc = None
        self._page_index = 0
        self._page_image = None
        self._page_ctk_image = None

        self._build_ui()
        self.refresh_tree()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        try:
            if self._doc is not None:
                self._doc.close()
        except Exception:
            pass
        self.destroy()

    def _status(self, msg: str) -> None:
        if self._set_status:
            self._set_status(msg)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkFrame(self, corner_radius=14)
        body.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # Left: file tree
        left = ctk.CTkFrame(body, width=340, corner_radius=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_rowconfigure(2, weight=1)
        left.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(left, text="ops_grouped", font=ctk.CTkFont(size=18, weight="bold"))
        title.grid(row=0, column=0, sticky="w", padx=14, pady=(14, 8))

        btn_row = ctk.CTkFrame(left, fg_color="transparent")
        btn_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        btn_row.grid_columnconfigure(0, weight=1)
        btn_row.grid_columnconfigure(1, weight=1)

        self.refresh_btn = ctk.CTkButton(btn_row, text="Refresh", command=self.refresh_tree)
        self.refresh_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.open_btn = ctk.CTkButton(btn_row, text="Open Externally", command=self.open_selected_external)
        self.open_btn.grid(row=0, column=1, sticky="ew")

        # Treeview lives in ttk, embed it inside CTkFrame.
        import tkinter as tk
        from tkinter import ttk

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Treeview",
            background="#1f2937",
            fieldbackground="#1f2937",
            foreground="#e5e7eb",
            rowheight=26,
            borderwidth=0,
        )
        style.configure("Treeview.Heading", background="#111827", foreground="#e5e7eb")
        style.map("Treeview", background=[("selected", "#2563eb")], foreground=[("selected", "#ffffff")])

        self.tree = ttk.Treeview(left, show="tree")
        self.tree.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        if not self.read_only:
            self.tree.bind("<ButtonPress-1>", self._on_drag_start)
            self.tree.bind("<ButtonRelease-1>", self._on_drag_drop)

        # Right: PDF viewer
        right = ctk.CTkFrame(body, corner_radius=14)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)

        self.file_label = ctk.CTkLabel(right, text="Select a PDF to preview", font=ctk.CTkFont(size=16, weight="bold"))
        self.file_label.grid(row=0, column=0, sticky="w", padx=14, pady=(14, 6))

        nav = ctk.CTkFrame(right, fg_color="transparent")
        nav.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        nav.grid_columnconfigure(2, weight=1)

        self.prev_btn = ctk.CTkButton(nav, text="Prev", width=80, command=self.prev_page)
        self.prev_btn.grid(row=0, column=0, sticky="w")
        self.next_btn = ctk.CTkButton(nav, text="Next", width=80, command=self.next_page)
        self.next_btn.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.page_label = ctk.CTkLabel(nav, text="Page 0/0", text_color="gray70")
        self.page_label.grid(row=0, column=3, sticky="e")

        self.viewer = ctk.CTkScrollableFrame(right, corner_radius=12)
        self.viewer.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.viewer.grid_columnconfigure(0, weight=1)

        self.page_canvas = ctk.CTkLabel(self.viewer, text="")
        self.page_canvas.grid(row=0, column=0, sticky="nsew")

    def refresh_tree(self) -> None:
        self._tree_nodes.clear()
        self.tree.delete(*self.tree.get_children())

        if not self.ops_root.is_dir():
            self._status(f"ops_grouped not found: {self.ops_root}")
            return

        def add_dir(parent_iid: str, p: Path) -> None:
            for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    iid = str(child)
                    self._tree_nodes[iid] = TreeNode(path=child, is_dir=True)
                    self.tree.insert(parent_iid, "end", iid=iid, text=child.name, open=False)
                    add_dir(iid, child)
                else:
                    if child.suffix.lower() != ".pdf":
                        continue
                    iid = str(child)
                    self._tree_nodes[iid] = TreeNode(path=child, is_dir=False)
                    self.tree.insert(parent_iid, "end", iid=iid, text=child.name, open=False)

        root_iid = str(self.ops_root)
        self._tree_nodes[root_iid] = TreeNode(path=self.ops_root, is_dir=True)
        self.tree.insert("", "end", iid=root_iid, text=self.ops_root.name, open=True)
        add_dir(root_iid, self.ops_root)

    def _selected_iid(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _on_tree_select(self, _event=None) -> None:
        iid = self._selected_iid()
        if not iid:
            return
        node = self._tree_nodes.get(iid)
        if not node or node.is_dir:
            return
        self.open_pdf(node.path)

    def _on_drag_start(self, _event=None) -> None:
        iid = self._selected_iid()
        if not iid:
            self._drag_item = None
            return
        node = self._tree_nodes.get(iid)
        if not node or node.is_dir:
            self._drag_item = None
            return
        self._drag_item = iid

    def _on_drag_drop(self, event=None) -> None:
        if not self._drag_item:
            return
        try:
            target = self.tree.identify_row(event.y) if event is not None else ""
        except Exception:
            target = ""
        if not target or target == self._drag_item:
            self._drag_item = None
            return

        src_node = self._tree_nodes.get(self._drag_item)
        dst_node = self._tree_nodes.get(target)
        if not src_node or not dst_node:
            self._drag_item = None
            return

        if dst_node.is_dir:
            dst_folder = dst_node.path
        else:
            dst_folder = dst_node.path.parent

        self._drag_item = None
        self._move_pdf(src_node.path, dst_folder)

    def _move_pdf(self, src_pdf: Path, dst_folder: Path) -> None:
        if self.read_only:
            self._status("Move blocked: read-only preview.")
            return
        if not src_pdf.is_file():
            self._status("Move failed: source not found.")
            return
        if not dst_folder.is_dir():
            self._status("Move failed: destination is not a folder.")
            return
        if self.ops_root not in dst_folder.parents and dst_folder != self.ops_root:
            self._status("Move blocked: destination must be under ops_grouped.")
            return

        dst = unique_dest_path(dst_folder / src_pdf.name)
        try:
            shutil.move(str(src_pdf), str(dst))
        except OSError:
            self._status("Move failed.")
            return

        self._status(f"Moved: {src_pdf.name} -> {dst_folder.relative_to(self.ops_root)}")
        self.refresh_tree()
        # Keep preview open on the moved file.
        self.open_pdf(dst)

    def open_selected_external(self) -> None:
        iid = self._selected_iid()
        if not iid:
            return
        node = self._tree_nodes.get(iid)
        if not node or node.is_dir:
            return
        try:
            os.startfile(str(node.path))  # type: ignore[attr-defined]
        except Exception:
            self._status("Could not open externally.")

    def open_pdf(self, path: Path) -> None:
        self._current_pdf = path
        self.file_label.configure(text=path.name)
        self._page_index = 0
        self._load_doc()
        self._render_page()

    def _load_doc(self) -> None:
        try:
            if self._doc is not None:
                self._doc.close()
        except Exception:
            pass
        self._doc = None

        try:
            import fitz  # PyMuPDF
        except Exception:
            self.page_label.configure(text="Missing PyMuPDF (install requirements.txt)")
            self.page_canvas.configure(text="Cannot render PDF: missing dependency PyMuPDF.", image=None)
            return

        if not self._current_pdf:
            return
        try:
            self._doc = fitz.open(str(self._current_pdf))
        except Exception:
            self.page_label.configure(text="Failed to open PDF")
            self.page_canvas.configure(text="Failed to open PDF.", image=None)

    def prev_page(self) -> None:
        if not self._doc:
            return
        if self._page_index <= 0:
            return
        self._page_index -= 1
        self._render_page()

    def next_page(self) -> None:
        if not self._doc:
            return
        if self._page_index >= (len(self._doc) - 1):
            return
        self._page_index += 1
        self._render_page()

    def _render_page(self) -> None:
        if not self._doc:
            return
        try:
            from PIL import Image
            import fitz
        except Exception:
            return

        n = len(self._doc)
        self.page_label.configure(text=f"Page {self._page_index + 1}/{n}")

        try:
            page = self._doc.load_page(self._page_index)
            # Render roughly to the viewer width.
            target_w = max(900, self.winfo_width() - 420)
            zoom = max(1.0, target_w / max(1, page.rect.width))
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        except Exception:
            self.page_canvas.configure(text="Failed to render page.", image=None)
            return

        # Convert to CTkImage for HiDPI scaling safety.
        self._page_ctk_image = ctk.CTkImage(light_image=img, dark_image=img, size=(img.width, img.height))
        self.page_canvas.configure(image=self._page_ctk_image, text="")

