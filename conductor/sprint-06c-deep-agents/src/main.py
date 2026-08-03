"""
Conductor -- Sprint 6b entry point.

Sprint 6b: run() backed by Deep Agents create_deep_agent() instead of LangGraph CompiledGraph.
graph.py removed -- Deep Agents owns the graph topology.
--db-path retained for SQLite cross-session message history (CheckpointStore), not LangGraph checkpointer.

Usage:
    # Fresh session (troubleshooting mode, default)
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main "My Snowflake connector keeps timing out."

    # Setup mode -- exercises SetupStateMachine + HITL gate on write_connector_config (RULE-DA04)
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo "Set up the snowflake-prod connector"
    # When status=INTERRUPTED, approve interactively at the prompt then re-run with --resume:
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo --resume ""

    # Resume a named session (SQLite cross-session message history)
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --session demo "What connectors do you support?"

    # Clear checkpoint and restart
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --session demo --restart "Start over"

    # Full trace depth
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --verbose "Why is my BigQuery connection failing?"
"""

import argparse
import asyncio
import sys
import uuid

from .agent import run
from .logger import TraceDepth


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Conductor -- technical co-pilot for data integration (Deep Agents)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("message", nargs="*", help="Message to send to the agent")
    parser.add_argument(
        "--session", default=None,
        help="Session ID for checkpoint continuity. Omit for a fresh session each run.",
    )
    parser.add_argument(
        "--mode", default="troubleshooting",
        choices=["troubleshooting", "setup", "onboarding", "qa"],
        help="Conductor capability mode (default: troubleshooting).",
    )
    parser.add_argument(
        "--restart", action="store_true",
        help="Clear the checkpoint for --session and start from step 1.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Full trace depth -- logs all input messages (useful for debugging).",
    )
    parser.add_argument(
        "--no-vault", action="store_true",
        help="Skip Vault, use env vars for secrets.",
    )
    parser.add_argument(
        "--user-id", default=None,
        help="Authenticated user identifier for memory namespace isolation.",
    )
    parser.add_argument(
        "--db-path", default="conductor_checkpoints.db",
        help="Path to SQLite cross-session message history file. Default: conductor_checkpoints.db",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume after a HITL interrupt -- prompts for approve/reject interactively.",
    )
    args = parser.parse_args()

    if not args.message and not args.restart and not args.resume:
        parser.print_help()
        sys.exit(0)

    query = " ".join(args.message) if args.message else ""
    depth = TraceDepth.FULL if args.verbose else TraceDepth.BOUNDARY
    session_id = args.session or str(uuid.uuid4())

    resume_decisions = None
    if args.resume:
        decision = input("HITL gate: approve / reject / edit? [approve]: ").strip().lower() or "approve"
        if decision == "edit":
            print("Note: to edit tool args, pass a JSON decisions list via code. Defaulting to approve.")
            decision = "approve"
        resume_decisions = [{"type": decision}]

    state, log = asyncio.run(run(
        query,
        session_id=session_id,
        user_id=args.user_id,
        mode=args.mode,
        log_dir="logs",
        trace_depth=depth,
        prefer_vault=not args.no_vault,
        restart=args.restart,
        db_path=args.db_path,
        resume_decisions=resume_decisions,
    ))

    print(f"\nStatus  : {state.status.value}")
    print(f"Steps   : {state.step_count}")

    if state.status.value == "interrupted":
        print("\nHITL gate triggered. Pending actions:")
        for i, action in enumerate(state.interrupts):
            print(f"  [{i}] tool={action.get('name', '?')} args={action.get('args', {})}")
        print(f"\nTo resume: add --resume --session {session_id} to your next invocation.")
    else:
        print(f"Answer  :\n{state.final_answer or '(no answer)'}")

    print(f"\nTrace   : logs/{log.run_id}.jsonl")
    print(f"Session : {session_id}  (use --session {session_id} to continue)")


if __name__ == "__main__":
    main()
