"""Reassemble the split bundle and check it arrived intact.

Standard library only, so it runs on the target with nothing installed.

    python rejoin.py                 look beside this script, then in the
                                     current folder
    python rejoin.py C:\\path\\to\\parts   look there instead

Deliberately forgiving about names. Parts leave here as ``....zip.001`` but
arrive with whatever the transfer did to them -- browsers append ``.bin`` or
``.download`` to extensions they do not recognise, and Windows adds ``ial (1)``
to a name it has seen before. Any file carrying a three-digit index is treated
as a part and ordered by that number, because the alternative is a script that
refuses a complete set over a suffix.

If it still finds nothing it prints what it did see, which is the one thing the
person running it cannot get from a bare "no parts found".
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent

#: The index is the last run of exactly three digits in the name, with anything
#: the transfer bolted on afterwards ignored.
INDEX = re.compile(r"\.(\d{3})(?:\D.*)?$")

DEFAULT_NAME = "cmdm-offline-win_amd64-py311.zip"


def expected() -> tuple[str | None, str]:
    """The recorded checksum and archive name, if SHA256.txt is present."""
    for folder in search_path():
        record = folder / "SHA256.txt"
        if record.exists():
            fields = record.read_text(encoding="utf-8").split()
            if len(fields) >= 2:
                return fields[0], fields[1]
    return None, DEFAULT_NAME


def search_path() -> list[pathlib.Path]:
    folders = []
    if len(sys.argv) > 1:
        folders.append(pathlib.Path(sys.argv[1]).expanduser().resolve())
    folders.append(HERE)
    folders.append(pathlib.Path.cwd().resolve())
    seen: list[pathlib.Path] = []
    for folder in folders:
        if folder.is_dir() and folder not in seen:
            seen.append(folder)
    return seen


def find_parts(folder: pathlib.Path) -> list[tuple[int, pathlib.Path]]:
    """Every file in one folder that looks like a numbered part."""
    found = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        match = INDEX.search(path.name)
        if match:
            found.append((int(match.group(1)), path))
    return sorted(found)


def main() -> int:
    digest_wanted, name = expected()

    parts: list[tuple[int, pathlib.Path]] = []
    folder = None
    for candidate in search_path():
        parts = find_parts(candidate)
        if parts:
            folder = candidate
            break

    if not parts:
        print("No parts found.\n", file=sys.stderr)
        print("Looked in:", file=sys.stderr)
        for candidate in search_path():
            print(f"  {candidate}", file=sys.stderr)
            entries = sorted(p.name for p in candidate.iterdir() if p.is_file())
            if not entries:
                print("      (no files)", file=sys.stderr)
            for entry in entries[:25]:
                print(f"      {entry}", file=sys.stderr)
            if len(entries) > 25:
                print(f"      ... and {len(entries) - 25} more", file=sys.stderr)
        print(
            "\nA part is any file whose name ends in .001 through .999 "
            "(a trailing .bin or similar is fine).\n"
            "Put all seven in one folder and run this again, or pass the "
            "folder:\n    python rejoin.py C:\\path\\to\\parts",
            file=sys.stderr,
        )
        return 1

    numbers = [n for n, _ in parts]
    if numbers != list(range(1, len(parts) + 1)):
        # A missing middle part concatenates perfectly happily and produces an
        # archive that fails much later, looking like corruption rather than an
        # incomplete transfer.
        missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        print(f"Parts are not contiguous. Found {numbers}.", file=sys.stderr)
        print(f"Missing: {missing}", file=sys.stderr)
        return 1

    target = folder / name
    print(f"joining {len(parts)} parts from {folder}")
    running = hashlib.sha256()
    with target.open("wb") as out:
        for _, part in parts:
            data = part.read_bytes()
            out.write(data)
            running.update(data)
            print(f"  + {part.name}  ({len(data) / 1024 / 1024:.1f} MiB)")

    size = target.stat().st_size / 1024 / 1024
    actual = running.hexdigest()

    if digest_wanted is None:
        print(f"\nWrote {target} ({size:.1f} MiB).")
        print("SHA256.txt was not found, so this could not be verified.")
        print(f"  sha256: {actual}")
        return 0

    if actual != digest_wanted:
        # The contiguity check above catches a hole in the middle; it cannot
        # catch a set that simply stops early, because 1..5 of 7 is contiguous.
        # The count is printed here so a truncated transfer is obvious rather
        # than reading as corruption.
        print(f"\nCHECKSUM MISMATCH after joining {len(parts)} part(s).")
        print(f"  expected {digest_wanted}\n  got      {actual}")
        print("\nThe bundle was split into 7 parts. If you have fewer than that,")
        print("the transfer is incomplete — copy the rest and run this again.")
        target.unlink(missing_ok=True)
        return 1

    print(f"\nOK — {target.name}, {size:.1f} MiB, sha256 matches.")
    print("Extract it, then run install.cmd and verify.cmd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
