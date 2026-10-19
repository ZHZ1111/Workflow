"""Inspect the integrated AgentDev provenance; initial snapshot capture is archived."""
import argparse
import json
from pathlib import Path
from vendor_audit import audit_vendor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentdev", type=Path, help="Obsolete: the source is already integrated")
    args = parser.parse_args()
    if args.agentdev is not None:
        parser.error("AgentDev is integrated in agents/test_generation/core; see docs/agent-layout.md")
    result = audit_vendor(Path(__file__).resolve().parents[1])
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
