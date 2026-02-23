from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


SECTION_RE = re.compile(r"^===\s+(?P<section>.+?)\s+===$")
OP_ORDER = ("Assembly", "Machining", "Welding")


@dataclass(frozen=True)
class SectionBlock:
    name: str
    lines: list[str]


def parse_ops_parts_sections(ops_parts_path: Path) -> list[SectionBlock]:
    blocks: list[SectionBlock] = []
    current_name: str | None = None
    current_lines: list[str] = []

    for raw in ops_parts_path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.rstrip("\r\n")
        m = SECTION_RE.match(s.strip())
        if m:
            if current_name is not None:
                blocks.append(SectionBlock(current_name, current_lines))
            current_name = m.group("section").strip()
            current_lines = []
            continue

        if current_name is None:
            continue
        current_lines.append(s.rstrip())

    if current_name is not None:
        blocks.append(SectionBlock(current_name, current_lines))

    return blocks


def wrap_line(text: str, max_width: float, *, fontname: str, fontsize: float) -> list[str]:
    import fitz

    s = (text or "").strip()
    if not s:
        return [""]

    words = s.split()
    out: list[str] = []
    cur = ""

    def text_width(v: str) -> float:
        return fitz.get_text_length(v, fontname=fontname, fontsize=fontsize)

    for w in words:
        if not cur:
            cur = w
            continue
        candidate = f"{cur} {w}"
        if text_width(candidate) <= max_width:
            cur = candidate
            continue
        out.append(cur)
        cur = w

    if cur:
        out.append(cur)

    # Hard-wrap any rare overlong token.
    fixed: list[str] = []
    for line in out:
        if text_width(line) <= max_width:
            fixed.append(line)
            continue
        piece = ""
        for ch in line:
            trial = piece + ch
            if text_width(trial) <= max_width:
                piece = trial
                continue
            if piece:
                fixed.append(piece)
            piece = ch
        if piece:
            fixed.append(piece)
    return fixed or [""]


def render_operation_summary_pdf(op_name: str, sections: list[SectionBlock], out_pdf: Path) -> int:
    import fitz

    page_w, page_h = 612, 792  # Letter
    margin_x = 42.0
    margin_top = 42.0
    margin_bottom = 42.0
    max_width = page_w - (margin_x * 2.0)

    # kind -> (fontname, fontsize, line_step)
    styles: dict[str, tuple[str, float, float]] = {
        "title": ("helv", 16.0, 22.0),
        "notice": ("helv", 10.5, 14.0),
        "section": ("helv", 11.0, 16.0),
        "line": ("cour", 10.0, 13.0),
        "blank": ("cour", 10.0, 11.0),
    }

    entries: list[tuple[str, str]] = []
    entries.append(("title", f"{op_name} Operations Summary"))
    entries.append(
        (
            "notice",
            (
                "This summary is generated automatically by DustyBot. "
                "Please verify all page, assembly, and part references against "
                "the latest approved job traveler before production or release."
            ),
        )
    )
    entries.append(("blank", ""))

    if not sections:
        entries.append(("line", f"No {op_name.lower()} entries were found in ops_parts.txt."))
    else:
        for idx, block in enumerate(sections):
            if idx > 0:
                entries.append(("blank", ""))
            entries.append(("section", f"=== {block.name} ==="))
            if not block.lines:
                entries.append(("line", "(no entries)"))
                continue
            for raw in block.lines:
                if not raw.strip():
                    entries.append(("blank", ""))
                else:
                    entries.append(("line", raw.strip()))

    doc = fitz.open()
    page = None
    y = margin_top
    page_count = 0

    def new_page(continued: bool) -> None:
        nonlocal page, y, page_count
        page = doc.new_page(width=page_w, height=page_h)
        page_count += 1
        y = margin_top
        if continued:
            page.insert_text(
                (margin_x, y),
                f"{op_name} Operations Summary (continued)",
                fontname="helv",
                fontsize=11.0,
            )
            y += 16.0

    new_page(continued=False)

    for kind, text in entries:
        fontname, fontsize, step = styles[kind]

        if kind == "blank":
            if y + step > (page_h - margin_bottom):
                new_page(continued=True)
            y += step
            continue

        wrapped = wrap_line(text, max_width, fontname=fontname, fontsize=fontsize)
        for line in wrapped:
            if y + step > (page_h - margin_bottom):
                new_page(continued=True)
            page.insert_text((margin_x, y), line, fontname=fontname, fontsize=fontsize)
            y += step

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_pdf)
    doc.close()
    return page_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build per-operation ops_parts summary PDFs for Assembly, Machining, and Welding."
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
        "--out-dir",
        default=None,
        help="Output folder for section cover PDFs (default: <ops-root>/ops_parts_sections_pdf)",
    )
    args = parser.parse_args()

    try:
        import fitz  # noqa: F401
    except Exception:
        print("Missing dependency: PyMuPDF (fitz)")
        print("Install with: python -m pip install -r requirements.txt")
        return 2

    ops_root = Path(args.ops_root)
    if not ops_root.is_dir():
        print(f"Error: ops root not found: {ops_root}")
        return 2

    ops_parts = Path(args.ops_parts) if args.ops_parts else (ops_root / "ops_parts.txt")
    if not ops_parts.is_file():
        print(f"Error: ops_parts.txt not found: {ops_parts}")
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else (ops_root / "ops_parts_sections_pdf")

    blocks = parse_ops_parts_sections(ops_parts)
    total_written = 0
    for op in OP_ORDER:
        section_blocks = [
            b for b in blocks if b.name.split("/", 1)[0].strip().lower() == op.lower()
        ]
        out_pdf = out_dir / f"{op}_ops_parts.pdf"
        pages = render_operation_summary_pdf(op, section_blocks, out_pdf)
        print(f"Wrote: {out_pdf} ({pages} pages, {len(section_blocks)} section(s))")
        total_written += 1

    print(f"Generated section cover PDFs: {total_written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
