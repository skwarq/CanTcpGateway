#!/usr/bin/env python3
"""Run all local formatting, analysis, syntax, and test checks."""

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PYTHON_FILES = ["can_tcp_gateway.py", "can_tcp_client.py", "tests/test_protocol.py"]


def run(label: str, *command: str) -> None:
    print(f"\n== {label} ==")
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", action="store_true", help="format files with Black before checking")
    args = parser.parse_args()
    black_args = () if args.format else ("--check",)

    try:
        run("Black", sys.executable, "-m", "black", *black_args, *PYTHON_FILES)
        run("Ruff", sys.executable, "-m", "ruff", "check", *PYTHON_FILES)
        run("Syntax", sys.executable, "-m", "compileall", "-q", *PYTHON_FILES)
        run("Pytest", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    except subprocess.CalledProcessError as error:
        print(f"\nCheck failed with exit code {error.returncode}.", file=sys.stderr)
        return error.returncode or 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
