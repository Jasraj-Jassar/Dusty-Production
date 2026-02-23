from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


PAGE_RE = re.compile(
    r"^Page\s+(?P<pages>\d+(?:-\d+)?)\s+Asm:\s*(?P<asm>\d+)\s+Part:\s*(?P<part>\S+)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AsmLoc:
    section: str  # e.g. "Machining" or "Assembly/PowderCoat"
    bucket: str
    subgroup: str


def load_asm_locations(manifest_csv: Path) -> dict[str, AsmLoc]:
    asm_loc: dict[str, AsmLoc] = {}
    with manifest_csv.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            asm = (row.get("asm") or "").strip()
            bucket = (row.get("bucket") or "").strip()
            subgroup = (row.get("subgroup") or "").strip()
            dest = (row.get("dest") or "").strip()
            if not asm:
                continue

            section = ""
            if dest:
                # dest includes filename, e.g. "Assembly/PowderCoat/Asm_1.pdf"
                section = str(Path(dest).parent).replace("\\", "/")
            elif bucket and subgroup:
                section = f"{bucket}/{subgroup}"
            elif bucket:
                section = bucket
            else:
                section = "Unmapped"

            asm_loc[asm] = AsmLoc(section=section, bucket=bucket, subgroup=subgroup)
    return asm_loc


def write_ops_parts(ops_root: Path, sections: dict[str, list[str]], job_line: str | None) -> None:
    order_bucket = {"Assembly": 0, "Machining": 1, "Welding": 2}

    def key(section: str) -> tuple[int, str]:
        bucket = section.split("/", 1)[0]
        return (order_bucket.get(bucket, 99), section.lower())

    out_lines: list[str] = []
    for section in sorted(sections.keys(), key=key):
        lines = sections[section]
        if not lines:
            continue
        if out_lines:
            out_lines.append("")
        out_lines.append(f"=== {section} ===")
        if job_line:
            out_lines.append(job_line)
        out_lines.extend(lines)

    ops_path = ops_root / "ops_parts.txt"
    if not out_lines:
        ops_path.write_text("No entries.\n", encoding="utf-8")
    else:
        ops_path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate ops_grouped/ops_parts.txt (single file) from parts.txt and ops_grouped/manifest.csv."
    )
    parser.add_argument(
        "--parts",
        default=str(Path.cwd() / "insert-traveler" / "parts.txt"),
        help="Path to parts.txt (default: ./insert-traveler/parts.txt)",
    )
    parser.add_argument(
        "--manifest",
        default=str(Path.cwd() / "ops_grouped" / "manifest.csv"),
        help="Path to manifest.csv (default: ./ops_grouped/manifest.csv)",
    )
    parser.add_argument(
        "--ops-root",
        default=str(Path.cwd() / "ops_grouped"),
        help="ops_grouped folder (default: ./ops_grouped)",
    )
    args = parser.parse_args()

    parts_path = Path(args.parts)
    manifest_path = Path(args.manifest)
    ops_root = Path(args.ops_root)

    if not parts_path.is_file():
        print(f"Error: parts.txt not found: {parts_path}")
        return 2
    if not manifest_path.is_file():
        print(f"Error: manifest.csv not found: {manifest_path}")
        return 2
    if not ops_root.is_dir():
        print(f"Error: ops root not found: {ops_root}")
        return 2

    # Delete old per-folder parts files so the repo only relies on ops_parts.txt.
    removed = 0
    for p in ops_root.rglob("parts_*.txt"):
        if p.name == "ops_parts.txt":
            continue
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
    if removed:
        print(f"Removed old parts_*.txt files: {removed}")

    asm_loc = load_asm_locations(manifest_path)
    if not asm_loc:
        print("Error: no usable Asm rows found in manifest.csv")
        return 2

    sections: dict[str, list[str]] = defaultdict(list)
    job_line: str | None = None
    missing_asms: set[str] = set()

    for raw in parts_path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s:
            continue

        low = s.lower()
        if low.startswith("job:"):
            job_line = s
            continue
        if low.startswith("file:"):
            continue
        if low.startswith("for stock:") or low.startswith("for order:") or low.startswith("for stock/order:"):
            continue

        m = PAGE_RE.match(s)
        if not m:
            continue

        asm = m.group("asm")
        loc = asm_loc.get(asm)
        if not loc:
            missing_asms.add(asm)
            section = "Unmapped"
        else:
            section = loc.section or "Unmapped"
        sections[section].append(s)

    write_ops_parts(ops_root, sections, job_line)
    print(f"Wrote: {ops_root / 'ops_parts.txt'}")
    if missing_asms:
        print(f"Warning: {len(missing_asms)} Asm value(s) were not in manifest.csv (placed in Unmapped): {sorted(missing_asms)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

