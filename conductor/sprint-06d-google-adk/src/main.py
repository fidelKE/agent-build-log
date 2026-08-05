"""
Conductor -- Sprint 6d entry point (Google ADK).

Sprint 6d: run() backed by Google ADK (LlmAgent / SequentialAgent / ParallelAgent)
instead of Deep Agents. --resume (mid-run pause/resume) replaced by --approve
(pre-authorization set in initial session state before the run starts) -- ADK has
no interrupt_on=/checkpointer primitive at this level. See agent.py module docstring
and results.md for the ceiling finding.

--db-path retained for SQLite cross-session message history (CheckpointStore), same
as every prior lab -- independent of whatever the framework's own session service does.

Usage:
    # Fresh session (troubleshooting mode, default)
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main "My Snowflake connector keeps timing out."

    # Setup mode -- exercises the SequentialAgent structural guarantee (RULE-ADK03)
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo "Set up the snowflake-prod connector"
    # write_connector_config is approval-gated -- pre-authorize before the run that needs it:
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo --approve "Set up the snowflake-prod connector"

    # Onboarding mode -- exercises the ParallelAgent concurrent checks
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode onboarding "I'm new here, what connectors do we have?"

    # BuiltInPlanner A/B -- same query, with and without Gemini native thinking
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --planner "Why is my BigQuery connection failing?"

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
        description="Conductor -- technical co-pilot for data integration (Google ADK)",
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
        "--approve", action="store_true",
        help="Pre-authorize write_connector_config for this run (Setup mode HITL workaround).",
    )
    parser.add_argument(
        "--planner", action="store_true",
        help="Wrap the troubleshooting/qa agent with BuiltInPlanner (Gemini native thinking).",
    )
    args = parser.parse_args()

    if not args.message and not args.restart:
        parser.print_help()
        sys.exit(0)

    query = " ".join(args.message) if args.message else ""
    depth = TraceDepth.FULL if args.verbose else TraceDepth.BOUNDARY
    session_id = args.session or str(uuid.uuid4())

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
        approved=args.approve,
        use_planner=args.planner,
    ))

    print(f"\nStatus  : {state.status.value}")
    print(f"Steps   : {state.step_count}")
    print(f"Answer  :\n{state.final_answer or '(no answer)'}")

    print(f"\nTrace   : logs/{log.run_id}.jsonl")
    print(f"Session : {session_id}  (use --session {session_id} to continue)")


if __name__ == "__main__":
    main()
