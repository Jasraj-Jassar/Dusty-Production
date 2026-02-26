import argparse
import contextlib
import csv
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


DEFAULT_PRINTER = "Kyocera TASKalfa 3501i"
DEFAULT_SLEEP_SECONDS = 0.2

# Match Asm_11.pdf or Asm_25_31.pdf
_ASM_RE = re.compile(r"^Asm_(\d+(?:_\d+)*)\.pdf$", re.IGNORECASE)
CROSS_SUBGROUPS = {"weld_to_machine", "machine_to_weld", "machin_to_weld"}


def asm_sort_key(name: str) -> tuple[int, tuple[int, ...], str]:
    m = _ASM_RE.match(name)
    if not m:
        return (1, (), name.lower())
    nums = tuple(int(x) for x in m.group(1).split("_") if x.isdigit())
    return (0, nums, name.lower())


def asm_key_from_filename(p: Path) -> str | None:
    m = _ASM_RE.match(p.name)
    if not m:
        return None
    return ",".join(m.group(1).split("_"))


def find_sumatra(explicit_path: str | None) -> str | None:
    if explicit_path:
        p = Path(explicit_path)
        return str(p) if p.is_file() else None

    local_appdata = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    script_dir = Path(__file__).resolve().parent

    candidates = [
        os.environ.get("SUMATRA_PDF"),
        r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
        r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
        str(script_dir / "SumatraPDF.exe"),
        str(Path.cwd() / "SumatraPDF.exe"),
        str(Path.cwd() / "SumatraPDF" / "SumatraPDF.exe"),
        str(script_dir / "SumatraPDF" / "SumatraPDF.exe"),
        str(Path(local_appdata) / "SumatraPDF" / "SumatraPDF.exe") if local_appdata else None,
        str(Path(appdata) / "SumatraPDF" / "SumatraPDF.exe") if appdata else None,
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c

    which = shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")
    return which


def resolve_sheet_any(primary: Path, fallbacks: list[Path]) -> Path | None:
    if primary.is_file():
        return primary
    for fb in fallbacks:
        if fb.is_file():
            return fb
    return None


def pdf_page_count(pdf: Path) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return None


def settings_with_range(settings: str, page_range: str) -> str:
    return f"{settings},{page_range}"


def load_rev_manifest(ops_root: Path) -> dict[str, list[str]]:
    """Return asm_key -> list of drawing dest paths (as strings), excluding PowderCoat PC rows."""
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
                subgroup = (row.get("subgroup") or "").strip().lower()
                part = (row.get("part") or "").strip().lower()
                base = (row.get("base") or "").strip().lower()
                if not asm_raw or not dest_raw:
                    continue
                if status != "COPIED" and status != "ALREADY_EXISTS":
                    continue
                if subgroup == "powdercoat" and ("(pc)" in part or base.endswith("-pc")):
                    # Powder coat PC files are printed with Welding set (handled separately).
                    continue
                key = ",".join([s.strip() for s in asm_raw.split(",") if s.strip()])
                out.setdefault(key, []).append(dest_raw)
    except Exception:
        return {}
    return out


def load_powdercoat_paths(ops_root: Path) -> list[str]:
    rev_path = ops_root / "rev_pull_manifest.csv"
    if not rev_path.is_file():
        return []

    out: list[str] = []
    seen: set[str] = set()
    try:
        with rev_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                subgroup = (row.get("subgroup") or "").strip().lower()
                status = (row.get("status") or "").strip().upper()
                dest_raw = (row.get("dest_path") or "").strip().strip('"')
                part = (row.get("part") or "").strip().lower()
                base = (row.get("base") or "").strip().lower()
                if subgroup != "powdercoat":
                    continue
                if status not in {"COPIED", "ALREADY_EXISTS"}:
                    continue
                if "(pc)" not in part and not base.endswith("-pc"):
                    continue
                if not dest_raw:
                    continue
                if dest_raw in seen:
                    continue
                seen.add(dest_raw)
                out.append(dest_raw)
    except Exception:
        return []

    return sorted(out, key=str.lower)


def load_group_manifest(ops_root: Path) -> dict[str, tuple[str, str]]:
    """Return filename -> (bucket, subgroup) from ops_grouped/manifest.csv."""
    manifest_path = ops_root / "manifest.csv"
    if not manifest_path.is_file():
        return {}
    out: dict[str, tuple[str, str]] = {}
    try:
        with manifest_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                fn = (row.get("filename") or "").strip().strip('"')
                bucket = (row.get("bucket") or "").strip()
                subgroup = (row.get("subgroup") or "").strip()
                if fn:
                    out[fn] = (bucket, subgroup)
    except Exception:
        return {}
    return out


def bucket_from_path(p: Path, ops_root: Path) -> str:
    try:
        rel = p.relative_to(ops_root)
    except Exception:
        return ""
    if not rel.parts:
        return ""
    top = rel.parts[0].strip().lower()
    if top == "machining":
        return "Machining"
    if top == "welding":
        return "Welding"
    if top == "assembly":
        return "Assembly"
    return ""


def find_by_name(root: Path, filename: str) -> Path | None:
    for p in root.rglob(filename):
        if p.is_file():
            return p
    return None


def resolve_drawings(ops_root: Path, paths: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        p = Path(raw)
        resolved: Path | None = None
        if p.is_file():
            resolved = p
        else:
            # Fallback: search by filename within ops_root
            resolved = find_by_name(ops_root, p.name)
        if resolved is None:
            continue
        try:
            key = os.path.normcase(str(resolved.resolve()))
        except Exception:
            key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def find_ops_parts_cover(ops_root: Path, bucket: str) -> Path | None:
    if not bucket:
        return None
    p = ops_root / "ops_parts_sections_pdf" / f"{bucket}_ops_parts.pdf"
    return p if p.is_file() else None


def print_pdf(sumatra: str, printer: str, settings: str, pdf: Path) -> int:
    cmd = [
        sumatra,
        "-print-to",
        printer,
        "-print-settings",
        settings,
        "-silent",
        str(pdf),
    ]
    completed = subprocess.run(cmd, check=False)
    return completed.returncode


def hard_rotate_pdf(src_pdf: Path, out_pdf: Path, degrees: int = 180) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(src_pdf))
    writer = PdfWriter()
    for page in reader.pages:
        page.rotate(degrees)
        writer.add_page(page)

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with out_pdf.open("wb") as f:
        writer.write(f)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print ops_grouped Asm PDFs (with optional ops summary cover per bucket) followed by linked drawings (tabloid). PowderCoat files print at the start of the Welding set."
    )
    parser.add_argument(
        "--ops-root",
        default=str(Path.cwd() / "ops_grouped"),
        help="Path to ops_grouped (default: ./ops_grouped)",
    )
    parser.add_argument(
        "--asm-printer",
        default=DEFAULT_PRINTER,
        help=f"Printer for Asm PDFs (default: {DEFAULT_PRINTER})",
    )
    parser.add_argument(
        "--drawing-printer",
        default=DEFAULT_PRINTER,
        help=f"Printer for drawings (default: {DEFAULT_PRINTER})",
    )
    parser.add_argument(
        "--sumatra",
        default=None,
        help="Full path to SumatraPDF.exe (optional if installed in default locations)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"Seconds to wait between print jobs (default: {DEFAULT_SLEEP_SECONDS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without sending to printer",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Only print a single bucket: Assembly, Machining, or Welding (default: all)",
    )
    parser.add_argument(
        "--hard-rotate-drawings",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Create temporary hard-rotated (180 deg) copies for tabloid drawing prints only. "
            "No files are modified in ops_grouped (default: on)."
        ),
    )

    args = parser.parse_args()
    ops_root = Path(args.ops_root)

    if not ops_root.is_dir():
        print(f"Error: ops_root not found: {ops_root}")
        return 2

    sumatra = find_sumatra(args.sumatra)
    if not sumatra:
        print("Error: SumatraPDF not found.")
        print("Install SumatraPDF or pass --sumatra with the full path to SumatraPDF.exe.")
        return 3

    asm_pdfs = [p for p in ops_root.rglob("*.pdf") if _ASM_RE.match(p.name)]
    manifest_info = load_group_manifest(ops_root)
    bucket_order = {"Machining": 0, "Welding": 1, "Assembly": 2}

    only_bucket = (args.only or "").strip()
    if only_bucket:
        only_bucket = only_bucket.title()

    rotate_drawings = bool(args.hard_rotate_drawings)
    if rotate_drawings:
        try:
            import pypdf  # noqa: F401
        except Exception:
            print("Error: pypdf is required for --hard-rotate-drawings.")
            print("Install with: python -m pip install -r requirements.txt")
            return 2

    def asm_bucket_name(p: Path) -> str:
        bucket, subgroup = manifest_info.get(p.name, ("", ""))
        if not bucket:
            bucket = bucket_from_path(p, ops_root)
        return bucket

    def asm_bucket_key(p: Path):
        bucket = asm_bucket_name(p)
        _, subgroup = manifest_info.get(p.name, ("", ""))
        return (bucket_order.get(bucket, 3), bucket, subgroup, asm_sort_key(p.name))

    asm_pdfs.sort(key=asm_bucket_key)
    if only_bucket:
        asm_pdfs = [p for p in asm_pdfs if asm_bucket_key(p)[1] == only_bucket]

    if not asm_pdfs:
        if only_bucket:
            print(f"No Asm PDFs found for bucket: {only_bucket}")
        else:
            print("No Asm PDFs found.")
        return 0

    rev_map = load_rev_manifest(ops_root)
    powdercoat_drawings = resolve_drawings(ops_root, load_powdercoat_paths(ops_root))

    core_root = Path(__file__).resolve().parents[1]
    weld_sheet_path = resolve_sheet_any(
        Path.cwd() / "weld_inspect_sheet.pdf",
        [
            Path.cwd() / "Inspection_files" / "weld_inspect_sheet.pdf",
            core_root / "Inspection_files" / "weld_inspect_sheet.pdf",
        ],
    )
    mach_sheet_path = resolve_sheet_any(
        Path.cwd() / "mech_inspect_sheet.pdf",
        [
            Path.cwd() / "Inspection_files" / "mech_inspect_sheet.pdf",
            core_root / "Inspection_files" / "mech_inspect_sheet.pdf",
        ],
    )
    weld_sheet_pages = pdf_page_count(weld_sheet_path) if weld_sheet_path else 0
    mach_sheet_pages = pdf_page_count(mach_sheet_path) if mach_sheet_path else 0

    # Sumatra settings
    asm_settings = "fit,paper=letter,duplex=off"
    asm_inspection_settings = "fit,paper=letter,duplex=on"
    # Drawings are printed in tabloid without the previous 180-degree flip
    # so the sheet orientation faces the operator at pickup.
    draw_settings = "fit,paper=tabloid,duplex=off,rotation=0"

    print(f"SumatraPDF: {sumatra}")
    print(f"Asm printer: {args.asm_printer}  settings: {asm_settings}")
    print(f"Inspection settings: {asm_inspection_settings}")
    print(f"Drawing printer: {args.drawing_printer}  settings: {draw_settings}")
    print(f"Hard rotate drawings: {'on' if rotate_drawings else 'off'}")
    print(f"Asm count: {len(asm_pdfs)}")
    if weld_sheet_pages:
        print(f"Weld inspection pages: {weld_sheet_pages}")
    if mach_sheet_pages:
        print(f"Machining inspection pages: {mach_sheet_pages}")

    failures = 0
    printed_bucket_covers: set[str] = set()
    rotate_ctx = (
        tempfile.TemporaryDirectory(prefix="dustybot_print_rotate_")
        if rotate_drawings
        else contextlib.nullcontext(None)
    )
    with rotate_ctx as rotate_dir_raw:
        rotate_dir = Path(rotate_dir_raw) if rotate_dir_raw else None
        rotated_cache: dict[Path, Path] = {}

        def drawing_for_print(src: Path) -> Path:
            if not rotate_drawings or rotate_dir is None:
                return src
            key = src.resolve()
            cached = rotated_cache.get(key)
            if cached and cached.is_file():
                return cached
            out_pdf = rotate_dir / f"{src.stem}__hardrot180_{len(rotated_cache) + 1}.pdf"
            hard_rotate_pdf(src, out_pdf, degrees=180)
            rotated_cache[key] = out_pdf
            return out_pdf

        def asm_has_cross_subgroup(asm_pdf: Path) -> bool:
            try:
                rel = asm_pdf.relative_to(ops_root)
            except Exception:
                return False
            return any(p.strip().lower() in CROSS_SUBGROUPS for p in rel.parts)

        def inspection_pages_for_asm(asm_pdf: Path, bucket: str) -> int:
            if asm_has_cross_subgroup(asm_pdf):
                return (weld_sheet_pages or 0) + (mach_sheet_pages or 0)
            if bucket == "Welding":
                return weld_sheet_pages or 0
            if bucket == "Machining":
                return mach_sheet_pages or 0
            return 0

        for asm_pdf in asm_pdfs:
            asm_key = asm_key_from_filename(asm_pdf)
            bucket = asm_bucket_name(asm_pdf)

            if bucket and bucket not in printed_bucket_covers:
                cover_pdf = find_ops_parts_cover(ops_root, bucket)
                if cover_pdf is not None:
                    print(f"\nOps Summary ({bucket}): {cover_pdf}")
                    if args.dry_run:
                        print(f"[DRY] Ops Summary -> {cover_pdf}")
                    else:
                        rc = print_pdf(sumatra, args.asm_printer, asm_settings, cover_pdf)
                        if rc != 0:
                            failures += 1
                            print(f"Failed Ops Summary: {cover_pdf} (exit {rc})")
                        time.sleep(args.sleep)

                    if bucket == "Welding" and powdercoat_drawings:
                        print("PowderCoat files (Welding set):")
                        for pc_pdf in powdercoat_drawings:
                            if args.dry_run:
                                note = " [hard-rotated temp]" if rotate_drawings else ""
                                print(f"[DRY] PowderCoat -> {pc_pdf}{note}")
                                continue
                            print(f"PowderCoat: {pc_pdf}")
                            try:
                                printable_pc = drawing_for_print(pc_pdf)
                            except Exception as e:
                                failures += 1
                                print(f"Failed PowderCoat hard-rotate: {pc_pdf} ({e})")
                                continue
                            rc = print_pdf(sumatra, args.drawing_printer, draw_settings, printable_pc)
                            if rc != 0:
                                failures += 1
                                print(f"Failed PowderCoat: {pc_pdf} (exit {rc})")
                            time.sleep(args.sleep)
                printed_bucket_covers.add(bucket)

            print(f"\nAsm: {asm_pdf}")
            inspection_pages = inspection_pages_for_asm(asm_pdf, bucket)
            total_pages = pdf_page_count(asm_pdf) if inspection_pages > 0 else None
            if inspection_pages > 0 and total_pages and total_pages > inspection_pages:
                traveler_last_page = total_pages - inspection_pages
                traveler_range = f"1-{traveler_last_page}"
                inspection_range = f"{traveler_last_page + 1}-{total_pages}"
                traveler_settings = settings_with_range(asm_settings, traveler_range)
                inspection_settings = settings_with_range(asm_inspection_settings, inspection_range)
                if args.dry_run:
                    print(f"[DRY] Asm Traveler (single) -> {asm_pdf} pages {traveler_range}")
                    print(f"[DRY] Asm Inspection (double) -> {asm_pdf} pages {inspection_range}")
                else:
                    rc = print_pdf(sumatra, args.asm_printer, traveler_settings, asm_pdf)
                    if rc != 0:
                        failures += 1
                        print(f"Failed Asm Traveler: {asm_pdf} (exit {rc})")
                    time.sleep(args.sleep)
                    rc = print_pdf(sumatra, args.asm_printer, inspection_settings, asm_pdf)
                    if rc != 0:
                        failures += 1
                        print(f"Failed Asm Inspection: {asm_pdf} (exit {rc})")
                    time.sleep(args.sleep)
            else:
                if inspection_pages > 0 and not total_pages:
                    print(f"Warning: unable to read page count for inspection split, printing single-sided: {asm_pdf}")
                if inspection_pages > 0 and total_pages and total_pages <= inspection_pages:
                    print(
                        f"Warning: inspection page estimate ({inspection_pages}) >= total pages ({total_pages}), "
                        f"printing single-sided: {asm_pdf}"
                    )
                if args.dry_run:
                    print(f"[DRY] Asm -> {asm_pdf}")
                else:
                    rc = print_pdf(sumatra, args.asm_printer, asm_settings, asm_pdf)
                    if rc != 0:
                        failures += 1
                        print(f"Failed Asm: {asm_pdf} (exit {rc})")
                    time.sleep(args.sleep)

            drawings: list[Path] = []
            if asm_key:
                drawings = resolve_drawings(ops_root, rev_map.get(asm_key, []))

            if not drawings:
                continue

            for dp in drawings:
                if args.dry_run:
                    note = " [hard-rotated temp]" if rotate_drawings else ""
                    print(f"[DRY] Drawing -> {dp}{note}")
                    continue
                print(f"Drawing: {dp}")
                try:
                    printable = drawing_for_print(dp)
                except Exception as e:
                    failures += 1
                    print(f"Failed Drawing hard-rotate: {dp} ({e})")
                    continue
                rc = print_pdf(sumatra, args.drawing_printer, draw_settings, printable)
                if rc != 0:
                    failures += 1
                    print(f"Failed Drawing: {dp} (exit {rc})")
                time.sleep(args.sleep)

    if failures:
        print(f"\nDone with {failures} failure(s).")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
