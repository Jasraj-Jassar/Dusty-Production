from __future__ import annotations

import argparse
import re
from pathlib import Path


LINE_RE = re.compile(
    r"^Page\s+(?P<start>\d+)(?:-(?P<end>\d+))?\s+Asm:\s*(?P<asm>\S+)",
    re.IGNORECASE,
)


def parse_ranges(parts_text: str) -> dict[str, list[tuple[int, int]]]:
    asm_ranges: dict[str, list[tuple[int, int]]] = {}
    for raw in parts_text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("job:", "file:")):
            continue
        match = LINE_RE.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        asm = match.group("asm")
        asm_ranges.setdefault(asm, []).append((start, end))
    return asm_ranges


def iter_pages(ranges: list[tuple[int, int]]) -> list[int]:
    pages: list[int] = []
    for start, end in ranges:
        pages.extend(range(start, end + 1))
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split JobTraveller.pdf by Asm ranges from parts.txt."
    )
    parser.add_argument(
        "--input",
        default=str(Path.cwd() / "insert-traveler" / "JobTraveller.pdf"),
        help="Path to JobTraveller.pdf (default: ./insert-traveler/JobTraveller.pdf)",
    )
    parser.add_argument(
        "--parts",
        default=str(Path.cwd() / "insert-traveler" / "parts.txt"),
        help="Path to parts.txt (default: ./insert-traveler/parts.txt)",
    )
    parser.add_argument(
        "--output",
        default=str(Path.cwd() / "insert-traveler" / "asm_split"),
        help="Output folder for split PDFs (default: ./insert-traveler/asm_split)",
    )
    args = parser.parse_args()

    try:
        from pypdf import PdfReader, PdfWriter
    except Exception:
        print("Missing dependency: pypdf")
        print("Install with: python -m pip install -r requirements.txt")
        return 2

    pdf_path = Path(args.input)
    parts_path = Path(args.parts)
    output_dir = Path(args.output)

    if not pdf_path.is_file():
        print(f"Error: PDF not found: {pdf_path}")
        return 2
    if not parts_path.is_file():
        print(f"Error: parts.txt not found: {parts_path}")
        return 2

    parts_text = parts_path.read_text(encoding="utf-8", errors="replace")
    asm_ranges = parse_ranges(parts_text)
    if not asm_ranges:
        print("No Asm ranges found in parts.txt.")
        return 0

    reader = PdfReader(str(pdf_path))
    output_dir.mkdir(parents=True, exist_ok=True)

    for asm, ranges in asm_ranges.items():
        pages = iter_pages(ranges)
        writer = PdfWriter()
        for page_num in pages:
            idx = page_num - 1
            if 0 <= idx < len(reader.pages):
                writer.add_page(reader.pages[idx])
        if not writer.pages:
            continue
        out_path = output_dir / f"Asm_{asm}.pdf"
        with out_path.open("wb") as f:
            writer.write(f)
        print(f"Wrote {out_path} ({len(writer.pages)} page(s))")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
