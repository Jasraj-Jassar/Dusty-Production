from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ASM_RE = re.compile(r"^Asm_(\d+(?:_\d+)*)\.pdf$", re.IGNORECASE)


def asm_sort_key(name: str) -> tuple[int, tuple[int, ...], str]:
    m = ASM_RE.match(name)
    if not m:
        return (1, (), name.lower())
    nums = tuple(int(x) for x in m.group(1).split("_") if x.isdigit())
    return (0, nums, name.lower())


def asm_key_from_filename(path: Path) -> str | None:
    m = ASM_RE.match(path.name)
    if not m:
        return None
    return ",".join(m.group(1).split("_"))


def first_job_in_ops_parts(ops_parts: Path) -> str | None:
    if not ops_parts.is_file():
        return None
    for line in ops_parts.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.lower().startswith("job:"):
            return line.split(":", 1)[1].strip() or None
    return None


def load_rev_manifest(ops_root: Path) -> dict[str, list[str]]:
    """
    Return asm_key -> list of drawing filenames.
    We store filenames (not paths) because user can move files around after the manifest is written.
    """
    rev_path = ops_root / "rev_pull_manifest.csv"
    if not rev_path.is_file():
        return {}

    out: dict[str, list[str]] = {}
    with rev_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
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
    return out


def find_by_name_under_root(root: Path, filename: str) -> Path | None:
    # Fast-ish on typical job sizes; keeps behavior stable after manual moves.
    for p in root.rglob(filename):
        if p.is_file():
            return p
    return None


def iter_asm_pdfs(op_root: Path) -> list[Path]:
    if not op_root.is_dir():
        return []
    asms = [p for p in op_root.rglob("Asm_*.pdf") if p.is_file() and ASM_RE.match(p.name)]

    def key(p: Path):
        rel_parent = str(p.parent.relative_to(op_root)).lower() if p.parent != op_root else ""
        return (rel_parent, asm_sort_key(p.name))

    return sorted(asms, key=key)


@dataclass(frozen=True)
class AddRow:
    op: str
    kind: str  # ASM|DRAW|ERROR
    asm_key: str
    src: str
    pages: int
    note: str


def add_pdf(writer, src_pdf: Path) -> int:
    from pypdf import PdfReader

    r = PdfReader(str(src_pdf))
    for page in r.pages:
        writer.add_page(page)
    return len(r.pages)


def build_op_package(
    *,
    op_name: str,
    op_root: Path,
    ops_root: Path,
    rev_map: dict[str, list[str]],
    out_pdf: Path,
    rows: list[AddRow],
) -> int:
    from pypdf import PdfWriter

    writer = PdfWriter()
    total_pages = 0

    for asm_pdf in iter_asm_pdfs(op_root):
        asm_key = asm_key_from_filename(asm_pdf) or ""
        try:
            pages = add_pdf(writer, asm_pdf)
        except Exception as e:
            rows.append(AddRow(op_name, "ERROR", asm_key, str(asm_pdf), 0, f"Asm read failed: {e}"))
            continue
        total_pages += pages
        rows.append(AddRow(op_name, "ASM", asm_key, str(asm_pdf), pages, ""))

        for draw_fn in rev_map.get(asm_key, []):
            draw_path = find_by_name_under_root(ops_root, draw_fn)
            if draw_path is None:
                rows.append(AddRow(op_name, "ERROR", asm_key, draw_fn, 0, "Drawing missing"))
                continue
            try:
                dpages = add_pdf(writer, draw_path)
            except Exception as e:
                rows.append(AddRow(op_name, "ERROR", asm_key, str(draw_path), 0, f"Drawing read failed: {e}"))
                continue
            total_pages += dpages
            rows.append(AddRow(op_name, "DRAW", asm_key, str(draw_path), dpages, ""))

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with out_pdf.open("wb") as f:
        writer.write(f)
    return total_pages


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build final combined drawing package PDFs from ops_grouped (Asm + drawings per asm)."
    )
    parser.add_argument(
        "--ops-root",
        default=str(Path.cwd() / "ops_grouped"),
        help="ops_grouped folder (default: ./ops_grouped)",
    )
    parser.add_argument(
        "--out-root",
        default=str(Path.cwd()),
        help="Output root folder (default: cwd). Output goes under <out-root>/final_packages/...",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Optional explicit output directory (overrides --out-root).",
    )
    args = parser.parse_args()

    ops_root = Path(args.ops_root)
    if not ops_root.is_dir():
        print(f"Error: ops root not found: {ops_root}")
        return 2

    job = first_job_in_ops_parts(ops_root / "ops_parts.txt") or "UNKNOWN"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = Path(args.out_root) / "final_packages" / f"DrawingPackage - {job} - {stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    rev_map = load_rev_manifest(ops_root)
    rows: list[AddRow] = []

    op_order = ["Assembly", "Machining", "Welding"]
    op_pdfs: list[Path] = []
    for op in op_order:
        op_root = ops_root / op
        out_pdf = out_dir / f"{op}_Package.pdf"
        if not op_root.is_dir():
            continue
        pages = build_op_package(
            op_name=op,
            op_root=op_root,
            ops_root=ops_root,
            rev_map=rev_map,
            out_pdf=out_pdf,
            rows=rows,
        )
        op_pdfs.append(out_pdf)
        print(f"Wrote: {out_pdf} ({pages} pages)")

    # Build one combined PDF across operations.
    if op_pdfs:
        from pypdf import PdfWriter

        all_pdf = out_dir / "FINAL_Drawing_Package.pdf"
        w = PdfWriter()
        total = 0
        for p in op_pdfs:
            if not p.is_file():
                continue
            try:
                total += add_pdf(w, p)
            except Exception as e:
                rows.append(AddRow("ALL", "ERROR", "", str(p), 0, f"Op package read failed: {e}"))
        with all_pdf.open("wb") as f:
            w.write(f)
        print(f"Wrote: {all_pdf} ({total} pages)")
        print(f"FINAL_PDF: {all_pdf}")

    manifest = out_dir / "final_package_manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["operation", "kind", "asm_key", "source", "pages", "note"])
        for r in rows:
            w.writerow([r.op, r.kind, r.asm_key, r.src, r.pages, r.note])
    print(f"Wrote: {manifest}")
    print(f"OUT_DIR: {out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

