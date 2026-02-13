from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


OPS_PART_RE = re.compile(
    r"^Page\s+(?P<pages>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)\s+Asm:\s*(?P<asm>\d+(?:,\d+)*)\s+Part:\s*(?P<part>\S+)(?:\s+.*)?$",
    re.IGNORECASE,
)
ASM_FILE_RE = re.compile(r"^Asm_(?P<asm>\d+)\.pdf$", re.IGNORECASE)

# Treat numeric trailing "-<rev>" as a revision for numeric dash parts like "10-0845-2".
# Avoid misclassifying non-numeric parts like "LC500WC-100" (not all segments numeric).
NUMERIC_PART_WITH_REV_RE = re.compile(r"^(?P<base>\d{2,}(?:-\d{2,})+)-(?P<rev>\d{1,3})$")


@dataclass(frozen=True)
class Candidate:
    rev: tuple[int, ...] | None
    path: Path
    mtime: float


@dataclass(frozen=True)
class AsmInfo:
    folder: Path
    bucket: str
    subgroup: str


def parse_base_rev(name: str) -> tuple[str, tuple[int, ...] | None]:
    s = (name or "").strip()
    if not s:
        return s, None

    parts = s.split("-")
    if len(parts) >= 3 and all(p.isdigit() for p in parts):
        # Treat the first two segments as base and the rest as revision chain.
        base = "-".join(parts[:2])
        rev = tuple(int(p) for p in parts[2:])
        return base, rev

    m = NUMERIC_PART_WITH_REV_RE.match(s)
    if m:
        return m.group("base"), (int(m.group("rev")),)

    return s, None


def better_candidate(a: Candidate | None, b: Candidate) -> Candidate:
    if a is None:
        return b

    ar = a.rev if a.rev is not None else ()
    br = b.rev if b.rev is not None else ()
    if br != ar:
        return b if br > ar else a
    # Same rev (or both none): prefer newest mtime.
    return b if b.mtime > a.mtime else a


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


def load_asm_info(ops_root: Path, manifest_csv: Path | None) -> dict[str, AsmInfo]:
    asm_info: dict[str, AsmInfo] = {}

    if manifest_csv and manifest_csv.is_file():
        with manifest_csv.open("r", encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                asm = (row.get("asm") or "").strip()
                dest = (row.get("dest") or "").strip()
                if not asm or not dest:
                    continue
                bucket = (row.get("bucket") or "").strip()
                subgroup = (row.get("subgroup") or "").strip()
                folder = (ops_root / Path(dest)).parent
                asm_info[asm] = AsmInfo(folder=folder, bucket=bucket, subgroup=subgroup)

    if asm_info:
        return asm_info

    # Fallback: search for Asm_*.pdf under ops_root.
    for pdf in ops_root.rglob("Asm_*.pdf"):
        m = ASM_FILE_RE.match(pdf.name)
        if not m:
            continue
        asm_info[m.group("asm")] = AsmInfo(folder=pdf.parent, bucket="", subgroup="")

    return asm_info


def iter_ops_parts(ops_parts_path: Path) -> list[tuple[str, str]]:
    # Returns (asm, part). asm can be "26" or "26,28" if combined.
    entries: list[tuple[str, str]] = []
    for raw in ops_parts_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        m = OPS_PART_RE.match(line)
        if not m:
            continue
        asm = m.group("asm")
        part = m.group("part")
        if asm and part:
            entries.append((asm, part))
    return entries


def iter_files(search_root: Path, exts: set[str]) -> tuple[int, int, list[Path]]:
    skip_dirs = {
        "$RECYCLE.BIN",
        "System Volume Information",
        "Windows",
        "Program Files",
        "Program Files (x86)",
        "ProgramData",
    }

    scanned_dirs = 0
    scanned_files = 0
    found: list[Path] = []

    for root, dirs, files in os.walk(search_root, topdown=True):
        scanned_dirs += 1
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        for fn in files:
            scanned_files += 1
            p = Path(root) / fn
            if exts and p.suffix.lower() not in exts:
                continue
            found.append(p)

    return scanned_dirs, scanned_files, found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Look up part files on a drive, pick latest numeric -<rev>, and copy into each Asm folder."
    )
    parser.add_argument(
        "--ops-root",
        default=str(Path.cwd() / "ops_grouped"),
        help="ops_grouped folder (default: ./ops_grouped)",
    )
    parser.add_argument(
        "--ops-parts",
        default=None,
        help="Path to ops_parts.txt (default: <ops-root>/ops_parts.txt)",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to manifest.csv (default: <ops-root>/manifest.csv)",
    )
    parser.add_argument(
        "--search-root",
        default="P:\\",
        help="Drive/folder to search, e.g. D:\\ or P:\\ (default: P:\\)",
    )
    parser.add_argument(
        "--skip-buckets",
        default="Assembly",
        help="Comma-separated buckets to skip (default: Assembly)",
    )
    parser.add_argument(
        "--skip-subgroups",
        default="PowderCoat",
        help="Comma-separated subgroups to skip (default: PowderCoat)",
    )
    parser.add_argument(
        "--ext",
        default=".pdf",
        help="Comma-separated list of file extensions to consider (default: .pdf)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not copy files, only print what would happen",
    )
    parser.add_argument(
        "--append-missing-to-ops-parts",
        action="store_true",
        help="Append a 'not found' summary to ops_parts.txt",
    )
    parser.add_argument(
        "--report-buckets",
        default="Machining",
        help="Comma-separated buckets to report missing parts for (default: Machining)",
    )
    args = parser.parse_args()

    ops_root = Path(args.ops_root)
    ops_parts_path = Path(args.ops_parts) if args.ops_parts else (ops_root / "ops_parts.txt")
    manifest_csv = Path(args.manifest) if args.manifest else (ops_root / "manifest.csv")
    search_root = Path(args.search_root)

    if not ops_root.is_dir():
        print(f"Error: ops root not found: {ops_root}")
        return 2
    if not ops_parts_path.is_file():
        print(f"Error: ops_parts.txt not found: {ops_parts_path}")
        return 2
    if not search_root.exists():
        print(f"Error: search root not found: {search_root}")
        return 2

    exts = {e.strip().lower() for e in (args.ext or "").split(",") if e.strip()}
    if "" in exts:
        exts.remove("")
    if exts and not all(e.startswith(".") for e in exts):
        print("Error: --ext must be a comma-separated list like .pdf,.dwg")
        return 2

    skip_buckets = {s.strip().lower() for s in (args.skip_buckets or "").split(",") if s.strip()}
    skip_subgroups = {s.strip().lower() for s in (args.skip_subgroups or "").split(",") if s.strip()}
    report_buckets = {s.strip().lower() for s in (args.report_buckets or "").split(",") if s.strip()}

    asm_info = load_asm_info(ops_root, manifest_csv if manifest_csv.is_file() else None)
    if not asm_info:
        print("Error: could not map Asm -> folder (manifest.csv missing and no Asm_*.pdf found).")
        return 2

    entries = iter_ops_parts(ops_parts_path)
    if not entries:
        print(f"Error: no Page/Asm/Part entries found in {ops_parts_path}")
        return 2

    # Build requested bases from parts.
    requested_bases: set[str] = set()
    asm_part_base: list[tuple[str, str, str]] = []
    skipped = 0
    for asm, part in entries:
        asm_key = asm.split(",", 1)[0].strip()
        info = asm_info.get(asm_key)
        if info:
            if info.bucket and info.bucket.strip().lower() in skip_buckets:
                skipped += 1
                continue
            if info.subgroup and info.subgroup.strip().lower() in skip_subgroups:
                skipped += 1
                continue
        base, _ = parse_base_rev(part)
        if base:
            requested_bases.add(base.upper())
            asm_part_base.append((asm, part, base))

    print(f"Parts (unique bases): {len(requested_bases)}")
    if skipped:
        print(f"Skipped entries: {skipped} (bucket/subgroup filters)")
    print(f"Searching: {search_root}  (ext: {', '.join(sorted(exts)) or 'ANY'})")

    if not requested_bases:
        out_manifest = ops_root / "rev_pull_manifest.csv"
        with out_manifest.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["asm", "bucket", "subgroup", "part", "base", "picked_rev", "source_path", "dest_path", "status"])
        print(f"Wrote: {out_manifest}")
        print("No parts to pull after filters.")
        return 0

    # Scan once, collect best candidate per base.
    best: dict[str, Candidate] = {}
    scanned_dirs, scanned_files, files = iter_files(search_root, exts)
    print(f"Scanned: {scanned_dirs} folders, {scanned_files} files")

    for p in files:
        stem = p.stem
        base, rev = parse_base_rev(stem)
        if not base:
            continue
        key = base.upper()
        if key not in requested_bases:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        cand = Candidate(rev=rev, path=p, mtime=mtime)
        best[key] = better_candidate(best.get(key), cand)

    out_manifest = ops_root / "rev_pull_manifest.csv"
    rows: list[list[str]] = []
    copied = 0
    missing = 0
    skipped_rows = 0

    for asm, part, base in asm_part_base:
        asm_key = asm.split(",", 1)[0].strip()
        info = asm_info.get(asm_key)
        bucket = info.bucket if info else ""
        subgroup = info.subgroup if info else ""
        if bucket and bucket.strip().lower() in skip_buckets:
            skipped_rows += 1
            rows.append([asm, bucket, subgroup, part, base, "", "", "", "SKIPPED_BUCKET"])
            continue
        if subgroup and subgroup.strip().lower() in skip_subgroups:
            skipped_rows += 1
            rows.append([asm, bucket, subgroup, part, base, "", "", "", "SKIPPED_SUBGROUP"])
            continue

        dest_folder = info.folder if info else None
        if dest_folder is None:
            missing += 1
            rows.append([asm, bucket, subgroup, part, base, "", "", "", "ASM_FOLDER_MISSING"])
            continue

        key = base.upper()
        cand = best.get(key)
        if cand is None:
            missing += 1
            rows.append([asm, bucket, subgroup, part, base, "", "", str(dest_folder), "NOT_FOUND"])
            continue

        src = cand.path
        dest_path = dest_folder / src.name
        if dest_path.exists():
            rev_str = "-".join(str(r) for r in (cand.rev or ()))
            rows.append([asm, bucket, subgroup, part, base, rev_str, str(src), str(dest_path), "ALREADY_EXISTS"])
            continue

        dest_path = unique_dest_path(dest_path)
        rev_str = "-".join(str(r) for r in (cand.rev or ()))
        rows.append([asm, bucket, subgroup, part, base, rev_str, str(src), str(dest_path), "COPIED" if not args.dry_run else "DRY_RUN"])
        if not args.dry_run:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(src), str(dest_path))
                copied += 1
            except OSError:
                missing += 1
                rows[-1][-1] = "COPY_FAILED"

    with out_manifest.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["asm", "bucket", "subgroup", "part", "base", "picked_rev", "source_path", "dest_path", "status"])
        w.writerows(rows)

    print(f"Wrote: {out_manifest}")
    print(f"Copied: {copied}")
    print(f"Missing/failed: {missing}")
    if skipped_rows:
        print(f"Skipped: {skipped_rows}")

    if args.append_missing_to_ops_parts and ops_parts_path.is_file():
        # Remove previous appended block if present, then append a fresh one.
        marker_start = "=== Drawing Lookup (Latest Revs) ==="
        marker_end = "=== End Drawing Lookup ==="
        existing = ops_parts_path.read_text(encoding="utf-8", errors="replace").splitlines()
        cleaned: list[str] = []
        in_block = False
        for line in existing:
            if line.strip() == marker_start:
                in_block = True
                continue
            if in_block and line.strip() == marker_end:
                in_block = False
                continue
            if not in_block:
                cleaned.append(line)
        while cleaned and cleaned[-1].strip() == "":
            cleaned.pop()

        missing_lines: list[str] = []
        for asm, bucket, subgroup, part, base, picked_rev, source_path, dest_path, status in rows:
            b = (bucket or "").strip().lower()
            if report_buckets and b not in report_buckets:
                continue
            if status not in {"NOT_FOUND", "COPY_FAILED", "ASM_FOLDER_MISSING"}:
                continue
            missing_lines.append(f"Asm {asm}: {part} ({status})")

        appended: list[str] = []
        appended.append(marker_start)
        appended.append(f"Search root: {search_root}")
        appended.append(f"Extensions: {', '.join(sorted(exts)) or 'ANY'}")
        appended.append(f"Skipped buckets: {', '.join(sorted(skip_buckets)) or '(none)'}")
        appended.append(f"Skipped subgroups: {', '.join(sorted(skip_subgroups)) or '(none)'}")
        appended.append(f"Copied: {copied}")
        if missing_lines:
            appended.append("")
            appended.append("Not found / failed (reported buckets):")
            appended.extend(missing_lines)
        else:
            appended.append("")
            appended.append("Not found / failed: none")
        appended.append(marker_end)

        ops_parts_path.write_text("\n".join(cleaned + [""] + appended).rstrip() + "\n", encoding="utf-8")

    # Missing parts are allowed; always return success unless the script hit a fatal error earlier.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
