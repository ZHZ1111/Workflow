"""Allow python -m testgen_agent to use the same CLI as the installed script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
