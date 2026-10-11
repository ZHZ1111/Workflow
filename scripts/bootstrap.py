"""Install the two local packages and locked dependencies without the original repository."""

import argparse
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 13):
        parser.error("CPython 3.13 is required by both project packages")
    destination = args.venv.resolve()
    python = destination / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(destination)
    commands = [
        [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.lock")],
        [str(python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", str(ROOT / "vendor/agentdev")],
        [str(python), "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", str(ROOT)],
        [str(python), "-m", "pip", "check"],
    ]
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)
    print(f"Ready: {python} -m test_workflow --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
