"""Offline, read-only preinstall SHA256SUMS.txt verification (stdlib only).

python scripts/verify_bundle.py --root E:\\agent_bundle [--output E:\\verification.json]

Always emits JSON; exits 0 only for a complete match, 1 for verification/report
failure. --output must be outside the bundle and must not already exist. This
does not install, extract, fetch, execute or modify any bundle content. Run the
verifier from a trusted copy before install.bat or update migration.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

MANIFEST = "SHA256SUMS.txt"
CHUNK_SIZE = 1024 * 1024
MAX_LINE = 1024 * 1024
LINE = re.compile(r"([0-9a-fA-F]{64})  (.+)")
DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.I)


def _error(report, kind, path, message):
    report["errors"].append({"kind": kind, "path": str(path), "message": message})


def _local(path):
    # Refuse UNC/device paths before filesystem access. Network-mounted drive
    # letters/mounts cannot be identified portably; operators must supply local media.
    text = os.fspath(path)
    if text.startswith(("\\\\", "//")):
        raise ValueError("UNC/network and device paths are not supported; use local media")
    return Path(os.path.abspath(text))


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _plain_path(path):
    """Reject aliases, including parent symlinks/junctions, without resolving them."""
    for component in reversed((path, *path.parents)):
        info = component.lstat()
        if _linked(info):
            raise ValueError("Symlinks and reparse points are forbidden")


def _safe_name(name):
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        return False
    return all(part not in ("", ".", "..") and not part.endswith((".", " "))
               and not DEVICE.fullmatch(part) for part in name.split("/"))


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_regular(path, expected=None):
    _plain_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _linked(info):
            raise ValueError("Only regular files are permitted")
        if expected is not None and _signature(info) != _signature(expected):
            raise ValueError("File changed since inventory")
        return os.fdopen(fd, "rb"), info
    except BaseException:
        os.close(fd)
        raise


def _inventory(root, report):
    files = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            _plain_path(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    name = path.relative_to(root).as_posix()
                    # On Windows DirEntry.stat may report zero st_ino/st_dev;
                    # lstat provides identity comparable to the opened handle.
                    info = path.lstat()
                    if _linked(info):
                        _error(report, "unsafe_file", name, "Symlink or reparse point rejected")
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        files[name] = info
                    else:
                        _error(report, "unsafe_file", name, "Special file rejected")
        except (OSError, ValueError) as exc:
            _error(report, "inventory_error", directory.relative_to(root), str(exc))
    return files


def _manifest(root, report, expected):
    records = {}
    seen = set()
    try:
        stream, before = _open_regular(root / MANIFEST, expected)
        with stream:
            number = 0
            while True:
                raw = stream.readline(MAX_LINE + 1)
                if not raw:
                    break
                number += 1
                if len(raw) > MAX_LINE:
                    raise ValueError(f"Manifest line {number} exceeds size limit")
                text = raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
                match = LINE.fullmatch(text)
                if not match:
                    _error(report, "malformed_manifest", MANIFEST, f"Invalid SHA-256 entry at line {number}")
                    continue
                digest, name = match.groups()
                if not _safe_name(name) or name.casefold() == MANIFEST.casefold():
                    _error(report, "unsafe_path", name, f"Unsafe manifest path at line {number}")
                    continue
                key = name.casefold()
                if key in seen:
                    _error(report, "duplicate_entry", name, f"Duplicate/case-alias entry at line {number}")
                    continue
                seen.add(key)
                records[name] = digest.lower()
            if _signature(before) != _signature(os.fstat(stream.fileno())):
                raise ValueError("Manifest changed during verification")
    except (OSError, ValueError) as exc:
        _error(report, "manifest_error", MANIFEST, str(exc))
    if not records:
        _error(report, "empty_manifest", MANIFEST, "Manifest must contain at least one valid file entry")
    return records


def _hash_file(path, expected):
    digest = hashlib.sha256()
    count = 0
    stream, before = _open_regular(path, expected)
    with stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            count += len(block)
            digest.update(block)
        if _signature(before) != _signature(os.fstat(stream.fileno())):
            raise ValueError("File changed during hashing")
    return digest.hexdigest(), count


def verify_bundle(root):
    """Return a JSON-serializable report; never follow links or write bundle files."""
    report = {"schema_version": 1, "kind": "offline_bundle_integrity", "ok": False,
              "root": os.fspath(root), "manifest": MANIFEST, "declared_files": 0,
              "verified_files": 0, "bytes_hashed": 0, "missing": [], "unexpected": [],
              "mismatched": [], "errors": [],
              "started_at": datetime.now(timezone.utc).isoformat(),
              "integrity": "A successful result means listed file hashes match SHA256SUMS.txt and no unlisted files were found (excluding only the root manifest).",
              "authenticity": {"verified": False, "explanation": "SHA-256 alone does not authenticate the publisher. An attacker can replace both files and manifest. Obtain the manifest/verifier through a trusted independent channel or verify an independently trusted signature."},
              "limitations": ["Point-in-time filesystem check, not a malware scan or guarantee against concurrent changes. Keep the bundle immutable through installation.",
                              "No network APIs are used. The operator must ensure the root/output reside on local media; mapped network drives/mounts cannot be recognized portably."]}
    try:
        root = _local(root)
        report["root"] = str(root)
        _plain_path(root)
        if not root.is_dir():
            raise ValueError("Bundle root must be a directory")
        files = _inventory(root, report)
        records = _manifest(root, report, files.get(MANIFEST))
        report["declared_files"] = len(records)
        report["missing"] = sorted(records.keys() - files.keys())
        report["unexpected"] = sorted(files.keys() - records.keys() - {MANIFEST})
        # Fail closed on malformed/unsafe input; do not hash a partially parsed
        # manifest or read through a tree with rejected links/special files.
        if not report["errors"]:
            for name, expected in sorted(records.items()):
                if name not in files:
                    continue
                try:
                    actual, size = _hash_file(root / name, files[name])
                    report["bytes_hashed"] += size
                    if actual == expected:
                        report["verified_files"] += 1
                    else:
                        report["mismatched"].append({"path": name, "expected": expected, "actual": actual})
                except (OSError, ValueError) as exc:
                    _error(report, "file_error", name, str(exc))
            # Detect additions/removals/replacements during the hashing pass.
            after = _inventory(root, report)
            if (files.keys() != after.keys() or any(_signature(info) != _signature(after[name])
                                                  for name, info in files.items() if name in after)):
                _error(report, "bundle_changed", ".", "Bundle changed during verification; retry on an immutable copy")
        report["ok"] = not any(report[key] for key in ("missing", "unexpected", "mismatched", "errors"))
    except (OSError, ValueError) as exc:
        _error(report, "root_error", report["root"], str(exc))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def _output_path(output, root):
    path = _local(output)
    root = _local(root)
    # No output exception is added to manifest scope. Reports must live elsewhere.
    if path == root or root in path.parents:
        raise ValueError("--output must be outside the bundle root")
    _plain_path(path.parent)
    if path.exists() or path.is_symlink():
        raise ValueError("--output must not already exist")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = verify_bundle(args.root)
    if args.output is not None:
        try:
            output = _output_path(args.output, args.root)
            with output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
        except (OSError, ValueError) as exc:
            _error(report, "output_error", args.output, str(exc))
            report["ok"] = False
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
