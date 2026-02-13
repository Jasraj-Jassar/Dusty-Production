from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


SECTION_RE = re.compile(r"^===\s+(?P<section>.+?)\s+===$")
PAGE_LINE_RE = re.compile(
    r"^Page\s+(?P<pages>\d+(?:-\d+)?)\s+Asm:\s*(?P<asm>\d+(?:,\d+)*)\s+Part:\s*(?P<part>\S+)(?:\s+.*)?$",
    re.IGNORECASE,
)


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


def parse_page_start(pages: str) -> int:
    try:
        return int(pages.split("-", 1)[0])
    except Exception:
        return 10**9


@dataclass(frozen=True)
class Entry:
    section: str
    pages: str
    asm: str
    part: str
    line_index: int  # index in the ops_parts lines list


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Combine Asm PDFs within the same ops section when they share the same Part number."
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
        "--skip-sections",
        default="Assembly,Welding/PowderCoat,Drawing Lookup (Latest Revs)",
        help="Comma-separated ops_parts sections to skip (default: Assembly,Welding/PowderCoat,Drawing Lookup (Latest Revs))",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write PDFs/text, only print actions")
    args = parser.parse_args()

    ops_root = Path(args.ops_root)
    ops_parts = Path(args.ops_parts) if args.ops_parts else (ops_root / "ops_parts.txt")
    if not ops_root.is_dir():
        print(f"Error: ops root not found: {ops_root}")
        return 2
    if not ops_parts.is_file():
        print(f"Error: ops_parts.txt not found: {ops_parts}")
        return 2

    skip = {s.strip().lower() for s in (args.skip_sections or "").split(",") if s.strip()}

    lines = ops_parts.read_text(encoding="utf-8", errors="replace").splitlines()
    current_section = ""
    entries: list[Entry] = []

    for idx, raw in enumerate(lines):
        s = raw.strip()
        msec = SECTION_RE.match(s)
        if msec:
            current_section = msec.group("section").strip()
            continue

        m = PAGE_LINE_RE.match(s)
        if not m:
            continue
        if not current_section or current_section.strip().lower() in skip:
            continue

        asm = m.group("asm").strip()
        if "," in asm:
            # Already combined display line.
            continue
        entries.append(
            Entry(
                section=current_section,
                pages=m.group("pages").strip(),
                asm=asm,
                part=m.group("part").strip(),
                line_index=idx,
            )
        )

    if not entries:
        print("No duplicate candidates found.")
        return 0

    # Group by (section, part)
    grouped: dict[tuple[str, str], list[Entry]] = {}
    for e in entries:
        grouped.setdefault((e.section, e.part), []).append(e)

    combined_groups = 0

    # We'll rewrite ops_parts lines in-memory.
    to_delete_line_indexes: set[int] = set()
    inserts: list[tuple[int, str]] = []  # (insert_at_index, text)

    for (section, part), group in sorted(grouped.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1].lower())):
        if len(group) < 2:
            continue

        # Combine order by page start then asm.
        group_sorted = sorted(group, key=lambda e: (parse_page_start(e.pages), int(e.asm)))
        asms = [e.asm for e in group_sorted]
        pages = [e.pages for e in group_sorted]

        folder = ops_root / Path(section)
        src_pdfs: list[Path] = []
        for a in asms:
            p = folder / f"Asm_{a}.pdf"
            if p.is_file():
                src_pdfs.append(p)

        combined_groups += 1
        out_pdf_base = folder / f"Asm_{'_'.join(asms)}.pdf"
        existing_combined = any(folder.glob(out_pdf_base.stem + "*.pdf"))
        out_pdf = out_pdf_base if not out_pdf_base.exists() else unique_dest_path(out_pdf_base)
        if len(src_pdfs) >= 2:
            print(f"Combine in {folder}: {part}  Asm {','.join(asms)} -> {out_pdf.name}")
        elif existing_combined:
            # PDFs already combined previously; only fix the display line in ops_parts.
            print(f"Update ops_parts: {section} {part}  Asm {','.join(asms)} (combined PDF already exists)")
        else:
            # Cannot combine and nothing existing to represent it.
            combined_groups -= 1
            continue

        # Update ops_parts text.
        insert_at = min(e.line_index for e in group_sorted)
        combined_line = f"Page {','.join(pages)}  Asm: {','.join(asms)}  Part: {part}  (same part diff assembly combined)"
        inserts.append((insert_at, combined_line))
        for e in group_sorted:
            to_delete_line_indexes.add(e.line_index)

        if args.dry_run:
            continue

        if len(src_pdfs) < 2:
            # Display-only update, no PDF work.
            continue

        try:
            from pypdf import PdfReader, PdfWriter
        except Exception:
            print("Missing dependency: pypdf (install requirements.txt)")
            return 2

        writer = PdfWriter()
        for sp in src_pdfs:
            reader = PdfReader(str(sp))
            for page in reader.pages:
                writer.add_page(page)

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_pdf.with_suffix(out_pdf.suffix + ".tmp")
        with tmp.open("wb") as f:
            writer.write(f)
        tmp.replace(out_pdf)

        for sp in src_pdfs:
            try:
                sp.unlink()
            except Exception:
                pass

    if combined_groups == 0:
        print("Combined groups: 0")
        return 0

    if args.dry_run:
        print(f"Combined groups: {combined_groups}")
        return 0

    # Apply deletes and inserts.
    new_lines: list[str] = []
    insert_map: dict[int, list[str]] = {}
    for idx, text in inserts:
        insert_map.setdefault(idx, []).append(text)

    for idx, raw in enumerate(lines):
        if idx in insert_map:
            for t in insert_map[idx]:
                new_lines.append(t)
        if idx in to_delete_line_indexes:
            continue
        new_lines.append(raw)

    ops_parts.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote: {ops_parts}")
    print(f"Combined groups: {combined_groups}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
