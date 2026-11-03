"""Install only the verified better-sqlite3 binary from its pinned release tarball.

This helper never invokes a compiler, npm script, or tar extraction operation.
The caller separately checks the installed package version and Node ABI.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import tarfile
import tempfile

MEMBER = "build/Release/better_sqlite3.node"
MAX_NATIVE_BYTES = 32 * 1024 * 1024


def checked_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if str(path).startswith("\\\\"):
        raise ValueError("The native package must use a local path.")
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise ValueError("The native package cannot traverse a link.")
        if entry.exists() and getattr(entry.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("The native package cannot traverse a junction.")
    return path


def install_native(archive: Path, destination: Path, sha256: str) -> Path:
    if len(sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in sha256):
        raise ValueError("A valid SHA256 digest is required.")
    archive = checked_path(archive)
    destination = checked_path(destination)
    if not destination.is_dir() or not (destination / "package.json").is_file():
        raise ValueError("The existing better-sqlite3 package directory is required.")
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != sha256.lower():
        raise ValueError("SQLite native archive checksum mismatch.")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        selected = []
        for member in members:
            name = member.name[2:] if member.name.startswith("./") else member.name
            if member.isdir() and name.rstrip("/") in {"build", "build/Release"}:
                continue
            if name != MEMBER or not member.isfile() or not 0 < member.size <= MAX_NATIVE_BYTES:
                raise ValueError("Unexpected or unsafe entry in SQLite native archive.")
            selected.append(member)
        if len(selected) != 1:
            raise ValueError("SQLite native archive must contain exactly one binary.")
        with source.extractfile(selected[0]) as binary:
            data = binary.read(MAX_NATIVE_BYTES + 1)
        if len(data) != selected[0].size or not data.startswith(b"MZ"):
            raise ValueError("SQLite native binary is incomplete or is not a Windows binary.")
    output = checked_path(destination / MEMBER)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".sqlite-native-", suffix=".tmp", dir=output.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    arguments = parser.parse_args()
    try:
        install_native(arguments.archive, arguments.destination, arguments.sha256)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"SQLite native installation failed: {error}\n")
    print("sqlite-native-installed")


if __name__ == "__main__":
    main()
