#!/usr/bin/env python3
"""Build and upload the package to PyPI."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def run(cmd: list[str], **kwargs) -> None:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit(f".env not found — create {env_file} with PYPI_API_KEY=<token>")

    load_env(env_file)
    api_key = os.environ.get("PYPI_API_KEY")
    if not api_key:
        sys.exit("PYPI_API_KEY not set in .env")

    # Clean old build artifacts
    for d in ("dist", "build"):
        shutil.rmtree(ROOT / d, ignore_errors=True)
    print("Cleaned dist/ and build/")

    run([sys.executable, "-m", "build"], cwd=ROOT)

    run(
        [
            sys.executable, "-m", "twine", "upload",
            "--username", "__token__",
            "--password", api_key,
            "dist/*",
        ],
        cwd=ROOT,
    )

    print("Done.")


if __name__ == "__main__":
    main()
