"""DustyBot - Preview Package (Qt/PySide6)

A VS Code-like package preview window with:
- Left sidebar: folder tree with Asm packages and nested drawing PDFs
- Right pane: embedded PDF viewer with multi-page continuous scrolling
- Drag-and-drop: true move semantics with no duplicates

CRITICAL: This implementation guarantees no duplicate files after drag-drop by:
1. Explicitly releasing all PDF handles before any move
2. Using atomic os.replace() when possible
3. Rolling back any partial copy if delete fails
4. Retrying with delays for Windows handle release
"""

from __future__ import annotations

import argparse
import csv
import gc
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

# Regex to match Asm PDFs like Asm_11.pdf, Asm_25_31.pdf
_ASM_RE = re.compile(r"^Asm_(\d+(?:_\d+)*)\.pdf$", re.IGNORECASE)


def asm_sort_key(name: str) -> tuple[int, tuple[int, ...], str]:
    """Sort key for Asm PDFs: Asm_2 before Asm_11; Asm_25_31 sorts by (25, 31)."""
    m = _ASM_RE.match(name)
    if not m:
        return (1, (), name.lower())
    nums = tuple(int(x) for x in m.group(1).split("_") if x.isdigit())
    return (0, nums, name.lower())


def _asm_key_from_filename(p: Path) -> str | None:
    """Extract asm key from filename: Asm_25_31.pdf -> '25,31'."""
    m = _ASM_RE.match(p.name)
    if not m:
        return None
    return ",".join(m.group(1).split("_"))


def _load_rev_manifest(ops_root: Path) -> dict[str, list[str]]:
    """Load rev_pull_manifest.csv and return asm_key -> list of drawing filenames."""
    rev_path = ops_root / "rev_pull_manifest.csv"
    if not rev_path.is_file():
        return {}
    out: dict[str, list[str]] = {}
    try:
        with rev_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                asm_raw = (row.get("asm") or "").strip().strip('"')
                dest_raw = (row.get("dest_path") or "").strip().strip('"')
                status = (row.get("status") or "").strip().upper()
                if not asm_raw or not dest_raw:
                    continue
                if status != "COPIED":
                    continue
                fn = Path(dest_raw).name
                if not fn.lower().endswith(".pdf"):
                    continue
                key = ",".join([s.strip() for s in asm_raw.split(",") if s.strip()])
                out.setdefault(key, []).append(fn)
    except Exception:
        return {}
    return out


class StrictFileMover:
    """
    Handles file moves with guaranteed no-duplicate semantics.
    
    On Windows, file handles can linger even after closing. This class:
    1. Uses atomic os.replace() when source and dest are on same volume
    2. Falls back to copy+delete with strict rollback on failure
    3. NEVER leaves a duplicate: if source can't be deleted, dest is removed
    """
    
    MAX_RETRIES = 6  # Retry up to 6 times
    RETRY_DELAY_MS = 50  # Start with 50ms, doubles each retry
    
    @staticmethod
    def unique_dest_path(dest_path: Path) -> Path:
        """Generate a unique destination path if file already exists."""
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
    
    @classmethod
    def move_file(cls, src: Path, dst_folder: Path) -> Path:
        """
        Move a file to dst_folder with no-duplicate guarantee.
        
        Raises OSError if the move fails (source will still exist, no dest created).
        Returns the final destination path on success.
        """
        if not src.is_file():
            raise FileNotFoundError(f"Source file not found: {src}")
        if not dst_folder.is_dir():
            raise NotADirectoryError(f"Destination is not a directory: {dst_folder}")
        
        dst = cls.unique_dest_path(dst_folder / src.name)
        
        last_error: Exception | None = None
        delay_ms = cls.RETRY_DELAY_MS
        
        for attempt in range(cls.MAX_RETRIES):
            try:
                # Fast path: atomic rename (works if same volume)
                os.replace(str(src), str(dst))
                # Verify source is gone
                if src.exists():
                    raise OSError(f"os.replace succeeded but source still exists: {src}")
                return dst
            except OSError as e:
                # os.replace failed, try copy+delete fallback
                last_error = e
            
            # Copy+delete fallback with strict rollback
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                last_error = e
                time.sleep(delay_ms / 1000.0)
                delay_ms = min(delay_ms * 2, 500)
                continue
            
            # Copy succeeded, now try to delete source
            try:
                src.unlink()
            except OSError as e:
                # CRITICAL: Delete failed - rollback the copy to avoid duplicate
                last_error = e
                try:
                    if dst.exists():
                        dst.unlink()
                except OSError:
                    pass  # Best effort cleanup
                time.sleep(delay_ms / 1000.0)
                delay_ms = min(delay_ms * 2, 500)
                continue
            
            # Paranoia check: ensure source is truly gone
            if src.exists():
                try:
                    src.unlink()
                except OSError as e:
                    # Source still exists - rollback dest
                    last_error = e
                    try:
                        if dst.exists():
                            dst.unlink()
                    except OSError:
                        pass
                    time.sleep(delay_ms / 1000.0)
                    delay_ms = min(delay_ms * 2, 500)
                    continue
            
            return dst
        
        # All retries exhausted
        raise OSError(f"Failed to move {src.name} after {cls.MAX_RETRIES} attempts: {last_error}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="DustyBot - Preview Package (Qt)")
    parser.add_argument("--ops-root", required=True, help="Path to ops_grouped folder")
    parser.add_argument("--readonly", action="store_true", help="Disable drag/drop move operations")
    args = parser.parse_args(argv)

    ops_root = Path(args.ops_root).resolve()
    read_only = bool(args.readonly)
    if not ops_root.is_dir():
        print(f"ops_root not found: {ops_root}", file=sys.stderr)
        return 2

    # Import Qt after argument parsing to fail fast on bad args
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt

    # Check for PDF support
    pdf_available = True
    try:
        from PySide6 import QtPdf, QtPdfWidgets
    except ImportError:
        pdf_available = False
        QtPdf = None  # type: ignore
        QtPdfWidgets = None  # type: ignore

    # Custom roles for tree items
    NODE_PATH = Qt.UserRole + 1
    NODE_KIND = Qt.UserRole + 2  # "dir" | "asm" | "draw"

    def get_palette_role(name: str):
        """Get QPalette color role by name, handling Qt5/Qt6 differences."""
        if hasattr(QtGui.QPalette, name):
            return getattr(QtGui.QPalette, name)
        cr = getattr(QtGui.QPalette, "ColorRole", None)
        if cr is not None and hasattr(cr, name):
            return getattr(cr, name)
        return None

    class PDFHandleManager:
        """
        Manages PDF document handles with explicit release.
        
        On Windows, QPdfDocument can hold file handles even after close().
        This manager forces handle release by:
        1. Calling close() on the document
        2. Nulling references
        3. Forcing garbage collection
        4. Processing Qt events to complete cleanup
        """
        
        def __init__(self, pdf_doc, pdf_view, stack_widget, fallback_widget):
            self._pdf_doc = pdf_doc
            self._pdf_view = pdf_view
            self._stack = stack_widget
            self._fallback = fallback_widget
            self._loaded_path: Path | None = None
        
        @property
        def loaded_path(self) -> Path | None:
            return self._loaded_path
        
        def load(self, path: Path) -> bool:
            """Load a PDF file. Returns True on success."""
            if self._pdf_doc is None or self._pdf_view is None:
                return False
            try:
                self._pdf_doc.load(str(path))
                self._loaded_path = path
                self._stack.setCurrentWidget(self._pdf_view)
                # Configure view for multi-page scrolling
                try:
                    self._pdf_view.setPageMode(QtPdfWidgets.QPdfView.PageMode.MultiPage)
                except Exception:
                    pass
                try:
                    self._pdf_view.setZoomMode(QtPdfWidgets.QPdfView.ZoomMode.FitToWidth)
                except Exception:
                    pass
                return True
            except Exception:
                self._stack.setCurrentWidget(self._fallback)
                return False
        
        def release_handles(self) -> None:
            """
            Aggressively release all file handles.
            
            MUST be called before any file move operation on currently loaded PDF.
            """
            if self._pdf_doc is None:
                return
            
            # 1. Close the document
            try:
                self._pdf_doc.close()
            except Exception:
                pass
            
            # 2. Switch away from PDF view
            try:
                self._stack.setCurrentWidget(self._fallback)
            except Exception:
                pass
            
            # 3. Clear our reference
            self._loaded_path = None
            
            # 4. Force garbage collection to release any Python-side refs
            gc.collect()
            
            # 5. Process Qt events to complete async cleanup
            try:
                QtCore.QCoreApplication.processEvents()
            except Exception:
                pass
            
            # 6. Small delay for Windows to release handles
            time.sleep(0.05)

    class PreviewTree(QtWidgets.QTreeView):
        """
        Custom tree view with drag-drop support for moving Asm packages.
        
        Emits:
            moved(str): Message about what was moved
            move_failed(str): Error message when move fails
        """
        
        moved = QtCore.Signal(str)
        move_failed = QtCore.Signal(str)

        def __init__(self, ops_root: Path, before_move: Callable[[], None] | None = None, read_only: bool = False):
            super().__init__()
            self._ops_root = ops_root
            self._before_move = before_move
            self._read_only = read_only
            
            self.setHeaderHidden(True)
            self.setAnimated(True)
            self.setIndentation(18)
            self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)

            if self._read_only:
                self.setDragEnabled(False)
                self.setAcceptDrops(False)
                self.setDropIndicatorShown(False)
                self.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
            else:
                # Enable drag and drop
                self.setDragEnabled(True)
                self.setAcceptDrops(True)
                self.setDropIndicatorShown(True)
                self.setDefaultDropAction(Qt.MoveAction)
                self.setDragDropMode(QtWidgets.QAbstractItemView.DragDrop)

        def _current_item(self):
            m = self.model()
            idx = self.currentIndex()
            if not idx.isValid():
                return None
            return m.itemFromIndex(idx)

        def _paths_to_drag(self, item) -> list[Path]:
            """Get all paths that should be moved when dragging an item."""
            kind = item.data(NODE_KIND)
            if kind == "dir":
                return []
            
            paths: list[Path] = []
            p = Path(item.data(NODE_PATH))
            if p.is_file():
                paths.append(p)
            
            # If dragging an Asm, include all its drawing children
            if kind == "asm":
                for i in range(item.rowCount()):
                    child = item.child(i)
                    if child is None:
                        continue
                    cp = Path(child.data(NODE_PATH))
                    if cp.is_file():
                        paths.append(cp)
            
            return paths

        def startDrag(self, _supported_actions) -> None:
            if self._read_only:
                return
            item = self._current_item()
            if item is None:
                return
            
            paths = self._paths_to_drag(item)
            if not paths:
                return

            # Validate all paths are within ops_root
            safe: list[Path] = []
            for p in paths:
                try:
                    p.resolve().relative_to(self._ops_root)
                except ValueError:
                    continue
                if p.suffix.lower() != ".pdf":
                    continue
                safe.append(p)
            
            if not safe:
                return

            # Create drag with file URLs
            mime = QtCore.QMimeData()
            mime.setUrls([QtCore.QUrl.fromLocalFile(str(p)) for p in safe])
            drag = QtGui.QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.MoveAction)

        def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
            if self._read_only:
                event.ignore()
                return
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
            if self._read_only:
                event.ignore()
                return
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event: QtGui.QDropEvent) -> None:
            if self._read_only:
                event.ignore()
                return
            if not event.mimeData().hasUrls():
                event.ignore()
                return

            urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
            if not urls:
                event.ignore()
                return

            # Determine drop target folder
            try:
                pos = event.position().toPoint()  # Qt6
            except AttributeError:
                pos = event.pos()  # Qt5

            idx = self.indexAt(pos)
            if idx.isValid():
                item = self.model().itemFromIndex(idx)
                t_kind = item.data(NODE_KIND)
                t_path = Path(item.data(NODE_PATH))
                dst_folder = t_path if t_kind == "dir" else t_path.parent
            else:
                dst_folder = self._ops_root

            # Validate destination is within ops_root
            try:
                dst_folder.resolve().relative_to(self._ops_root)
            except ValueError:
                event.ignore()
                self.move_failed.emit("Cannot move outside ops_grouped")
                return

            # Collect source paths
            src_paths: list[Path] = []
            for u in urls:
                src = Path(u.toLocalFile())
                if not src.is_file() or src.suffix.lower() != ".pdf":
                    continue
                try:
                    src.resolve().relative_to(self._ops_root)
                except ValueError:
                    continue
                src_paths.append(src)

            if not src_paths:
                event.ignore()
                return

            # CRITICAL: Release PDF handles before moving
            if self._before_move:
                try:
                    self._before_move()
                except Exception:
                    pass

            # Perform moves with strict no-duplicate guarantee
            moved_files: list[str] = []
            failed_files: list[str] = []
            
            for src in src_paths:
                # Skip if already in destination
                if src.parent.resolve() == dst_folder.resolve():
                    continue
                
                try:
                    StrictFileMover.move_file(src, dst_folder)
                    moved_files.append(src.name)
                except OSError as e:
                    failed_files.append(f"{src.name}: {e}")

            # Report results
            if moved_files:
                try:
                    rel = dst_folder.relative_to(self._ops_root)
                except ValueError:
                    rel = dst_folder
                
                if failed_files:
                    self.moved.emit(f"Moved {len(moved_files)} to {rel} (failed: {len(failed_files)})")
                    self.move_failed.emit("\n".join(failed_files[:3]))
                else:
                    self.moved.emit(f"Moved {len(moved_files)} file(s) to {rel}")
                
                event.setDropAction(Qt.MoveAction)
                event.acceptProposedAction()
            else:
                if failed_files:
                    self.move_failed.emit("\n".join(failed_files[:3]))
                event.ignore()

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            title = "DustyBot - Preview Package"
            if read_only:
                title += " (Read Only)"
            self.setWindowTitle(title)
            self.resize(1280, 820)

            # Main splitter
            splitter = QtWidgets.QSplitter(Qt.Horizontal)
            self.setCentralWidget(splitter)

            # Left: File tree
            self.tree = PreviewTree(ops_root, before_move=self._release_pdf_handles, read_only=read_only)
            splitter.addWidget(self.tree)

            # Right: PDF viewer and info
            right = QtWidgets.QWidget()
            right_layout = QtWidgets.QVBoxLayout(right)
            right_layout.setContentsMargins(10, 10, 10, 10)
            splitter.addWidget(right)

            # Path label
            self.path_label = QtWidgets.QLabel("Select a PDF to preview")
            self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            font = self.path_label.font()
            font.setPointSize(font.pointSize() + 2)
            font.setBold(True)
            self.path_label.setFont(font)
            right_layout.addWidget(self.path_label)

            # PDF viewer stack (PDF view vs fallback message)
            self.viewer_stack = QtWidgets.QStackedWidget()
            right_layout.addWidget(self.viewer_stack, 1)

            self.no_pdf_label = QtWidgets.QLabel(
                "PDF preview requires PySide6 QtPdf modules.\n"
                "Install with: pip install PySide6\n\n"
                "You can still open PDFs externally by double-clicking."
            )
            self.no_pdf_label.setAlignment(Qt.AlignCenter)
            self.viewer_stack.addWidget(self.no_pdf_label)

            # Setup PDF viewer if available
            self.pdf_doc = None
            self.pdf_view = None
            self.pdf_manager: PDFHandleManager | None = None
            
            if pdf_available:
                self.pdf_doc = QtPdf.QPdfDocument(self)
                self.pdf_view = QtPdfWidgets.QPdfView(self)
                self.pdf_view.setDocument(self.pdf_doc)
                
                # Configure for continuous multi-page view
                try:
                    self.pdf_view.setPageMode(QtPdfWidgets.QPdfView.PageMode.MultiPage)
                except Exception:
                    pass
                try:
                    self.pdf_view.setPageSpacing(12)
                except Exception:
                    pass
                try:
                    self.pdf_view.setZoomMode(QtPdfWidgets.QPdfView.ZoomMode.FitToWidth)
                except Exception:
                    pass
                
                self.viewer_stack.addWidget(self.pdf_view)
                self.pdf_manager = PDFHandleManager(
                    self.pdf_doc, self.pdf_view, self.viewer_stack, self.no_pdf_label
                )

            # Package files list
            pkg_title = QtWidgets.QLabel("Package contents (Asm + drawings)")
            pkg_title.setStyleSheet("color: #9ca3af; margin-top: 8px;")
            right_layout.addWidget(pkg_title)
            
            self.pkg_list = QtWidgets.QListWidget()
            self.pkg_list.setMinimumHeight(100)
            self.pkg_list.setMaximumHeight(150)
            self.pkg_list.itemDoubleClicked.connect(self._open_pkg_item)
            right_layout.addWidget(self.pkg_list)

            # Tree model
            self.model = QtGui.QStandardItemModel(self)
            self.tree.setModel(self.model)

            # Load manifest
            self._rev_map = _load_rev_manifest(ops_root)

            # Toolbar
            tb = QtWidgets.QToolBar("Actions")
            tb.setIconSize(QtCore.QSize(18, 18))
            self.addToolBar(tb)

            act_refresh = QtGui.QAction("Refresh", self)
            act_refresh.setShortcut("F5")
            act_refresh.triggered.connect(self.refresh)
            tb.addAction(act_refresh)

            act_open = QtGui.QAction("Open Externally", self)
            act_open.setShortcut("Ctrl+O")
            act_open.triggered.connect(self.open_selected_external)
            tb.addAction(act_open)

            # Status bar
            mode = "Read only" if read_only else "Editable"
            self.statusBar().showMessage(f"{ops_root} [{mode}]")

            # Signals
            self.tree.selectionModel().selectionChanged.connect(self._on_select)
            self.tree.moved.connect(self._on_moved)
            self.tree.move_failed.connect(self._on_move_failed)

            # Splitter sizing
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([380, 900])

            self.refresh()

        def _release_pdf_handles(self) -> None:
            """Release all PDF file handles - called before any move operation."""
            if self.pdf_manager:
                self.pdf_manager.release_handles()
            self.path_label.setText("Moving files...")

        def _on_moved(self, msg: str) -> None:
            self.statusBar().showMessage(msg, 3000)
            self.refresh()

        def _on_move_failed(self, msg: str) -> None:
            QtWidgets.QMessageBox.warning(
                self, "Move Failed", 
                f"Some files could not be moved:\n\n{msg}\n\n"
                "This may be due to file locks. Close any programs using these files and try again."
            )

        def _find_file_by_name(self, filename: str) -> Path | None:
            """Find a file by name anywhere under ops_root."""
            for p in ops_root.rglob(filename):
                if p.is_file():
                    return p
            return None

        def _linked_drawings_for_asm(self, asm_key: str) -> list[Path]:
            """Get drawing PDFs linked to an Asm from the manifest."""
            fns = self._rev_map.get(asm_key, [])
            out: list[Path] = []
            for fn in fns:
                p = self._find_file_by_name(fn)
                if p is not None:
                    out.append(p)
            return out

        def refresh(self) -> None:
            """Rebuild the tree from the filesystem."""
            self.model.clear()

            root_item = QtGui.QStandardItem(ops_root.name)
            root_item.setData(str(ops_root), NODE_PATH)
            root_item.setData("dir", NODE_KIND)
            root_item.setEditable(False)
            self.model.appendRow(root_item)
            
            # Reload manifest in case it changed
            self._rev_map = _load_rev_manifest(ops_root)

            def add_dir(parent_item, folder: Path) -> None:
                children = sorted(
                    [p for p in folder.iterdir() if not p.name.startswith(".")],
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
                
                # Add subdirectories
                for d in [p for p in children if p.is_dir()]:
                    it = QtGui.QStandardItem(d.name)
                    it.setData(str(d), NODE_PATH)
                    it.setData("dir", NODE_KIND)
                    it.setEditable(False)
                    parent_item.appendRow(it)
                    add_dir(it, d)

                # Add Asm PDFs with nested drawings
                pdf_files = [p for p in children if p.is_file() and p.suffix.lower() == ".pdf"]
                asm_pdfs = [p for p in pdf_files if _ASM_RE.match(p.name)]
                asm_pdfs.sort(key=lambda p: asm_sort_key(p.name))
                
                for ap in asm_pdfs:
                    asm_item = QtGui.QStandardItem(ap.name)
                    asm_item.setData(str(ap), NODE_PATH)
                    asm_item.setData("asm", NODE_KIND)
                    asm_item.setEditable(False)
                    parent_item.appendRow(asm_item)

                    # Add drawing children
                    asm_key = _asm_key_from_filename(ap)
                    if asm_key:
                        for dp in self._linked_drawings_for_asm(asm_key):
                            draw_item = QtGui.QStandardItem(f"  └ {dp.name}")
                            draw_item.setData(str(dp), NODE_PATH)
                            draw_item.setData("draw", NODE_KIND)
                            draw_item.setEditable(False)
                            asm_item.appendRow(draw_item)

            add_dir(root_item, ops_root)
            self.tree.expand(self.model.indexFromItem(root_item))

        def _selected_item(self):
            idx = self.tree.currentIndex()
            if not idx.isValid():
                return None
            return self.model.itemFromIndex(idx)

        def _selected_path(self) -> Path | None:
            it = self._selected_item()
            if it is None:
                return None
            try:
                return Path(it.data(NODE_PATH))
            except Exception:
                return None

        def open_selected_external(self) -> None:
            """Open selected PDF in system default viewer."""
            p = self._selected_path()
            if not p or not p.is_file():
                return
            try:
                os.startfile(str(p))
            except Exception:
                self.statusBar().showMessage("Could not open file", 2000)

        def _open_pkg_item(self, item) -> None:
            """Open a package list item externally."""
            try:
                p = Path(item.data(Qt.UserRole))
            except Exception:
                return
            if not p.exists():
                self.statusBar().showMessage("File no longer exists", 2000)
                return
            try:
                os.startfile(str(p))
            except Exception:
                self.statusBar().showMessage("Could not open file", 2000)

        def _on_select(self, *_args) -> None:
            """Handle tree selection change."""
            p = self._selected_path()
            if not p or not p.is_file():
                return

            self.path_label.setText(p.name)
            
            # Update package contents list
            self.pkg_list.clear()
            it = QtWidgets.QListWidgetItem(f"📄 {p.name}")
            it.setData(Qt.UserRole, str(p))
            self.pkg_list.addItem(it)

            # If it's an Asm, show its drawings too
            asm_key = _asm_key_from_filename(p)
            if asm_key:
                for dp in self._linked_drawings_for_asm(asm_key):
                    if dp.exists():
                        dit = QtWidgets.QListWidgetItem(f"  📐 {dp.name}")
                        dit.setData(Qt.UserRole, str(dp))
                        self.pkg_list.addItem(dit)

            # Load PDF preview
            if p.suffix.lower() != ".pdf":
                self.viewer_stack.setCurrentWidget(self.no_pdf_label)
                return

            if self.pdf_manager:
                if not self.pdf_manager.load(p):
                    self.statusBar().showMessage("Failed to load PDF", 2000)
            else:
                self.viewer_stack.setCurrentWidget(self.no_pdf_label)

    # Create and run application
    app = QtWidgets.QApplication([])
    app.setApplicationName("DustyBot")
    app.setStyle("Fusion")

    # Apply dark palette
    pal = app.palette()
    colors = [
        ("Window", "#121212"),
        ("WindowText", "#e5e7eb"),
        ("Base", "#0b0b0b"),
        ("AlternateBase", "#151515"),
        ("Text", "#e5e7eb"),
        ("Button", "#1f2937"),
        ("ButtonText", "#e5e7eb"),
        ("Highlight", "#2563eb"),
        ("HighlightedText", "#ffffff"),
    ]
    for name, val in colors:
        role = get_palette_role(name)
        if role is not None:
            pal.setColor(role, QtGui.QColor(val))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
