# 🗂️ Bulk File Renamer

> **Day 1 / 30 — Daily Coding Project** · Python · Lightweight Automation

A fast, safe command-line tool to rename hundreds of files at once — vacation photos, scanned documents, downloaded episodes, invoice PDFs — with a **preview before anything changes**, a **progress bar**, automatic **conflict detection** and a **one-command undo**.

Zero dependencies: it uses only the Python standard library.

## ✨ Features

| | |
|---|---|
| 🔢 **Templates & numbering** | `trip_{n:03}` → `trip_001.jpg`, `trip_002.jpg` … (natural sort: `img2` before `img10`) |
| 🔁 **Find & replace / regex** | `--replace " " "_"`, `--regex "^(\d{4})(\d{2})" "\1-\2"` |
| 🔠 **Case conversion** | `lower`, `upper`, `title`, `snake_case`, `kebab-case` |
| ➕ **Prefix / suffix / extension** | `--prefix 2026_`, `--suffix _final`, `--ext jpg`, `--lower-ext` |
| 👀 **Preview & dry run** | Every change is listed before you confirm; `--dry-run` changes nothing |
| 🛡️ **Conflict handling** | Detects duplicate targets and existing files; `--on-conflict abort / skip / suffix` |
| 🔄 **Safe execution** | Two-phase renaming handles swaps (`a↔b`) and case-only renames; automatic rollback if anything fails |
| ↩️ **Undo** | Every run writes a small log; `--undo` restores the original names |
| 📊 **Progress bar & colours** | Live progress on large folders; respects `NO_COLOR` and `--no-color` |

## 📋 Prerequisites

- **Python 3.8+** (check with `python --version`)
- No packages to install. Optional, for running the tests the pytest way: `pip install pytest`

```bash
git clone https://github.com/Frtsnts10/30-days-code-challenge.git
cd 30-days-code-challenge/day01-bulk-file-renamer
python bulk_renamer.py --help
```

> On macOS/Linux you can also run `chmod +x bulk_renamer.py` and call `./bulk_renamer.py`.

## 🚀 How to Run

```
python bulk_renamer.py [DIRECTORY] [options]
```

`DIRECTORY` defaults to the current folder. Without `--yes`, the tool shows a preview and asks before renaming.

### 1. Number vacation photos

```bash
python bulk_renamer.py ~/Pictures/Bali -g "*.jpg" -i -t "bali_{n:03}" --lower-ext
```

```
Bulk Renamer v1.0.0  ·  12 file(s) in /home/you/Pictures/Bali

  IMG_1.JPG → bali_001.jpg
  IMG_2.JPG → bali_002.jpg
  IMG_3.JPG → bali_003.jpg
  IMG_4.JPG → bali_004.jpg
  … and 8 more (use --limit 0 to show all)

12 to rename
Rename 12 file(s)? [y/N] y
Renaming ██████████████████████████████ 100.0% (24/24)
✔ Renamed 12 file(s) in 0.01s.
  Undo anytime with: python bulk_renamer.py "/home/you/Pictures/Bali" --undo
```

### 2. Clean up messy document names

```bash
python bulk_renamer.py ~/Documents -g "*.pdf" -i --clean --case kebab --lower-ext --dry-run
```

```
  Laporan  Bulanan  Final.PDF → laporan-bulanan-final.pdf

1 to rename
Dry run — no files were changed.
```

### 3. Reformat dates with a regex

```bash
python bulk_renamer.py ./scans --regex "^(\d{4})(\d{2})(\d{2})" "\1-\2-\3"
# 20260115_invoice.pdf → 2026-01-15_invoice.pdf
```

### 4. Prefix files with their modified date, recursively

```bash
python bulk_renamer.py ./notes -r -t "{date}_{name}"
# meeting.md → 2026-11-21_meeting.md
```

### 5. Undo the last rename

```bash
python bulk_renamer.py ~/Pictures/Bali --undo
```

## ⚙️ Options

**Template fields:** `{name}` original name (without extension) · `{n}` number (supports `{n:03}`) · `{ext}` extension · `{date}` modified date `YYYY-MM-DD` · `{parent}` folder name

| Option | Description |
|---|---|
| `-g, --glob PATTERN` | File pattern, repeatable (`-g "*.jpg" -g "*.png"`). Default `*` |
| `-r, --recursive` | Include sub-folders |
| `-i, --ignore-case` | Case-insensitive pattern matching (`*.jpg` also matches `.JPG`) |
| `--hidden` | Include hidden files/folders |
| `-t, --template` | New name template |
| `--start`, `--step` | First number and increment for `{n}` |
| `--replace OLD NEW` | Replace text (repeatable) |
| `--regex PATTERN REPL` | Regex substitution (`\1` for groups) |
| `--clean` | Collapse repeated spaces and trim |
| `--case` | `lower` · `upper` · `title` · `snake` · `kebab` |
| `--prefix`, `--suffix` | Add text at the start / before the extension |
| `--ext EXT` | Change extension (`--ext ""` removes it) |
| `--lower-ext` | Lowercase the extension |
| `-n, --dry-run` | Preview only |
| `-y, --yes` | Skip the confirmation prompt (needed in scripts / CI) |
| `--on-conflict` | `abort` (default) · `skip` · `suffix` → `name (1).jpg` |
| `--limit N` | Preview lines to show, `0` = all |
| `--undo [LOG]` | Undo the latest rename in `DIRECTORY`, or a given log file |
| `--no-log`, `--no-progress`, `--no-color` | Turn off the undo log, progress bar or colours |

Operations are applied in this order: template → replace → regex → clean → case → prefix/suffix → extension. Text operations act on the name only; the extension is kept unless you use `--ext`/`--lower-ext`.

## 🛡️ Safety notes

- Nothing is renamed until you confirm (or pass `--yes`).
- Characters that are invalid in file names (`<>:"/\|?*`) are replaced with `_`; names that would become empty are skipped.
- If two files would get the same name, the run stops before touching anything (unless you choose `skip` or `suffix`).
- Undo logs are stored as hidden `.bulk-rename-YYYYMMDD-HHMMSS.json` files in the target folder and are ignored by the renamer itself.

## 🧪 Tests

```bash
python -m unittest discover tests -v
# or: pytest
```

## 📁 Project structure

```
day01-bulk-file-renamer/
├── bulk_renamer.py        # the CLI tool (single file, stdlib only)
├── tests/
│   └── test_bulk_renamer.py
├── README.md
└── .gitignore
```

## 📄 License

MIT — see the [LICENSE](../LICENSE) in the repository root.
