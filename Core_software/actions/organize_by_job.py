import argparse
import re
import shutil
from datetime import datetime
from pathlib import Path


def _unique_path(dest_path: Path) -> Path:
    """Return a non-existing path by appending (1), (2), ... when needed."""
    if not dest_path.exists():
        return dest_path
    stem = dest_path.stem
    suffix = dest_path.suffix
    i = 1
    while True:
        candidate = dest_path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def iter_pdfs(folder: Path, recursive: bool):
    if recursive:
        yield from folder.rglob("*.pdf")
    else:
        yield from folder.glob("*.pdf")


def safe_folder_name(name: str) -> str:
    # Keep it Windows-safe.
    return re.sub(r"[<>:\"/\\\\|?*]", "_", name).strip().rstrip(".")


def move_pdf(pdf: Path, dest_folder: Path):
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest_path = _unique_path(dest_folder / pdf.name)
    shutil.move(str(pdf), str(dest_path))
    return dest_path


def get_job_from_parts_txt(parts_txt: Path) -> str | None:
    if not parts_txt.is_file():
        return None
    for line in parts_txt.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip().lower().startswith("job:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _try_rmdir_if_empty(p: Path) -> bool:
    try:
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
            return True
    except Exception:
        pass
    return False


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty directories bottom-up under root (best-effort)."""
    if not root.is_dir():
        return
    try:
        for p in sorted(root.rglob("*"), key=lambda x: len(str(x)), reverse=True):
            _try_rmdir_if_empty(p)
    except Exception:
        pass
    _try_rmdir_if_empty(root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive a workspace into History/Job - <job> - <timestamp>."
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Include PDFs in subfolders",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path.cwd()),
        help="Repo root that contains History/ (default: cwd)",
    )
    parser.add_argument(
        "--workspace-root",
        default=str(Path.cwd()),
        help="Workspace root that contains insert-traveler/, ops_grouped/, final_packages/ (default: cwd)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    ws_root = Path(args.workspace_root).resolve()

    insert_traveler = ws_root / "insert-traveler"
    sources = [insert_traveler]

    job = get_job_from_parts_txt(insert_traveler / "parts.txt")
    if not job:
        print(f"Error: Could not find Job in {insert_traveler}\\parts.txt")
        return 2

    history_root = repo_root / "History"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_folder = history_root / safe_folder_name(f"Job - {job} - {stamp}")

    moved = 0
    for src in sources:
        if not src.exists():
            continue
        for pdf in iter_pdfs(src, args.recursive):
            move_pdf(pdf, dest_folder)
            moved += 1

    asm_split = insert_traveler / "asm_split"
    if asm_split.is_dir():
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_asm = dest_folder / "asm_split"
        if dest_asm.exists():
            shutil.rmtree(dest_asm)
        shutil.move(str(asm_split), str(dest_asm))
        moved += 1

    ops_grouped = ws_root / "ops_grouped"
    legacy_ops_grouped = insert_traveler / "ops_grouped"
    if ops_grouped.is_dir() or legacy_ops_grouped.is_dir():
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_ops = dest_folder / "ops_grouped"
        if dest_ops.exists():
            shutil.rmtree(dest_ops)
        shutil.move(str(ops_grouped if ops_grouped.is_dir() else legacy_ops_grouped), str(dest_ops))
        moved += 1

    final_packages = ws_root / "final_packages"
    if final_packages.is_dir():
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_final = dest_folder / "final_packages"
        dest_final.mkdir(parents=True, exist_ok=True)

        # Prefer moving only this job's outputs (build_final_package.py writes:
        # final_packages/DrawingPackage - <job> - <stamp>/...).
        wanted_prefix = f"DrawingPackage - {job} -"
        children = [p for p in final_packages.iterdir()]
        wanted = [p for p in children if p.name.startswith(wanted_prefix)]
        if not wanted and len(children) == 1:
            # Best-effort: if there is only one output folder, assume it is for this job.
            wanted = children

        # Move contents, not the container folder, so the History layout is stable.
        for child in sorted(wanted, key=lambda p: p.name.lower()):
            try:
                target = _unique_path(dest_final / child.name)
                shutil.move(str(child), str(target))
                moved += 1
            except Exception:
                # Best-effort: keep archiving other items.
                pass
        try:
            if not any(final_packages.iterdir()):
                final_packages.rmdir()
        except Exception:
            pass

    parts_txt = insert_traveler / "parts.txt"
    if parts_txt.is_file():
        dest_folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(parts_txt), str(dest_folder / parts_txt.name))
        moved += 1

    # Streamline: clean up empty temp folders after archiving.
    _prune_empty_dirs(ws_root / "insert-traveler")
    _prune_empty_dirs(ws_root / "ops_grouped")
    _prune_empty_dirs(ws_root / "final_packages")

    print(f"Job folder: {dest_folder}")
    print(f"Repo root: {repo_root}")
    print(f"Workspace root: {ws_root}")
    print(f"Moved: {moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
