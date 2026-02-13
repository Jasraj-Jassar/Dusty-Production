from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="DustyBot - Preview PDF (Qt)")
    parser.add_argument("--pdf", required=True, help="Path to PDF to preview")
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.is_file():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        from PySide6.QtCore import Qt
    except Exception as e:
        print(f"Missing PySide6: {e}", file=sys.stderr)
        try:
            os.startfile(str(pdf_path))  # type: ignore[attr-defined]
            return 0
        except Exception:
            return 3

    try:
        from PySide6 import QtPdf, QtPdfWidgets  # type: ignore
    except Exception as e:
        print(f"Missing QtPdf modules: {e}", file=sys.stderr)
        try:
            os.startfile(str(pdf_path))  # type: ignore[attr-defined]
            return 0
        except Exception:
            return 3

    app = QtWidgets.QApplication([])
    app.setApplicationName("DustyBot")
    app.setStyle("Fusion")

    win = QtWidgets.QMainWindow()
    win.setWindowTitle(f"DustyBot - {pdf_path.name}")
    win.resize(1200, 820)

    doc = QtPdf.QPdfDocument(win)  # type: ignore[attr-defined]
    view = QtPdfWidgets.QPdfView(win)  # type: ignore[attr-defined]
    view.setDocument(doc)
    try:
        view.setPageMode(QtPdfWidgets.QPdfView.PageMode.MultiPage)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        view.setPageSpacing(12)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        view.setZoomMode(QtPdfWidgets.QPdfView.ZoomMode.FitToWidth)  # type: ignore[attr-defined]
    except Exception:
        pass

    win.setCentralWidget(view)

    tb = QtWidgets.QToolBar("Actions")
    win.addToolBar(tb)
    act_open = QtGui.QAction("Open Externally", win)
    act_open.triggered.connect(lambda: os.startfile(str(pdf_path)))  # type: ignore[attr-defined]
    tb.addAction(act_open)

    doc.load(str(pdf_path))
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

