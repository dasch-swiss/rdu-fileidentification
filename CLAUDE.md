# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Python CLI for file format identification and bulk conversion, designed for digital preservation workflows. It wraps [pygfried](https://pypi.org/project/pygfried/) (siegfried), ffmpeg, imagemagick, and LibreOffice to identify file formats via PRONOM UIDs (PUIDs) and convert files according to JSON policy files.

## Commands

All commands use `uv`. Install dependencies with:
```bash
uv sync --no-group dev   # production
uv sync                  # with dev tools (ruff, mypy)
```

**Run the CLI:**
```bash
uv run identify.py path/to/directory         # identify + generate policies
uv run identify.py path/to/directory -iar    # identify, assert integrity, apply, remove tmp
uv run identify.py --help
```

**Lint and type check:**
```bash
just check          # runs lint + typecheck
just lint           # ruff check
just format         # ruff format
just typecheck      # mypy (strict mode)
```

Or directly:
```bash
uv run ruff check .
uv run ruff format .
uv run mypy .
```

**Update PUID definitions** (fetches from nationalarchives.gov.uk):
```bash
uv sync --extra update_fmt && uv run update.py
```

**Docker:**
```bash
just dockerise      # build image + link fidr.sh to PATH
just dasch          # DaSCH-specific: use dasch_policies.json as default, then dockerise
```

## Architecture

### Data Flow

1. `identify.py` — Typer CLI entrypoint; collects the flags (assembling the mode flags into a `Mode`) and delegates to `FileHandler.run(root_folder, mode, ...)`
2. `FileHandler` (`fileidentification/filehandling.py`) — main orchestrator class; holds the processing state (`stack`, `policies`, `ba`, `journal`, `ws`, `mode`)
3. `_build_stack` populates `self.stack` — either reloading an existing `_log.json` or scanning the folder with pygfried; each file becomes an `SfInfo` object
4. `_resolve_policies` sets `self.policies` (JSON keyed by PUID) via the `resolve_policies` module — read from the default location / an external file, or generated
5. Tasks (integrity check, apply policies, convert, move) operate on the stack in sequence. `assert_integrity` and `apply_policies` skip files already marked `status.probed` / `status.applied`, so a re-run against a reloaded `_log.json` doesn't re-process or re-log them

### Key Models (`fileidentification/definitions/models.py`)

- **`SfInfo`** — primary metadata object per file; wraps siegfried output and accumulates its processing log (`processing_logs`), `status`, `media_info`, and derived info; `processed_as` holds the matched PUID. `status` flags drive the lifecycle — `probed`/`applied` gate re-processing on a rerun, `pending` → converted (`dest` set) → `added` or `removed`. `filename` is a portable path relative to `root_folder`, **except** a converted file awaiting move (`dest` set), whose `filename` is its working-dir location relative to `tmp_dir` until `move_tmp` relocates it.
- **`PolicyParams`** — one policy entry: `bin` (ffmpeg/magick/soffice), `accepted`, `target_container`, `processing_args`, `expected` (list of PUIDs to verify output), `remove_original`
- **`BasicAnalytics`** — groups `SfInfo` objects by PUID and tracks duplicates (by MD5)
- **`LogMsg`** — one timestamped log entry (`name`, `msg`, `level`, `timestamp`); `level` is a `LogLevel` (info/warning/error, default info) set by the journal and at explicit error sites.
- **`RunJournal`** — the single record of what happened to each file during a run. `diagnose` writes a diagnostic (appends the message to the `SfInfo`'s `processing_logs`, sets its `level` from the `FDMsg` severity, and buckets the file by severity for the console report, which prints each bucketed file's `processing_logs`); `record_error` records a processing failure (marking the summary error-level); `error_records` returns the `"errors"`-section copies non-destructively (so the console and persisted views can be produced in any order). Thread-safe: `diagnose` and `record_error` hold an internal `threading.Lock`.
- **`Mode`** — flags: `REMOVEORIGINAL`, `VERBOSE`, `STRICT`, `QUIET`
- **`Workspace`** (`fileidentification/workspace.py`) — the single run-scoped path module; a frozen dataclass holding `root_folder` + `tmp_dir`, built once via `Workspace.for_run(root, tmp_dir)` (validates root, normalizes a single-file target, creates the tmp dir). Derives `logjson` (`_log.json`), `poljson` (`_policies.json`), and `report_json(ymd)`, plus `abs_path` / `working_dir` / `removed_dest`. `write_logs` targets `ws.logjson` by default; the read-only `inspect` mode passes `ws.report_json(ymd)` so its report stays separate from a processing run.

### Task Modules (`fileidentification/tasks/`)

| Module | Responsibility |
|---|---|
| `inspection.py` | `inspect_file` / `assert_file_integrity` — probes files via ffmpeg/magick, detects corruption and extension mismatches |
| `policies.py` | `apply_policy` — marks `SfInfo.status.pending = True` for files that need conversion |
| `conversion.py` | `convert_file` — runs the converter, then re-identifies output with pygfried to verify |
| `os_tasks.py` | `move_tmp`, `remove` — filesystem operations, moving converted files to destination and quarantining removed ones |
| `console_output.py` | Rich/typer formatted console output (tables, diagnostics) |

### Wrappers (`fileidentification/wrappers/`)

- `ffmpeg.py` / `imagemagick.py` — media info extraction helpers

Running a converter (building and running the shell command, writing to a working subdirectory `__fileidentification/<filename>_<pathhash[:6]>/` — the hash is of the file's relative path, so identical files at different paths don't collide) lives in `tasks/conversion.py` (`_run_tool`), with the per-bin command shape owned by the `MediaTool` seam in `wrappers/tools.py`.

### Definitions (`fileidentification/definitions/`)

- `fmt_info.json` — maps PUID → `{name, file_extensions[], mime}`, used for display and blank policy generation; regenerated by `update.py`. Loaded via `settings.py` into `FMT_INFO: dict[str, FmtEntry]` (typed access: `.name` / `.file_extensions` / `.mime`)
- `default_policies.json` — default conversion rules applied when generating policies
- `settings.py` — constants, enums (`Bin`, `FDMsg`, `FPMsg`, etc.), paths, and `MAX_WORKERS`

### Concurrency

`inspect`, `assert_integrity`, `apply_policies`, and `convert` in `FileHandler` all run file processing in parallel using `ThreadPoolExecutor`. The work is subprocess-bound (ffprobe, magick, ffmpeg, soffice), so threading is effective. `MAX_WORKERS` in `settings.py` controls the pool size (default: 4).

Thread-safety notes:
- Each `SfInfo` is owned by exactly one worker — no locking needed on the object itself.
- `RunJournal` uses an internal lock for `diagnose` and `record_error`; always use these methods rather than mutating `diagnostics` / `processing_errors` directly.
- `FileHandler._stack_lock` protects `self.stack.append` in `convert` (converted `SfInfo` objects appended from workers).
- `os_tasks.remove` uses `mkdir(exist_ok=True)` to avoid races when multiple files in the same subdirectory are removed concurrently.

### Policies Directory (`policies/`)

Contains alternative policy sets (e.g. `dasch_policies.json`). `just setdasch` copies one to `default_policies.json`.

### Temporary Files

`Workspace.for_run` creates the tmp dir: `__fileidentification/` inside the target directory, `<parent>/<stem>/` for a single-file target, or a custom `--tmp-dir` (which may be on another volume). It holds:
- `_policies.json` — generated or read-in policies
- `_log.json` — cumulative log of all processing (appended across runs)
- `<yymmdd>_report.json` — the read-only `--inspect` mode's findings; `--inspect` also writes the bare scanned inventory to `_log.json` up front so a rerun reloads it instead of rescanning
- `_REMOVED/` — corrupt or removed files
- `<filename>_<pathhash[:6]>/` — per-file conversion working directories with converted file
