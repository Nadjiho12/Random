"""
remove-name.py

Search a directory for files (and optionally folders) whose names contain a
given substring or regex, and rename them by removing or replacing that text.
Standard library only.

Usage:
    python remove-name.py "<directory>" "<text to remove>" [options]

Examples:
    # "report_copy.pdf" -> "report.pdf"
    python remove-name.py "C:\\Users\\you\\Downloads" "_copy"

    # Preview first — always a good idea
    python remove-name.py . "_copy" --dry-run

    # Replace rather than remove: "IMG_1234.jpg" -> "photo_1234.jpg"
    python remove-name.py ./pics "IMG" --replace "photo"

    # Regex, with capture groups usable in --replace
    python remove-name.py . "^\\d{4}-\\d{2}-\\d{2}[ _-]*" --regex

    # Tidy up leftover separators: "report -  .pdf" -> "report.pdf"
    python remove-name.py . " - draft" --tidy

    # Rename folders too, and auto-number instead of skipping collisions
    python remove-name.py . "_old" --dirs --on-conflict number

Exit codes:
    0  finished (including "nothing matched")
    1  one or more renames failed
    2  bad usage (missing directory, bad regex, cancelled at the prompt, ...)
"""

from __future__ import annotations

import argparse
import os
import re
import sys

DEFAULT_EXCLUDES = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv"}


def build_matcher(text: str, use_regex: bool, ignore_case: bool) -> re.Pattern[str]:
    """Compile the search text into a regex, escaping it unless --regex was given."""
    pattern = text if use_regex else re.escape(text)
    return re.compile(pattern, re.IGNORECASE if ignore_case else 0)


def split_name(name: str) -> tuple[str, str]:
    """Split into (stem, extension), unlike os.path.splitext for dotfiles.

    '.txt' splits to ('', '.txt') here, so a removal that eats the whole stem is
    detectable instead of silently producing a hidden, extension-only file.
    """
    dot = name.rfind(".")
    if dot < 0:
        return name, ""
    return name[:dot], name[dot:]


def tidy_name(name: str) -> str:
    """Clean up separators left behind by a removal: 'a -  _ b .txt' -> 'a - b.txt'."""
    stem, ext = split_name(name)
    stem = re.sub(r"\s+", " ", stem)                 # collapse runs of whitespace
    stem = re.sub(r"[-_ ]*([-_])[-_ ]*", r"\1", stem)  # collapse mixed separator runs
    stem = stem.strip(" -_.")
    return stem + ext


def unique_target(dirpath: str, name: str) -> str | None:
    """Return `name` with ' (1)', ' (2)', ... appended until it is free, or None."""
    stem, ext = split_name(name)
    for counter in range(1, 1000):
        candidate = f"{stem} ({counter}){ext}"
        if not os.path.exists(os.path.join(dirpath, candidate)):
            return candidate
    return None


def plan_rename(
    dirpath: str,
    name: str,
    matcher: re.Pattern[str],
    args: argparse.Namespace,
) -> tuple[str | None, str | None]:
    """Work out the new name for `name`. Returns (new_name, skip_reason)."""
    if not matcher.search(name):
        return None, None

    new_name = matcher.sub(args.replace, name)
    if args.tidy:
        new_name = tidy_name(new_name)

    if new_name == name:
        return None, None  # nothing actually changed

    stem, _ = split_name(new_name)
    if not new_name.strip() or not stem.strip():
        return None, f"would leave an empty or extension-only name ('{new_name}')"

    if os.sep in new_name or (os.altsep and os.altsep in new_name):
        return None, "result contains a path separator"

    target = os.path.join(dirpath, new_name)
    # On Windows/macOS a case-only rename hits an "exists" check against itself,
    # so compare normalized case before deciding it is a real collision.
    same_entry = os.path.normcase(new_name) == os.path.normcase(name)
    if os.path.exists(target) and not same_entry:
        if args.on_conflict == "skip":
            return None, f"target already exists ('{new_name}')"
        alt = unique_target(dirpath, new_name)
        if alt is None:
            return None, f"target exists and no free numbered name ('{new_name}')"
        new_name = alt

    return new_name, None


def do_rename(old_path: str, new_path: str) -> None:
    """Rename, going through a temp name when only the letter case changes."""
    if os.path.normcase(old_path) == os.path.normcase(new_path) and old_path != new_path:
        temp_path = old_path + ".tmp-rename"
        counter = 0
        while os.path.exists(temp_path):
            counter += 1
            temp_path = f"{old_path}.tmp-rename{counter}"
        os.rename(old_path, temp_path)
        os.rename(temp_path, new_path)
    else:
        os.rename(old_path, new_path)


def collect_entries(root_dir: str, args: argparse.Namespace) -> list[tuple[str, str, bool]]:
    """Return (dirpath, name, is_dir) for every candidate entry, deepest first."""
    excludes = set() if args.no_default_excludes else set(DEFAULT_EXCLUDES)
    excludes.update(args.exclude or [])

    entries: list[tuple[str, str, bool]] = []
    root_depth = root_dir.rstrip(os.sep).count(os.sep)

    # topdown=False so directories are renamed after their contents, which keeps
    # the paths we already collected valid.
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False):
        if any(part in excludes for part in os.path.relpath(dirpath, root_dir).split(os.sep)):
            continue

        depth = dirpath.count(os.sep) - root_depth
        if args.no_recursive and depth > 0:
            continue
        if args.max_depth is not None and depth > args.max_depth:
            continue

        for filename in sorted(filenames):
            entries.append((dirpath, filename, False))
        if args.dirs:
            for dirname in sorted(dirnames):
                if dirname not in excludes:
                    entries.append((dirpath, dirname, True))

    return entries


def confirm(count: int) -> bool:
    if not sys.stdin.isatty():
        print("[!] Not running interactively — re-run with --yes to confirm, or --dry-run to preview.")
        return False
    try:
        return input(f"\nRename {count} item(s)? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def remove_name(root_dir: str, text: str, args: argparse.Namespace) -> int:
    if not os.path.isdir(root_dir):
        print(f"[!] Not a valid directory: {root_dir}")
        return 2

    if not text:
        print("[!] You must provide the text to remove.")
        return 2

    try:
        matcher = build_matcher(text, args.regex, args.ignore_case)
    except re.error as e:
        print(f"[!] Invalid regex: {e}")
        return 2

    planned: list[tuple[str, str, str, bool]] = []  # dirpath, old, new, is_dir
    for dirpath, name, is_dir in collect_entries(root_dir, args):
        new_name, reason = plan_rename(dirpath, name, matcher, args)
        if reason:
            print(f"[SKIP] {name}: {reason}")
        elif new_name:
            planned.append((dirpath, name, new_name, is_dir))

    if not planned:
        print("[!] No matching files found.")
        return 0

    for dirpath, old, new, is_dir in planned:
        kind = "dir " if is_dir else "file"
        prefix = "[DRY] " if args.dry_run else "      "
        location = os.path.relpath(dirpath, root_dir)
        where = "" if location == "." else f"  ({location})"
        print(f"{prefix}{kind}  {old}  ->  {new}{where}")

    if args.dry_run:
        print(f"\nDry run. {len(planned)} item(s) would be renamed.")
        return 0

    if not args.yes and not confirm(len(planned)):
        print("Cancelled. Nothing was renamed.")
        return 2

    renamed = failed = 0
    for dirpath, old, new, _ in planned:
        try:
            do_rename(os.path.join(dirpath, old), os.path.join(dirpath, new))
            print(f"[OK]   {old}  ->  {new}")
            renamed += 1
        except Exception as e:
            print(f"[FAIL] Could not rename '{old}': {e}")
            failed += 1

    print(f"\nDone. {renamed} item(s) renamed, {failed} failed.")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rename files in a directory tree by removing or replacing text in their names.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("directory", help="Directory to search in")
    parser.add_argument("text", help="Text (or regex with --regex) to remove from file names")
    parser.add_argument("--replace", default="", metavar="TEXT",
                        help="Replace the match with this instead of removing it")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Show the renames that would happen and exit")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt")
    parser.add_argument("-e", "--regex", action="store_true",
                        help="Treat the text as a regular expression")
    parser.add_argument("-i", "--ignore-case", action="store_true",
                        help="Match case-insensitively")
    parser.add_argument("-d", "--dirs", action="store_true",
                        help="Rename matching folders as well as files")
    parser.add_argument("--tidy", action="store_true",
                        help="Clean up leftover spaces, dashes and underscores after the removal")
    parser.add_argument("--on-conflict", choices=("skip", "number"), default="skip",
                        help="What to do when the new name is taken (default: skip)")
    parser.add_argument("--no-recursive", action="store_true",
                        help="Only look in the given directory, not in subfolders")
    parser.add_argument("--max-depth", type=int, metavar="N",
                        help="Descend at most N levels below the given directory")
    parser.add_argument("--exclude", action="append", metavar="DIR",
                        help="Folder name to skip (repeatable)")
    parser.add_argument("--no-default-excludes", action="store_true",
                        help=f"Also search inside {', '.join(sorted(DEFAULT_EXCLUDES))}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return remove_name(args.directory, args.text, args)


if __name__ == "__main__":
    sys.exit(main())
