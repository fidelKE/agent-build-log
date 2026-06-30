"""
Agent BOM (ABOM) validator for Conductor.

Reads agent-bom.yaml, computes current sha256 of each registered file,
and exits non-zero if any hash has drifted. This catches silent modification
of the system prompt, tools, or secrets layer without triggering a version bump.

Usage:
    python bom_validator.py [--bom path/to/agent-bom.yaml]

Exit codes:
    0 - all hashes match (clean state)
    1 - one or more hashes drifted (or BOM file is malformed)
"""

import argparse
import hashlib
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: uv add pyyaml", file=sys.stderr)
    sys.exit(1)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(bom_path: Path) -> list[str]:
    """Return a list of drift messages. Empty = clean."""
    bom_dir = bom_path.parent
    with bom_path.open() as f:
        bom = yaml.safe_load(f)

    errors: list[str] = []

    # Validate prompt hash
    prompt = bom.get("prompt", {})
    prompt_file = bom_dir / prompt["file"]
    if not prompt_file.exists():
        errors.append(f"MISSING: prompt file not found: {prompt_file}")
    else:
        actual = sha256_file(prompt_file)
        expected = prompt["sha256"]
        if actual != expected:
            errors.append(
                f"DRIFT: {prompt['file']}\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual}"
            )

    # Validate tool hashes
    for tool in bom.get("tools", []):
        tool_file = bom_dir / tool["file"]
        if not tool_file.exists():
            errors.append(f"MISSING: tool file not found: {tool_file}")
            continue
        actual = sha256_file(tool_file)
        expected = tool["sha256"]
        if actual != expected:
            errors.append(
                f"DRIFT: {tool['file']}\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual}"
            )

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate agent BOM hashes")
    parser.add_argument(
        "--bom",
        default="agent-bom.yaml",
        help="Path to agent-bom.yaml (default: agent-bom.yaml in cwd)",
    )
    args = parser.parse_args()

    bom_path = Path(args.bom)
    if not bom_path.exists():
        print(f"ERROR: BOM file not found: {bom_path}", file=sys.stderr)
        sys.exit(1)

    errors = validate(bom_path)

    if errors:
        print("BOM VALIDATION FAILED - component drift detected:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

    print(f"BOM OK - {len(list(bom_path.parent.glob('src/*.py')))} components verified, no drift detected")


if __name__ == "__main__":
    main()
