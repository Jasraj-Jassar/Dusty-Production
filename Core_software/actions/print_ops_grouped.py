import argparse
import csv
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


DEFAULT_PRINTER = "Kyocera TASKalfa 3501i"
DEFAULT_SLEEP_SECONDS = 0.2

# Match Asm_11.pdf or Asm_25_31.pdf
_ASM_RE = re.compile(r"^Asm_(\d+(?:_\d+)*)\.pdf$", re.IGNORECASE)


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


def load_rev_manifest(ops_root: Path) -> dict[str, list[str]]:
    """Return asm_key -> list of drawing dest paths (as strings)."""
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
                if status != "COPIED" and status != "ALREADY_EXISTS":
                    continue
                key = ",".join([s.strip() for s in asm_raw.split(",") if s.strip()])
                out.setdefault(key, []).append(dest_raw)
    except Exception:
        return {}
    return out


def load_group_manifest(ops_root: Path) -> dict[str, str]:
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
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            out.append(p)
            continue
        # Fallback: search by filename within ops_root
        found = find_by_name(ops_root, p.name)
        if found is not None:
            out.append(found)
    return out


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print ops_grouped Asm PDFs (letter landscape) followed by linked drawings (tabloid)."
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
        help="Seconds to wait between print jobs (default: 1.5)",
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

    def asm_bucket_key(p: Path):
        bucket, subgroup = manifest_info.get(p.name, ("", ""))
        if not bucket:
            bucket = bucket_from_path(p, ops_root)
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

    # Sumatra settings
    asm_settings = "fit,paper=letter,duplex=off"
    draw_settings = "fit,paper=tabloid,duplex=off,rotation=180"

    print(f"SumatraPDF: {sumatra}")
    print(f"Asm printer: {args.asm_printer}  settings: {asm_settings}")
    print(f"Drawing printer: {args.drawing_printer}  settings: {draw_settings}")
    print(f"Asm count: {len(asm_pdfs)}")

    failures = 0

    for asm_pdf in asm_pdfs:
        asm_key = asm_key_from_filename(asm_pdf)
        print(f"\nAsm: {asm_pdf}")

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
                print(f"[DRY] Drawing -> {dp}")
                continue
            print(f"Drawing: {dp}")
            rc = print_pdf(sumatra, args.drawing_printer, draw_settings, dp)
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
