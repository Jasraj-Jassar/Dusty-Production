# DustyBot

DustyBot is a Windows-first desktop utility that converts a single job traveler PDF into organized, operation-specific drawing packages for fabrication teams. It standardizes a workflow that is typically manual, repetitive, and error-prone.

The application focuses on three goals:

- Reduce prep time for machining, welding, and assembly packages.
- Keep package generation consistent from job to job.
- Preserve traceable job outputs in a timestamped history structure.

## What DustyBot Automates

From one uploaded traveler PDF, DustyBot can:

- Extract job and part metadata.
- Split traveler content by assembly.
- Group files by operation bucket.
- Validate page totals after grouping.
- Pull latest revision drawings from a search root (default `P:\`).
- Build final operation packages with summary/inspection pages.
- Send grouped packages to print using SumatraPDF.
- Launch integrated Serial Entry automation for Epicor serial creation.

For a full step-by-step breakdown, see [`Core_software/WORKFLOW.md`](Core_software/WORKFLOW.md).

## Repository Layout

```text
DustyBot/
|- Core_software/
|  |- actions/        # Processing pipeline scripts
|  |- gui/            # Desktop UI and preview components (including Serial Entry window)
|  `- WORKFLOW.md     # Detailed workflow documentation
|- Serialnumber Enter Autoamtion/
|  `- serial_entry_automation.py    # Serial Entry automation engine
|- History/           # Runtime job output root (kept in git via .gitkeep)
|- DustyBot.cmd       # Windows bootstrap + launcher
|- launch_serial_automation.cmd     # Optional shortcut launcher (opens DustyBot)
|- requirements.txt   # Root dependency entrypoint
`- README.md
```

## Requirements

- Windows environment (PowerShell + `winget` recommended)
- Python 3.12+ (auto-detected/installed by `DustyBot.cmd`)
- SumatraPDF for printing (auto-installed by `DustyBot.cmd`)
- Google Chrome for serial-entry browser automation

Python dependencies are defined in root `requirements.txt`.

## Quick Start

1. Clone this repository.
2. Open PowerShell in the repo root.
3. Run:

```powershell
.\DustyBot.cmd
```

The launcher will:

- Locate or install Python.
- Create/update `.venv`.
- Install dependencies for both core DustyBot and Serial Entry automation when requirements change.
- Locate or install SumatraPDF.
- Start the GUI app.

From the upload screen, use `Serial Entry` to open the integrated serial automation page.

Optional: you can also run `launch_serial_automation.cmd`, which opens DustyBot and directs you to the same integrated `Serial Entry` feature.

## Data and Environment Configuration

- `DUSTYBOT_DATA_ROOT`: Overrides where `History/` and working files are created.
- `DUSTYBOT_SEARCH_ROOT`: Overrides the revision lookup root (defaults to `P:\`).

If not set, data is written under this repository's `History/` folder.

## History Folder in Git

`History/` is intentionally tracked as an empty folder using `History/.gitkeep`. This keeps the expected runtime directory in the repository without committing generated job output.
