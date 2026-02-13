from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify total pages in asm_split equals JobTraveller.pdf pages."
    )
    parser.add_argument(
        "--input",
        default=str(Path.cwd() / "insert-traveler" / "JobTraveller.pdf"),
        help="Path to JobTraveller.pdf (default: ./insert-traveler/JobTraveller.pdf)",
    )
    parser.add_argument(
        "--split",
        default=str(Path.cwd() / "insert-traveler" / "asm_split"),
        help="Folder with split PDFs (default: ./insert-traveler/asm_split)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Include PDFs in subfolders (useful for ops_grouped)",
    )
    args = parser.parse_args()

    try:
        from pypdf import PdfReader
    except Exception:
        print("Missing dependency: pypdf")
        print("Install with: python -m pip install -r requirements.txt")
        return 2

    main_pdf = Path(args.input)
    split_dir = Path(args.split)

    if not main_pdf.is_file():
        print(f"Error: PDF not found: {main_pdf}")
        return 2
    if not split_dir.is_dir():
        print(f"Error: split folder not found: {split_dir}")
        return 2

    main_pages = len(PdfReader(str(main_pdf)).pages)
    split_files = sorted(split_dir.rglob("*.pdf") if args.recursive else split_dir.glob("*.pdf"))
    if not split_files:
        print("No split PDFs found.")
        return 2

    split_pages = 0
    for pdf in split_files:
        split_pages += len(PdfReader(str(pdf)).pages)

    if split_pages == main_pages:
        print(f"OK: main={main_pages} pages, split total={split_pages} pages.")
        return 0

    print(f"Mismatch: main={main_pages} pages, split total={split_pages} pages.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
