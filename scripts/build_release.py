#!/usr/bin/env python3
"""Build an MCP Lens source ZIP from an explicit, checked file list."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
import zipfile
from pathlib import Path


FILES = (
    ".gitignore",
    "LICENSE",
    "Open MCP Lens.command",
    "README.md",
    "docs/images/demo-activity.jpg",
    "docs/images/demo-list.jpg",
    "docs/images/how-mcp-lens-works.svg",
    "docs/images/mcp-lens-banner.svg",
    "docs/images/terminal-preview.jpg",
    "mcp-lens-demo.html",
    "mcp_lens.py",
    "scripts/build_release.py",
    "tests/test_accuracy_matrix.py",
    "tests/test_functional_qa.py",
    "tests/test_mcp_lens.py",
)

PRIVATE_PATTERNS = {
    "home-folder path": re.compile(rb"(?i)/(?:Users|home)/[a-z][a-z0-9._-]{2,}"),
    "Windows home-folder path": re.compile(rb"(?i)[a-z]:\\Users\\[a-z][a-z0-9._-]{2,}"),
    "temporary device path": re.compile(rb"/private/var/folders/[a-z0-9_/-]{12,}"),
    "email address": re.compile(rb"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"),
    "credential": re.compile(
        rb"(?i)(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
        rb"github_pat_[a-z0-9_]{20,}|gh[pousr]_[a-z0-9_]{20,}|"
        rb"sk-[a-z0-9_-]{20,}|AKIA[0-9A-Z]{16})"
    ),
    "private IP address": re.compile(
        rb"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    ),
    "MAC address": re.compile(rb"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b"),
    "device UUID": re.compile(
        rb"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
        rb"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
    ),
}


def check_jpeg_metadata(name: str, data: bytes) -> None:
    if not name.lower().endswith((".jpg", ".jpeg")):
        return
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"{name}: invalid JPEG")
    offset = 2
    while offset + 4 < len(data) and data[offset] == 0xFF:
        marker = data[offset + 1]
        offset += 2
        if marker == 0xDA:
            return
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise ValueError(f"{name}: malformed JPEG segment")
        if marker in {0xE1, 0xE2, 0xED, 0xFE}:
            raise ValueError(f"{name}: JPEG metadata or comment needs review")
        offset += length
    raise ValueError(f"{name}: JPEG image data not found")


def build(source: Path, output: Path) -> None:
    approved = set(FILES)
    actual = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(source).parts
        and path.resolve() != output.resolve()
    }
    extra = sorted(actual - approved)
    missing = sorted(approved - actual)
    if extra or missing:
        raise ValueError(f"Release folder differs from approved list. Extra: {extra}; missing: {missing}")

    contents = {}
    for name in FILES:
        path = source / name
        if path.is_symlink():
            raise ValueError(f"{name}: symlinks are not allowed in the release")
        data = path.read_bytes()
        for label, pattern in PRIVATE_PATTERNS.items():
            matches = pattern.findall(data)
            if label == "home-folder path":
                matches = [match for match in matches if match != b"/Users/person"]
            if matches:
                raise ValueError(f"{name}: possible {label}; review before packaging")
        check_jpeg_metadata(name, data)
        contents[name] = data

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="mcp-lens-release-", suffix=".zip", dir=output.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name in FILES:
                info = zipfile.ZipInfo(name, (2026, 10, 27, 0, 0, 0))
                info.create_system = 3
                info.external_attr = ((0o100755 if name == "Open MCP Lens.command" else 0o100644) << 16)
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, contents[name])
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("Release ZIP failed its integrity check")
            if set(archive.namelist()) != approved:
                raise ValueError("Release ZIP has an unexpected file list")
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Built {output} with {len(FILES)} approved files.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, default=Path("mcp-lens-publish-ready.zip"))
    args = parser.parse_args()
    build(args.source.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
