from __future__ import annotations

import argparse
import csv
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


ASM_RE = re.compile(r"Asm[_\s-]*(\d+)", re.IGNORECASE)


def extract_first_value(text: str, label: str) -> str | None:
    match = re.search(rf"(?i)\b{re.escape(label)}\s*:\s*([^\r\n]+)", text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def extract_first_part(text: str) -> str | None:
    value = extract_first_value(text, "Part")
    if not value:
        return None
    if "/" in value:
        value = value.split("/", 1)[0].strip()
    return value or None


def read_pdf_text(pdf_path: Path, max_pages: int = 50, max_chars: int = 200_000) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    chunks: list[str] = []
    total = 0
    for page in reader.pages[:max_pages]:
        t = page.extract_text() or ""
        if not t:
            continue
        chunks.append(t)
        total += len(t)
        if total >= max_chars:
            break
    return "\n".join(chunks)


def extract_operations_text(text: str, max_block_chars: int = 60_000) -> str:
    """Best-effort extraction of the OPERATIONS section(s).

    This avoids misclassification from part descriptions like "welding machine"
    that can appear in Assembly travelers but are not actual welding operations.
    """
    upper = text.upper()
    starts = [m.start() for m in re.finditer(r"\bOPERATIONS\b", upper)]
    if not starts:
        return ""

    # Common section boundaries after the operations table.
    stops = [
        "SCHEDULING RESOURCES",
        "SUBASSEMBLY COMPONENTS",
        "RAW MATERIAL COMPONENTS",
        "MATERIAL COMPONENTS",
        "COMPONENTS:",
        "JOBTRAV:",
    ]

    blocks: list[str] = []
    for s in starts:
        e = len(text)
        for marker in stops:
            idx = upper.find(marker, s + 10)
            if idx != -1:
                e = min(e, idx)
        blocks.append(text[s:e].strip())

    out = "\n\n".join([b for b in blocks if b])
    if len(out) > max_block_chars:
        out = out[:max_block_chars]
    return out


def get_pdf_page_count(pdf_path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path)).pages)


def count_kw(text: str, kw: str) -> int:
    if re.match(r"^[A-Za-z0-9_]+$", kw):
        return len(re.findall(rf"(?i)\b{re.escape(kw)}\b", text))
    return len(re.findall(rf"(?i){re.escape(kw)}", text))


@dataclass(frozen=True)
class Classification:
    bucket: str
    asm: str | None
    part: str | None
    score_welding: int
    score_machining: int
    score_assembly: int
    reason: str


def choose_subgroup(cls: Classification, text: str, page_count: int) -> str | None:
    part_upper = (cls.part or "").upper()
    text_upper = text.upper()

    if cls.bucket == "Assembly":
        if "LASER ETCH" in text_upper:
            return "LaserEtch"
        if part_upper.startswith("CMN"):
            return "LaserEtch"
        if "CMN" in part_upper:
            return "LaserEtch"
        if "ELC" in part_upper or "ELEC" in part_upper:
            return "Elec"
        return None

    if cls.bucket == "Welding":
        if "POWDER COAT" in text_upper or "POWDERCOAT" in text_upper:
            return "PowderCoat"
        # Single-page travelers are very likely single-op; avoid cross-step subfolders.
        if page_count > 1 and cls.score_machining > 0 and cls.score_welding >= 6:
            return "Weld_to_Machine"
        return None

    if cls.bucket == "Machining":
        if page_count > 1 and cls.score_welding > 0 and cls.score_machining >= 6:
            return "Machin_to_Weld"
        return None

    return None


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


def classify(pdf_path: Path, text: str) -> Classification:
    name = pdf_path.stem
    asm_match = ASM_RE.search(name)
    asm = asm_match.group(1) if asm_match else None

    part = extract_first_part(text)
    part_upper = (part or "").upper()
    ops_text = extract_operations_text(text)
    score_text = ops_text if ops_text else text
    score_upper = score_text.upper()

    # Hard rules (domain rules)
    if "LASER ETCH" in score_upper:
        return Classification("Assembly", asm, part, 0, 0, 0, "Laser Etch op -> Assembly")
    if "CMN" in part_upper:
        return Classification("Assembly", asm, part, 0, 0, 0, "CMN* -> Assembly (laser etching)")
    if "ELC" in part_upper:
        return Classification("Assembly", asm, part, 0, 0, 0, "ELC* -> Assembly (electrical)")
    if "POWDER COAT" in score_upper or "POWDERCOAT" in score_upper:
        return Classification("Welding", asm, part, 0, 0, 0, "Powder coat -> Welding")
    if "SAW CUT" in score_upper or "SAW-CUT" in score_upper or "SAWCUT" in score_upper:
        return Classification("Machining", asm, part, 0, 0, 0, "Saw cut -> Machining")
    if "KEY CUT" in score_upper or "KEYCUT" in score_upper or "KEY CUTTING" in score_upper:
        return Classification("Machining", asm, part, 0, 0, 0, "Key cut -> Machining")

    # Keyword scoring (fallback)
    welding_kws = [
        ("weld", 3),
        ("welding", 3),
        ("fabricat", 2),
        ("grind", 1),
        ("fixture", 1),
        ("mig", 2),
        ("tig", 2),
        ("spot weld", 3),
        ("tap", 2),
    ]
    machining_kws = [
        ("machine", 3),
        ("machining", 3),
        ("cnc", 2),
        ("mill", 2),
        ("milling", 2),
        ("lathe", 2),
        ("turn", 2),
        ("drill", 2),
        ("bore", 2),
        ("ream", 2),
        ("press brake", 3),
        ("pressbrake", 3),
        ("pressing", 2),
        ("press", 2),
        ("deburr", 1),
        ("saw", 2),
        ("bandsaw", 2),
        ("sawcut", 3),
        ("saw cut", 3),
        ("key cut", 3),
        ("keycut", 3),
        ("keyway", 2),
    ]
    assembly_kws = [
        ("assembly", 3),
        ("assemble", 3),
        ("install", 2),
        ("wiring", 2),
        ("electrical", 2),
        ("label", 1),
        ("etch", 2),
        ("laser", 2),
    ]

    sw = sum(count_kw(score_text, kw) * w for kw, w in welding_kws)
    sm = sum(count_kw(score_text, kw) * w for kw, w in machining_kws)
    sa = sum(count_kw(score_text, kw) * w for kw, w in assembly_kws)

    scores = {"Welding": sw, "Machining": sm, "Assembly": sa}
    best_bucket = max(scores, key=scores.get)
    best_score = scores[best_bucket]

    if best_score == 0:
        return Classification("Assembly", asm, part, sw, sm, sa, "No keywords matched; default Assembly")

    tied = [b for b, s in scores.items() if s == best_score]
    if len(tied) > 1:
        for p in ["Machining", "Welding", "Assembly"]:
            if p in tied:
                return Classification(p, asm, part, sw, sm, sa, f"Tie {tied}; priority -> {p}")

    if ops_text:
        return Classification(best_bucket, asm, part, sw, sm, sa, "Ops keyword score")
    return Classification(best_bucket, asm, part, sw, sm, sa, "Keyword score")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Group asm_split PDFs into Welding/Machining/Assembly folders based on operations."
    )
    parser.add_argument(
        "--input",
        default=str(Path.cwd() / "insert-traveler" / "asm_split"),
        help="Folder with Asm_*.pdf (default: ./insert-traveler/asm_split)",
    )
    parser.add_argument(
        "--output",
        default=str(Path.cwd() / "ops_grouped"),
        help="Output folder (default: ./ops_grouped)",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Move files instead of copying (default: copy)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing files",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete previous Assembly/Machining/Welding folders (and manifest) before grouping",
    )
    args = parser.parse_args()

    try:
        import pypdf  # noqa: F401
    except Exception:
        print("Missing dependency: pypdf")
        print("Install with: python -m pip install -r requirements.txt")
        return 2

    in_dir = Path(args.input)
    out_dir = Path(args.output)
    if not in_dir.is_dir():
        print(f"Error: input folder not found: {in_dir}")
        return 2

    pdfs = sorted(in_dir.glob("*.pdf"))
    if not pdfs:
        print("No PDFs found to group.")
        return 0

    buckets = ["Assembly", "Machining", "Welding"]
    if not args.dry_run:
        if args.clean and out_dir.exists():
            for b in buckets:
                p = out_dir / b
                if p.is_dir():
                    shutil.rmtree(p)
            for p in [out_dir / "manifest.csv", out_dir / "manifest.txt"]:
                if p.is_file():
                    p.unlink()
        for b in buckets:
            (out_dir / b).mkdir(parents=True, exist_ok=True)

    rows: list[list[str]] = []
    fallback_or_tie = 0

    for pdf in pdfs:
        page_count = get_pdf_page_count(pdf)
        text = read_pdf_text(pdf, max_pages=1 if page_count <= 1 else 50)
        cls = classify(pdf, text)
        subgroup = choose_subgroup(cls, text, page_count)

        if "default" in cls.reason.lower() or "tie" in cls.reason.lower():
            fallback_or_tie += 1

        dest_folder = out_dir / cls.bucket
        if subgroup:
            dest_folder = dest_folder / subgroup

        dest_rel = str(dest_folder.relative_to(out_dir) / pdf.name).replace("\\", "/")

        rows.append(
            [
                pdf.name,
                cls.bucket,
                subgroup or "",
                dest_rel,
                cls.asm or "",
                cls.part or "",
                str(cls.score_welding),
                str(cls.score_machining),
                str(cls.score_assembly),
                cls.reason,
            ]
        )

        dest_path = unique_dest_path(dest_folder / pdf.name)
        if args.dry_run:
            sg = f"/{subgroup}" if subgroup else ""
            print(f"[DRY] {pdf} -> {out_dir / cls.bucket / (subgroup or '') / pdf.name}  ({cls.reason}{sg})")
            continue

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if args.move:
            shutil.move(str(pdf), str(dest_path))
        else:
            shutil.copy2(str(pdf), str(dest_path))

    # If we moved everything out of asm_split, remove the now-empty folder.
    if args.move:
        try:
            if in_dir.is_dir() and not any(in_dir.iterdir()):
                in_dir.rmdir()
        except Exception:
            pass

    if args.dry_run:
        print("\n--- manifest preview (CSV) ---")
        print("filename,bucket,asm,part,score_welding,score_machining,score_assembly,reason")
        for r in rows:
            print(",".join([c.replace(',', ';') for c in r]))
        return 0

    manifest_path = out_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "filename",
                "bucket",
                "subgroup",
                "dest",
                "asm",
                "part",
                "score_welding",
                "score_machining",
                "score_assembly",
                "reason",
            ]
        )
        w.writerows(rows)
    print(f"Wrote: {manifest_path}")

    if fallback_or_tie:
        print(f"Note: {fallback_or_tie} file(s) used fallback/tie rules. Review manifest.csv.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
