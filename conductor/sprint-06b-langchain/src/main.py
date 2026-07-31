"""
Conductor -- Sprint 6b entry point.

Sprint 6b: backed by create_agent() harness instead of LangGraph CompiledGraph.
--db-path removed (InMemorySaver replaces SQLite checkpointer for this sprint).

Usage:
    # Fresh session (troubleshooting mode, default)
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main "My Snowflake connector keeps timing out."

    # Setup mode -- exercises SetupStateMachine via stm_gate + HumanInTheLoopMiddleware
    UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo "Set up the snowflake-prod connector"

    # Resume a named session (InMemorySaver + message history from SessionStore)
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
        description="Conductor -- technical co-pilot for data integration (LangChain create_agent())",
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
    ))

    print(f"\nStatus  : {state.status.value}")
    print(f"Steps   : {state.step_count}")
    print(f"Answer  :\n{state.final_answer or '(no answer)'}")
    print(f"\nTrace   : logs/{log.run_id}.jsonl")
    print(f"Session : {session_id}  (use --session {session_id} to continue)")


if __name__ == "__main__":
    main()
