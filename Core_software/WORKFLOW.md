# DustyBot Workflow (Step by Step)

Runtime location:
- On upload, GUI creates one working folder:
- `History/Job - <job> - <timestamp>/`
- This means progress is saved in `History/` immediately while the package is being processed.

1. Upload `pdf`
  - GUI copies it to `insert-traveler/file.pdf`

2. Extract Parts
   - Runs `actions/extract_parts.py`
   - Output: `insert-traveler/parts.txt`
   - Includes: `Job`, `File`, `For Stock`, `For Order`, and per-page `Asm/Part` ranges

3. Split By Asm
   - Runs `actions/split_by_asm.py`
   - Output folder: `insert-traveler/asm_split/` (`Asm_<n>.pdf`)

4. Group By Operation
   - Runs `actions/group_by_operation.py --move`
   - Moves PDFs from `insert-traveler/asm_split/` into:
     - `ops_grouped/Assembly/`
     - `ops_grouped/Machining/`
     - `ops_grouped/Welding/`
   - Removes `insert-traveler/asm_split/` if it becomes empty after moving
   - Output: `ops_grouped/manifest.csv`

5. Check Page Totals (Grouped)
   - Runs `actions/split_check.py --split ops_grouped --recursive`
   - Verifies total pages in grouped PDFs equals total pages in `JobTraveller.pdf`

6. Split parts.txt By Operation
   - Runs `actions/split_parts_by_operation.py`
   - Output:
     - `ops_grouped/ops_parts.txt` (single combined view, grouped by operation folders)

7. Combine Duplicate Asms By Part
   - Runs `actions/combine_asms_by_part.py`
   - If two Asm PDFs in the same operation folder share the same `Part:`, they get merged into one `Asm_<a>_<b>.pdf`
   - Updates `ops_grouped/ops_parts.txt`
   - Skips `Assembly` by default

8. Pull Latest Part Revisions (From Drive)
   - Runs `actions/pull_latest_revs.py`
   - Reads: `ops_grouped/ops_parts.txt` and `ops_grouped/manifest.csv`
   - Searches a drive/folder (default `P:\\`, override via `DUSTYBOT_SEARCH_ROOT`)
   - By default skips `Assembly` and `PowderCoat` (`--skip-buckets`, `--skip-subgroups`)
   - For a part like `10-0845`, if it finds `10-0845-2` and `10-0845-1`, it picks `-2`
   - Copies the picked file into the same folder as that `Asm_<n>.pdf`
   - Output: `ops_grouped/rev_pull_manifest.csv`

9. Job (Start Next)
   - GUI button: `Job`
   - Resets the app for next import
   - No additional archive move is performed
