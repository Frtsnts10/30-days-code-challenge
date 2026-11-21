#!/usr/bin/env python3
"""
bulk-renamer — rename many files at once, safely.

Features
  * Preview of every change before anything is touched (plus --dry-run)
  * Templates with sequential numbering: "holiday_{n:03}"
  * Find/replace, regex substitution, case conversion, prefix/suffix
  * Extension changes, whitespace cleanup, invalid-character sanitising
  * Conflict detection with abort / skip / auto-suffix strategies
  * Two-phase renaming (handles swaps and case-only renames) with rollback
  * Progress bar, coloured output and one-command undo

Pure Python standard library, Python 3.8+.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import string
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

LOG_PREFIX = ".bulk-rename-"
LOG_SUFFIX = ".json"
UNDONE_SUFFIX = ".undone.json"
TEMPLATE_FIELDS = ("name", "n", "ext", "date", "parent")
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS = os.name == "nt"
CASE_INSENSITIVE_FS = sys.platform.startswith(("win", "darwin"))
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}
MAX_NAME_BYTES = 255

Op = Tuple[Path, Path]


class RenameError(Exception):
    """A friendly, user-facing error."""


# --------------------------------------------------------------------------- #
# Terminal output helpers
# --------------------------------------------------------------------------- #
class Color:
    enabled = (
        sys.stdout.isatty()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("TERM") != "dumb"
    )

    @classmethod
    def wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text


def red(t: str) -> str:
    return Color.wrap("31", t)


def green(t: str) -> str:
    return Color.wrap("32", t)


def yellow(t: str) -> str:
    return Color.wrap("33", t)


def cyan(t: str) -> str:
    return Color.wrap("36", t)


def bold(t: str) -> str:
    return Color.wrap("1", t)


def dim(t: str) -> str:
    return Color.wrap("2", t)


def configure_stdio() -> None:
    """Never crash on consoles that can't print the arrows / block characters."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    if WINDOWS:
        os.system("")  # enables ANSI escape processing on Windows 10+ consoles


class ProgressBar:
    """A dependency-free progress bar written to stderr (only on a TTY)."""

    def __init__(self, total: int, enabled: bool = True, label: str = "Renaming") -> None:
        self.total = max(total, 1)
        self.count = 0
        self.label = label
        self.enabled = enabled and sys.stderr.isatty()
        self._last_draw = 0.0
        self._drawn = False

    def update(self, item: str = "") -> None:
        self.count += 1
        if not self.enabled:
            return
        now = time.monotonic()
        if self.count < self.total and now - self._last_draw < 0.04:
            return
        self._last_draw = now
        columns = shutil.get_terminal_size((80, 20)).columns
        pct = min(self.count / self.total, 1.0)
        bar_width = max(10, min(30, columns - 50))
        filled = int(bar_width * pct)
        bar = "█" * filled + "░" * (bar_width - filled)
        head = f"{self.label} {bar} {pct * 100:5.1f}% ({self.count}/{self.total}) "
        room = max(columns - len(head) - 1, 0)
        tail = item[:room].ljust(room)
        colored = f"{self.label} {cyan(bar)} {bold(f'{pct * 100:5.1f}%')} ({self.count}/{self.total}) "
        sys.stderr.write("\r" + colored + dim(tail))
        sys.stderr.flush()
        self._drawn = True

    def close(self) -> None:
        if self._drawn:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._drawn = False


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #
def natural_key(text: str) -> list:
    """Sort 'img2' before 'img10'."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def path_key(path: Path) -> str:
    text = str(path)
    return text.casefold() if CASE_INSENSITIVE_FS else text


def is_log_file(name: str) -> bool:
    return name.startswith(LOG_PREFIX) and name.endswith(LOG_SUFFIX)


def collect_files(
    root: Path,
    patterns: Sequence[str],
    recursive: bool = False,
    include_hidden: bool = False,
    ignore_case: bool = False,
) -> Tuple[List[Path], List[str]]:
    """Return (matching files sorted naturally, warnings)."""
    if not root.exists():
        raise RenameError(f"Directory not found: {root}")
    if not root.is_dir():
        raise RenameError(f"Not a directory: {root}")

    warnings: List[str] = []
    pats = [p.casefold() for p in patterns] if ignore_case else list(patterns)

    def matches(name: str) -> bool:
        candidate = name.casefold() if ignore_case else name
        return any(fnmatch.fnmatchcase(candidate, p) for p in pats)

    def on_error(err: OSError) -> None:
        warnings.append(f"Cannot read {err.filename}: {err.strerror}")

    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        dirnames.sort(key=natural_key)
        for fname in filenames:
            if is_log_file(fname) or fname.endswith(".renaming"):
                continue
            if not include_hidden and fname.startswith("."):
                continue
            if not matches(fname):
                continue
            full = Path(dirpath) / fname
            if full.is_file():
                found.append(full)
        if not recursive:
            break

    found.sort(key=lambda p: natural_key(str(p.relative_to(root))))
    return found, warnings


# --------------------------------------------------------------------------- #
# Name transformation
# --------------------------------------------------------------------------- #
def validate_template(template: str) -> None:
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise RenameError(f"Invalid template {template!r}: {exc}") from None
    for _literal, field, spec, _conv in parsed:
        if field is None:
            continue
        if field == "":
            raise RenameError("Template fields must be named, e.g. {n} or {name} — empty {} is not allowed.")
        if field not in TEMPLATE_FIELDS:
            allowed = ", ".join("{" + f + "}" for f in TEMPLATE_FIELDS)
            raise RenameError(f"Unknown template field {{{field}}}. Allowed fields: {allowed}")
        if spec and "{" in spec:
            raise RenameError("Nested fields inside a format spec are not supported.")
    try:
        template.format(name="sample", n=1, ext="txt", date="2026-01-01", parent="folder")
    except (ValueError, TypeError) as exc:
        raise RenameError(f"Invalid template {template!r}: {exc}") from None


def split_words(text: str) -> List[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [w for w in re.split(r"[^0-9A-Za-zÀ-￿]+", text) if w]


def apply_case(text: str, mode: str) -> str:
    if mode == "lower":
        return text.lower()
    if mode == "upper":
        return text.upper()
    if mode == "title":
        return text.title()
    if mode == "snake":
        return "_".join(w.lower() for w in split_words(text))
    if mode == "kebab":
        return "-".join(w.lower() for w in split_words(text))
    raise RenameError(f"Unknown case mode: {mode}")


def transform_name(path: Path, number: int, args: argparse.Namespace) -> str:
    """Compute the new file name (stem + extension) for one file."""
    stem, ext = path.stem, path.suffix

    if args.template:
        try:
            mdate = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        except OSError:
            mdate = datetime.now().strftime("%Y-%m-%d")
        stem = args.template.format(
            name=stem, n=number, ext=ext.lstrip("."), date=mdate, parent=path.parent.name
        )

    for old, new in args.replace or []:
        stem = stem.replace(old, new)

    if args.regex_compiled is not None:
        stem = args.regex_compiled.sub(args.regex[1], stem)

    if args.clean:
        stem = re.sub(r"\s+", " ", stem).strip()

    if args.case:
        stem = apply_case(stem, args.case)

    stem = f"{args.prefix}{stem}{args.suffix}"
    if not stem.strip():
        raise ValueError("the new name would be empty (only the extension would be left)")

    if args.ext is not None:
        new_ext = args.ext.strip().lstrip(".")
        ext = f".{new_ext}" if new_ext else ""
    if args.lower_ext:
        ext = ext.lower()

    return stem + ext


def sanitize(name: str) -> str:
    return INVALID_CHARS.sub("_", name)


def name_problem(name: str) -> Optional[str]:
    if not name or name in {".", ".."} or not name.strip():
        return "the new name would be empty"
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        return f"the new name is longer than {MAX_NAME_BYTES} bytes"
    if WINDOWS:
        if name.endswith((" ", ".")):
            return "Windows names cannot end with a space or a dot"
        if name.split(".")[0].upper() in WINDOWS_RESERVED:
            return f"'{name}' is a reserved name on Windows"
    return None


# --------------------------------------------------------------------------- #
# Planning & conflict resolution
# --------------------------------------------------------------------------- #
class Plan:
    def __init__(self) -> None:
        self.ops: List[Op] = []
        self.unchanged: List[Path] = []
        self.errors: List[Tuple[Path, str]] = []
        self.conflicts: List[Tuple[Path, Path, str]] = []
        self.skipped: List[Tuple[Path, Path, str]] = []
        self.suffixed: int = 0


def free_alternative(dst: Path, claimed: Dict[str, str], moving: set) -> Path:
    stem, ext = dst.stem, dst.suffix
    for i in range(1, 100_000):
        candidate = dst.with_name(f"{stem} ({i}){ext}")
        key = path_key(candidate)
        if key in claimed:
            continue
        if os.path.lexists(candidate) and key not in moving:
            continue
        return candidate
    raise RenameError(f"Could not find a free name for {dst.name}")


def resolve_conflicts(ops: List[Op], stationary: List[Path], mode: str, plan: Plan) -> List[Op]:
    """Check targets against each other and the disk; apply the conflict strategy."""
    stationary = list(stationary)
    while True:
        moving = {path_key(src) for src, _ in ops}
        claimed: Dict[str, str] = {path_key(p): f"existing file '{p.name}'" for p in stationary}
        accepted: List[Op] = []
        dropped: List[Tuple[Path, Path, str]] = []

        for src, dst in ops:
            key = path_key(dst)
            reason = None
            if key in claimed:
                reason = f"target clashes with {claimed[key]}"
            elif os.path.lexists(dst) and key not in moving:
                reason = "a file with that name already exists"

            if reason is None:
                claimed[key] = f"'{src.name}' (also renamed to '{dst.name}')"
                accepted.append((src, dst))
            elif mode == "suffix":
                alt = free_alternative(dst, claimed, moving)
                claimed[path_key(alt)] = f"'{src.name}' (renamed to '{alt.name}')"
                accepted.append((src, alt))
                plan.suffixed += 1
            else:
                dropped.append((src, dst, reason))

        if mode == "abort" or not dropped or mode == "suffix":
            if mode == "abort":
                plan.conflicts.extend(dropped)
            return accepted

        # skip mode: dropped files stay where they are, which may create new clashes
        plan.skipped.extend(dropped)
        stationary.extend(src for src, _, _ in dropped)
        dropped_keys = {path_key(src) for src, _, _ in dropped}
        ops = [(s, d) for s, d in ops if path_key(s) not in dropped_keys]


def build_plan(files: List[Path], args: argparse.Namespace) -> Plan:
    plan = Plan()
    proposed: List[Op] = []
    for index, path in enumerate(files):
        number = args.start + index * args.step
        try:
            new_name = sanitize(transform_name(path, number, args))
        except (re.error, IndexError, ValueError, KeyError) as exc:
            plan.errors.append((path, str(exc) if isinstance(exc, ValueError) else f"could not build new name: {exc}"))
            continue
        problem = name_problem(new_name)
        if problem:
            plan.errors.append((path, problem))
            continue
        if new_name == path.name:
            plan.unchanged.append(path)
            continue
        proposed.append((path, path.with_name(new_name)))

    stationary = plan.unchanged + [p for p, _ in plan.errors]
    plan.ops = resolve_conflicts(proposed, stationary, args.on_conflict, plan)
    return plan


# --------------------------------------------------------------------------- #
# Execution (two-phase with rollback)
# --------------------------------------------------------------------------- #
def execute(ops: List[Op], show_progress: bool = True, label: str = "Renaming") -> None:
    """Rename via temporary names so swaps / chains / case-only changes work.

    If anything fails, every file is moved back to its original name.
    """
    token = uuid.uuid4().hex[:8]
    staged: List[Tuple[Path, Path, Path]] = []  # (src, tmp, dst)
    finished = 0
    bar = ProgressBar(len(ops) * 2, enabled=show_progress, label=label)
    try:
        for i, (src, dst) in enumerate(ops):
            tmp = src.with_name(f".{token}-{i}.renaming")
            os.rename(src, tmp)
            staged.append((src, tmp, dst))
            bar.update(src.name)
        for src, tmp, dst in staged:
            if os.path.lexists(dst):
                raise RenameError(f"'{dst}' appeared while renaming (another program?)")
            os.rename(tmp, dst)
            finished += 1
            bar.update(dst.name)
        bar.close()
    except (OSError, RenameError, KeyboardInterrupt) as exc:
        bar.close()
        failures = rollback(staged, finished)
        if isinstance(exc, KeyboardInterrupt):
            reason = "interrupted by user"
        elif isinstance(exc, OSError):
            reason = f"{exc.strerror or exc} ({exc.filename})"
        else:
            reason = str(exc)
        msg = f"Rename failed: {reason}."
        if failures:
            msg += "\nRollback could not restore these files (please check manually):\n" + "\n".join(
                f"  {f}" for f in failures
            )
        else:
            msg += " All files were restored to their original names."
        raise RenameError(msg) from None


def rollback(staged: List[Tuple[Path, Path, Path]], finished: int) -> List[str]:
    failures: List[str] = []
    for i, (src, tmp, dst) in reversed(list(enumerate(staged))):
        current = dst if i < finished else tmp
        try:
            os.rename(current, src)
        except OSError as exc:
            failures.append(f"{current} -> {src}: {exc.strerror or exc}")
    return failures


# --------------------------------------------------------------------------- #
# Undo log
# --------------------------------------------------------------------------- #
def write_log(root: Path, ops: List[Op]) -> Optional[Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = root / f"{LOG_PREFIX}{stamp}{LOG_SUFFIX}"
    counter = 1
    while path.exists():
        path = root / f"{LOG_PREFIX}{stamp}-{counter}{LOG_SUFFIX}"
        counter += 1
    data = {
        "tool": "bulk-renamer",
        "version": __version__,
        "created": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "operations": [{"from": str(src), "to": str(dst)} for src, dst in ops],
    }
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path
    except OSError as exc:
        print(yellow(f"⚠ Could not write undo log ({exc.strerror}). Renames succeeded, but undo is unavailable."))
        return None


def find_latest_log(root: Path) -> Path:
    logs = sorted(
        (p for p in root.glob(f"{LOG_PREFIX}*{LOG_SUFFIX}") if not p.name.endswith(UNDONE_SUFFIX)),
        key=lambda p: p.stat().st_mtime,
    )
    if not logs:
        raise RenameError(f"No undo log found in {root}. Nothing to undo.")
    return logs[-1]


def load_log(path: Path) -> Tuple[Path, List[Op]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        root = Path(data["root"])
        ops = [(Path(o["from"]), Path(o["to"])) for o in data["operations"]]
    except FileNotFoundError:
        raise RenameError(f"Undo log not found: {path}") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RenameError(f"Undo log is unreadable or corrupted ({path.name}): {exc}") from None
    return root, ops


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def display(path: Path, root: Path) -> Tuple[str, str]:
    try:
        rel_parent = path.parent.relative_to(root)
        folder = "" if str(rel_parent) == "." else f"{rel_parent}{os.sep}"
    except ValueError:
        folder = f"{path.parent}{os.sep}"
    return folder, path.name


def print_preview(ops: List[Op], root: Path, limit: int) -> None:
    shown = ops if limit <= 0 else ops[:limit]
    for src, dst in shown:
        folder, old = display(src, root)
        print(f"  {dim(folder)}{red(old)} {dim('→')} {green(dst.name)}")
    if limit > 0 and len(ops) > limit:
        print(dim(f"  … and {len(ops) - limit} more (use --limit 0 to show all)"))


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise RenameError("Not running interactively — add --yes to confirm, or --dry-run to preview only.")
    try:
        answer = input(f"{bold(question)} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def has_transform(args: argparse.Namespace) -> bool:
    return any(
        [
            args.template,
            args.replace,
            args.regex,
            args.case,
            args.clean,
            args.prefix,
            args.suffix,
            args.ext is not None,
            args.lower_ext,
        ]
    )


def run_rename(args: argparse.Namespace) -> int:
    if not has_transform(args):
        raise RenameError(
            "Nothing to do — give at least one rename option, e.g. --template \"photo_{n:03}\" "
            "or --replace \" \" \"_\". See --help."
        )
    if args.template:
        validate_template(args.template)
    args.regex_compiled = None
    if args.regex:
        try:
            args.regex_compiled = re.compile(args.regex[0])
        except re.error as exc:
            raise RenameError(f"Invalid regex {args.regex[0]!r}: {exc}") from None

    root = Path(args.directory).expanduser().resolve()
    files, warnings = collect_files(root, args.glob, args.recursive, args.hidden, args.ignore_case)
    for w in warnings:
        print(yellow(f"⚠ {w}"))
    if not files:
        print(yellow(f"No files matched {', '.join(args.glob)} in {root}"))
        return 0

    print(f"{bold('Bulk Renamer')} {dim('v' + __version__)}  ·  {len(files)} file(s) in {cyan(str(root))}\n")
    plan = build_plan(files, args)

    for path, reason in plan.errors:
        folder, name = display(path, root)
        print(yellow(f"  ⚠ skipped {folder}{name}: {reason}"))

    if plan.conflicts:
        print(red(bold(f"✖ {len(plan.conflicts)} naming conflict(s) found — nothing was renamed:")))
        for src, dst, reason in plan.conflicts[:20]:
            print(f"  {red(src.name)} → {dst.name}  {dim('(' + reason + ')')}")
        if len(plan.conflicts) > 20:
            print(dim(f"  … and {len(plan.conflicts) - 20} more"))
        print(dim("\nTip: include {n} or {name} in the template, or use --on-conflict skip|suffix."))
        return 1

    for src, dst, reason in plan.skipped:
        print(yellow(f"  ⚠ skipped {src.name} → {dst.name}: {reason}"))

    if not plan.ops:
        print(green("✔ Nothing to rename — all names already match the requested format."))
        return 0

    print_preview(plan.ops, root, args.limit)
    summary = f"\n{bold(str(len(plan.ops)))} to rename"
    if plan.unchanged:
        summary += f", {len(plan.unchanged)} unchanged"
    if plan.skipped or plan.errors:
        summary += f", {len(plan.skipped) + len(plan.errors)} skipped"
    if plan.suffixed:
        summary += f", {plan.suffixed} given a ' (n)' suffix"
    print(summary)

    if args.dry_run:
        print(cyan("Dry run — no files were changed."))
        return 0

    if not confirm(f"Rename {len(plan.ops)} file(s)?", args.yes):
        print("Cancelled — no files were changed.")
        return 0

    started = time.monotonic()
    execute(plan.ops, show_progress=not args.no_progress)
    elapsed = time.monotonic() - started

    log_path = None if args.no_log else write_log(root, plan.ops)
    print(green(f"✔ Renamed {len(plan.ops)} file(s) in {elapsed:.2f}s."))
    if log_path:
        print(dim(f"  Undo anytime with: python {Path(__file__).name} \"{root}\" --undo"))
    return 0


def run_undo(args: argparse.Namespace) -> int:
    root = Path(args.directory).expanduser().resolve()
    log_path = find_latest_log(root) if args.undo == "latest" else Path(args.undo).expanduser().resolve()
    log_root, recorded = load_log(log_path)

    # Reverse: current name ("to") goes back to the original ("from").
    reverse_ops: List[Op] = [(dst, src) for src, dst in reversed(recorded)]
    moving = {path_key(s) for s, _ in reverse_ops}
    ops: List[Op] = []
    missing: List[Path] = []
    blocked: List[Tuple[Path, Path]] = []
    for current, original in reverse_ops:
        if not os.path.lexists(current):
            missing.append(current)
        elif os.path.lexists(original) and path_key(original) not in moving:
            blocked.append((current, original))
        else:
            ops.append((current, original))

    print(f"{bold('Undo')} {dim(log_path.name)}  ·  {len(recorded)} recorded rename(s)\n")
    for path in missing:
        print(yellow(f"  ⚠ not found (moved or deleted?): {path}"))
    if blocked:
        print(red(bold("✖ Cannot undo — the original names are now taken by other files:")))
        for current, original in blocked:
            print(f"  {current.name} → {original.name}")
        return 1
    if not ops:
        print(yellow("Nothing left to undo."))
        return 1 if missing else 0

    print_preview(ops, log_root, args.limit)
    if args.dry_run:
        print(cyan("\nDry run — no files were changed."))
        return 0
    if not confirm(f"\nRestore {len(ops)} file(s) to their original names?", args.yes):
        print("Cancelled — no files were changed.")
        return 0

    execute(ops, show_progress=not args.no_progress, label="Restoring")
    try:
        log_path.rename(log_path.with_name(log_path.name[: -len(LOG_SUFFIX)] + UNDONE_SUFFIX))
    except OSError:
        pass
    print(green(f"✔ Restored {len(ops)} file(s)."))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bulk_renamer.py",
        description="Rename many files at once — with preview, conflict checks, progress bar and undo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "template fields: {name} original name, {n} number, {ext} extension,\n"
            "                 {date} modified date (YYYY-MM-DD), {parent} folder name\n\n"
            "examples:\n"
            '  %(prog)s ~/Photos -g "*.jpg" -t "bali_{n:03}"\n'
            '  %(prog)s . --replace " " "_" --case lower\n'
            '  %(prog)s docs --regex "^(\\d{4})(\\d{2})(\\d{2})" "\\1-\\2-\\3" --dry-run\n'
            "  %(prog)s ~/Photos --undo"
        ),
    )
    parser.add_argument("directory", nargs="?", default=".", help="folder containing the files (default: current)")

    sel = parser.add_argument_group("selecting files")
    sel.add_argument("-g", "--glob", action="append", metavar="PATTERN",
                     help='file pattern, repeatable (default: "*"), e.g. -g "*.jpg" -g "*.png"')
    sel.add_argument("-r", "--recursive", action="store_true", help="include sub-folders")
    sel.add_argument("-i", "--ignore-case", action="store_true", help="case-insensitive pattern matching")
    sel.add_argument("--hidden", action="store_true", help="include hidden files/folders (starting with '.')")

    tr = parser.add_argument_group("renaming (applied in this order)")
    tr.add_argument("-t", "--template", help='new name template, e.g. "trip_{n:03}" or "{date}_{name}"')
    tr.add_argument("--start", type=int, default=1, help="first number for {n} (default: 1)")
    tr.add_argument("--step", type=int, default=1, help="increment for {n} (default: 1)")
    tr.add_argument("--replace", nargs=2, action="append", metavar=("OLD", "NEW"),
                    help="replace text in the name (repeatable)")
    tr.add_argument("--regex", nargs=2, metavar=("PATTERN", "REPL"), help="regex substitution on the name")
    tr.add_argument("--clean", action="store_true", help="collapse repeated spaces and trim the name")
    tr.add_argument("--case", choices=["lower", "upper", "title", "snake", "kebab"], help="change letter case")
    tr.add_argument("--prefix", default="", help="text to add at the start")
    tr.add_argument("--suffix", default="", help="text to add before the extension")
    tr.add_argument("--ext", help='new extension, e.g. "jpg" (use "" to remove it)')
    tr.add_argument("--lower-ext", action="store_true", help="lowercase the extension (.JPG → .jpg)")

    beh = parser.add_argument_group("behaviour")
    beh.add_argument("-n", "--dry-run", action="store_true", help="preview only, change nothing")
    beh.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    beh.add_argument("--on-conflict", choices=["abort", "skip", "suffix"], default="abort",
                     help="what to do when names clash (default: abort)")
    beh.add_argument("--limit", type=int, default=50, help="max preview lines, 0 = all (default: 50)")
    beh.add_argument("--undo", nargs="?", const="latest", metavar="LOG",
                     help="undo the last rename in DIRECTORY (or a specific log file)")
    beh.add_argument("--no-log", action="store_true", help="don't write an undo log")
    beh.add_argument("--no-progress", action="store_true", help="hide the progress bar")
    beh.add_argument("--no-color", action="store_true", help="disable coloured output")
    beh.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.glob:
        args.glob = ["*"]
    if args.no_color:
        Color.enabled = False
    try:
        if args.undo is not None:
            return run_undo(args)
        return run_rename(args)
    except RenameError as exc:
        print(red("✖ ") + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
