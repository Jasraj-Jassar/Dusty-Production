from __future__ import annotations

import argparse
import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SheetSpec:
    name: str
    path: Path
    sha256: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_sheet(primary: Path, fallback: Path | None) -> Path | None:
    if primary.is_file():
        return primary
    if fallback and fallback.is_file():
        return fallback
    return None


def resolve_sheet_any(primary: Path, fallbacks: list[Path]) -> Path | None:
    if primary.is_file():
        return primary
    for fb in fallbacks:
        if fb.is_file():
            return fb
    return None


def normalize_text(s: str) -> str:
    # Best-effort normalization for reliable comparisons.
    return " ".join((s or "").split()).strip().lower()


def sheet_text_has_signal(sheet_pdf: Path) -> bool:
    from pypdf import PdfReader

    try:
        r = PdfReader(str(sheet_pdf))
    except Exception:
        return True

    combined = ""
    for p in r.pages:
        t = p.extract_text() or ""
        combined += t
        if len(combined) > 5000:
            break
    combined = normalize_text(combined)
    # If the sheet is completely un-extractable/blank, don't treat matches as definitive.
    return any(ch.isalnum() for ch in combined)


def is_sheet_appended(target_pdf: Path, sheet_pdf: Path) -> bool:
    from pypdf import PdfReader

    try:
        tgt = PdfReader(str(target_pdf))
        sht = PdfReader(str(sheet_pdf))
    except Exception:
        return False

    if len(tgt.pages) < len(sht.pages):
        return False

    if not sheet_text_has_signal(sheet_pdf):
        return False

    # Compare the last N pages' extracted text.
    n = len(sht.pages)
    for i in range(1, n + 1):
        a = normalize_text(tgt.pages[-i].extract_text() or "")
        b = normalize_text(sht.pages[-i].extract_text() or "")
        if a != b:
            return False
    return True


def is_sequence_appended(target_pdf: Path, sheets: list[Path]) -> bool:
    """Check if the target ends with the exact concatenation of all sheets (in order)."""
    if not sheets:
        return True

    from pypdf import PdfReader

    try:
        tgt = PdfReader(str(target_pdf))
    except Exception:
        return False

    sheet_readers = []
    for s in sheets:
        try:
            sheet_readers.append(PdfReader(str(s)))
        except Exception:
            return False

    total = sum(len(r.pages) for r in sheet_readers)
    if len(tgt.pages) < total:
        return False

    # If any sheet is completely un-extractable/blank, don't treat matches as definitive.
    for s in sheets:
        if not sheet_text_has_signal(s):
            return False

    idx = len(tgt.pages) - total
    for r in sheet_readers:
        for p in r.pages:
            a = normalize_text(tgt.pages[idx].extract_text() or "")
            b = normalize_text(p.extract_text() or "")
            if a != b:
                return False
            idx += 1
    return True


def append_sheets(target_pdf: Path, sheets: list[SheetSpec], dry_run: bool, force: bool) -> str:
    """
    Append multiple sheets in order. Idempotent by checking the full end-of-file sequence.
    """
    if not sheets:
        return "SKIP_NO_SHEETS"

    if not force and is_sequence_appended(target_pdf, [s.path for s in sheets]):
        return "SKIP_ALREADY"

    if dry_run:
        return "DRY_RUN"

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()

    src = PdfReader(str(target_pdf))
    for page in src.pages:
        writer.add_page(page)

    for sheet in sheets:
        sheet_reader = PdfReader(str(sheet.path))
        for page in sheet_reader.pages:
            writer.add_page(page)

    tmp = target_pdf.with_suffix(target_pdf.suffix + ".tmp")
    with tmp.open("wb") as f:
        writer.write(f)
    tmp.replace(target_pdf)

    return "APPENDED"


def append_sheet(target_pdf: Path, sheet: SheetSpec, kind: str, dry_run: bool, force: bool) -> str:
    if not force and is_sheet_appended(target_pdf, sheet.path):
        return "SKIP_ALREADY"

    if dry_run:
        return "DRY_RUN"

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()

    src = PdfReader(str(target_pdf))
    for page in src.pages:
        writer.add_page(page)

    sheet_reader = PdfReader(str(sheet.path))
    for page in sheet_reader.pages:
        writer.add_page(page)

    tmp = target_pdf.with_suffix(target_pdf.suffix + ".tmp")
    with tmp.open("wb") as f:
        writer.write(f)
    tmp.replace(target_pdf)

    return "APPENDED"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append inspection sheet PDFs to the end of traveler Asm_*.pdf files."
    )
    parser.add_argument(
        "--ops-root",
        default=str(Path.cwd() / "ops_grouped"),
        help="ops_grouped folder (default: ./ops_grouped)",
    )
    parser.add_argument(
        "--weld-sheet",
        default=str(Path.cwd() / "weld_inspect_sheet.pdf"),
        help="Path to weld inspection sheet PDF (default: ./weld_inspect_sheet.pdf)",
    )
    parser.add_argument(
        "--mach-sheet",
        default=str(Path.cwd() / "mech_inspect_sheet.pdf"),
        help="Path to machining inspection sheet PDF (default: ./mech_inspect_sheet.pdf)",
    )
    parser.add_argument(
        "--assets-root",
        default=None,
        help="Optional assets folder containing Inspection_files/ (defaults to repo root inferred from script location)",
    )
    parser.add_argument(
        "--exclude-weld-subgroup",
        default="PowderCoat",
        help="Exclude welding subgroup folder name (default: PowderCoat)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip text-based already-appended checks (fastest, always append unless --force is false).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write, only print actions")
    parser.add_argument("--force", action="store_true", help="Append even if marker exists")
    args = parser.parse_args()

    ops_root = Path(args.ops_root)
    if not ops_root.is_dir():
        print(f"Error: ops root not found: {ops_root}")
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    assets_root = Path(args.assets_root).resolve() if args.assets_root else repo_root
    # Search order:
    # 1) explicit file path (args.*_sheet)
    # 2) workspace-local Inspection_files/
    # 3) repo-root/Inspection_files/ (or --assets-root/Inspection_files)
    weld_sheet_path = resolve_sheet_any(
        Path(args.weld_sheet),
        [
            Path.cwd() / "Inspection_files" / "weld_inspect_sheet.pdf",
            assets_root / "Inspection_files" / "weld_inspect_sheet.pdf",
        ],
    )
    mach_sheet_path = resolve_sheet_any(
        Path(args.mach_sheet),
        [
            Path.cwd() / "Inspection_files" / "mech_inspect_sheet.pdf",
            assets_root / "Inspection_files" / "mech_inspect_sheet.pdf",
        ],
    )

    if not weld_sheet_path:
        print("Error: weld inspection sheet not found (weld_inspect_sheet.pdf).")
        return 2
    if not mach_sheet_path:
        print("Error: machining inspection sheet not found (mech_inspect_sheet.pdf).")
        return 2

    weld_sheet = SheetSpec("weld", weld_sheet_path, sha256_file(weld_sheet_path))
    mach_sheet = SheetSpec("mach", mach_sheet_path, sha256_file(mach_sheet_path))

    exclude = (args.exclude_weld_subgroup or "").strip().lower()

    CROSS_SUBGROUPS = {"weld_to_machine", "machine_to_weld", "machin_to_weld"}

    def rel_has_any(rel_parts: Iterable[str], names: set[str]) -> bool:
        for p in rel_parts:
            if p.strip().lower() in names:
                return True
        return False

    targets: list[tuple[Path, list[SheetSpec], str]] = []
    for pdf in ops_root.rglob("Asm_*.pdf"):
        try:
            rel = pdf.relative_to(ops_root)
        except Exception:
            continue

        top = rel.parts[0].lower() if rel.parts else ""
        if top == "welding":
            if exclude and any(p.lower() == exclude for p in rel.parts):
                continue
            if rel_has_any(rel.parts, CROSS_SUBGROUPS):
                targets.append((pdf, [weld_sheet, mach_sheet], "weld+mach"))
            else:
                targets.append((pdf, [weld_sheet], "weld"))
        elif top == "machining":
            if rel_has_any(rel.parts, CROSS_SUBGROUPS):
                targets.append((pdf, [weld_sheet, mach_sheet], "weld+mach"))
            else:
                targets.append((pdf, [mach_sheet], "mach"))

    if not targets:
        print("No Asm_*.pdf targets found under Welding/Machining.")
        return 0

    rows: list[list[str]] = []
    counts: dict[str, int] = {"APPENDED": 0, "SKIP_ALREADY": 0, "DRY_RUN": 0, "ERROR": 0}
    status_by_pdf: dict[str, str] = {}
    for pdf, sheets, kind in sorted(targets, key=lambda t: str(t[0]).lower()):
        try:
            if args.no_verify:
                if args.dry_run:
                    status = "DRY_RUN"
                else:
                    from pypdf import PdfReader, PdfWriter

                    writer = PdfWriter()
                    src = PdfReader(str(pdf))
                    for page in src.pages:
                        writer.add_page(page)
                    for sheet in sheets:
                        sheet_reader = PdfReader(str(sheet.path))
                        for page in sheet_reader.pages:
                            writer.add_page(page)
                    tmp = pdf.with_suffix(pdf.suffix + ".tmp")
                    with tmp.open("wb") as f:
                        writer.write(f)
                    tmp.replace(pdf)
                    status = "APPENDED"
            else:
                if len(sheets) == 1:
                    status = append_sheet(pdf, sheets[0], kind=kind, dry_run=bool(args.dry_run), force=bool(args.force))
                else:
                    status = append_sheets(pdf, sheets, dry_run=bool(args.dry_run), force=bool(args.force))
        except Exception as e:
            status = "ERROR"
            print(f"Error: {pdf} ({e})")
            for s in sheets:
                rows.append([str(pdf), kind, s.path.name, s.sha256, status, str(e)])
        else:
            for s in sheets:
                rows.append([str(pdf), kind, s.path.name, s.sha256, status, ""])
        status_by_pdf[pdf.name] = status
        counts[status] = counts.get(status, 0) + 1
        if status in {"APPENDED", "DRY_RUN"}:
            added = "+".join([s.path.name for s in sheets])
            print(f"{status}: {pdf}  (+{added})")

    if not args.dry_run:
        manifest = ops_root / "inspection_append_manifest.csv"
        with manifest.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["pdf", "kind", "sheet", "sheet_sha256", "status", "error"])
            w.writerows(rows)
        print(f"Wrote: {manifest}")

        # Update ops_grouped/manifest.csv with an inspection status column.
        ops_manifest = ops_root / "manifest.csv"
        if ops_manifest.is_file():
            try:
                with ops_manifest.open("r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    fieldnames = list(reader.fieldnames or [])
                    status_col = "inspection_appended"
                    if status_col not in fieldnames:
                        fieldnames.append(status_col)
                    updated_rows: list[dict[str, str]] = []
                    for row in reader:
                        fn = (row.get("filename") or "").strip()
                        row[status_col] = status_by_pdf.get(fn, row.get(status_col, ""))
                        updated_rows.append(row)
                with ops_manifest.open("w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(updated_rows)
                print(f"Updated: {ops_manifest} (added {status_col})")
            except Exception as e:
                print(f"Warning: failed to update manifest.csv ({e})")

        # Clean up any older sidecar marker files from previous versions.
        removed = 0
        for p in ops_root.rglob("*.inspect_*.txt"):
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
        if removed:
            print(f"Removed old marker files: {removed}")

    print("Summary:")
    for k in ["APPENDED", "SKIP_ALREADY", "DRY_RUN", "ERROR"]:
        print(f"{k}: {counts.get(k, 0)}")

    return 0 if counts.get("ERROR", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
