"""Allow python -m test_workflow.agents.test_generation.core to use the same CLI as the installed script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
