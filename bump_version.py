#!/usr/bin/env python3
"""Update the project version in pyproject.toml and kflow/_version.py."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent

FILES = [
    (ROOT / "pyproject.toml",   r'(version\s*=\s*")[^"]+(")',  r'\g<1>{ver}\g<2>'),
    (ROOT / "kflow" / "_version.py", r'(__version__\s*=\s*")[^"]+(")', r'\g<1>{ver}\g<2>'),
]


def current_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'version\s*=\s*"([^"]+)"', text)
    if not m:
        raise ValueError("Could not find version in pyproject.toml")
    return m.group(1)


def bump(text: str, pattern: str, replacement: str, ver: str) -> tuple[str, bool]:
    new_text, n = re.subn(pattern, replacement.format(ver=ver), text)
    return new_text, n > 0


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Current version: {current_version()}")
        print(f"Usage: python bump_version.py <new-version>")
        sys.exit(0)

    new_ver = sys.argv[1]

    for path, pattern, replacement in FILES:
        text = path.read_text()
        new_text, changed = bump(text, pattern, replacement, new_ver)
        if not changed:
            print(f"WARNING: no match in {path}")
            continue
        path.write_text(new_text)
        print(f"Updated {path.relative_to(ROOT)}")

    print(f"Version bumped to {new_ver}")


if __name__ == "__main__":
    main()
