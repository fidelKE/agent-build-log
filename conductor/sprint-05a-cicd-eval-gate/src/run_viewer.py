"""
run_viewer.py — render a Conductor trace as a readable timeline.

Usage:
    python run_viewer.py <run_id>                       # reads logs/<run_id>.jsonl
    python run_viewer.py <run_id> --log-dir /path/to/logs
    python run_viewer.py <run_id> --no-truncate         # full output, no length limits
    python run_viewer.py --compare <run_id_a> <run_id_b>

Answers the source week manual inspection questions:
  - What happened in this run?
  - Where did the time go?
  - What failed?
  - What did the tokens cost?
"""

import argparse
import json
import os
import sys


def load_trace(run_id: str, log_dir: str = "logs") -> list[dict]:
    path = os.path.join(log_dir, f"{run_id}.jsonl")
    if not os.path.exists(path):
        print(f"Trace not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _t(value: str, limit: int) -> str:
    """Truncate to limit characters; limit=0 means no truncation."""
    return value if limit == 0 or len(value) <= limit else value[:limit] + "…"


def render_trace(events: list[dict], title: str = "", truncate: int = 200) -> None:
    """
    Render a trace timeline to stdout.

    Args:
        truncate: max characters for long field values. 0 = no truncation.
    """
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")

    total_input_tokens = 0
    total_output_tokens = 0
    total_duration_ms = 0.0

    for ev in events:
        event = ev.get("event", "unknown")
        step_id = ev.get("step_id", "-")
        ts = ev.get("ts", "")

        if event == "run_start":
            print(f"\n[{ts}] RUN START  run_id={ev['run_id']}")
            print(f"  schema_version : {ev.get('schema_version')}")
            print(f"  user_message   : {_t(ev.get('user_message', ''), truncate)}")

        elif event == "llm_call":
            it = ev.get("gen_ai.usage.input_tokens", 0)
            ot = ev.get("gen_ai.usage.output_tokens", 0)
            total_input_tokens += it
            total_output_tokens += ot
            dur = ev.get("duration_ms", 0)
            total_duration_ms += dur
            parent = ev.get("parent_step_id") or "-"
            print(f"\n[{ts}] LLM CALL   {step_id}  (parent: {parent})")
            print(f"  status         : {ev.get('status')}")
            print(f"  tokens in/out  : {it} / {ot}")
            print(f"  duration_ms    : {dur}")
            print(f"  output         : {_t(ev.get('output', ''), truncate)}")

        elif event == "tool_call":
            dur = ev.get("duration_ms", 0)
            total_duration_ms += dur
            parent = ev.get("parent_step_id") or "-"
            dispatch = ev.get("dispatch_index")
            dispatch_str = f"  dispatch_index : {dispatch}\n" if dispatch is not None else ""
            print(f"\n[{ts}] TOOL CALL  {step_id}  (parent: {parent})")
            print(f"  tool           : {ev.get('tool.name')}")
            if dispatch_str:
                print(dispatch_str, end="")
            print(f"  status         : {ev.get('status')}")
            print(f"  duration_ms    : {dur}")
            print(f"  input          : {_t(json.dumps(ev.get('input', {})), truncate)}")
            print(f"  output         : {_t(json.dumps(ev.get('output', {})), truncate)}")
            if ev.get("error"):
                print(f"  error          : {ev['error']}")

        elif event == "http_call":
            dur = ev.get("duration_ms", 0)
            status = ev.get("http.status_code", "-")
            error = ev.get("error")
            print(f"\n[{ts}] HTTP CALL  {step_id}")
            print(f"  method         : {ev.get('http.method')}  {ev.get('http.url')}")
            print(f"  request_body   : {_t(json.dumps(ev.get('http.request_body', {})), truncate)}")
            print(f"  status_code    : {status}")
            print(f"  response_body  : {_t(json.dumps(ev.get('http.response_body', {})), truncate)}")
            print(f"  duration_ms    : {dur}")
            if error:
                print(f"  error          : {error}")
            # Confirm credential injection — Authorization header should never appear
            headers = ev.get("http.request_headers", {})
            if "Authorization" in headers:
                print(f"  WARNING        : Authorization header present in trace — credential leak!")
            else:
                print(f"  auth_header    : [not logged — credential injection confirmed]")

        elif event in ("session_new", "session_resumed", "session_discarded", "checkpoint_cleared",
                        "checkpoint_none", "checkpoint_resumed", "checkpoint_cleared", "checkpoint_discarded"):
            detail = ""
            if event == "session_resumed":
                detail = f"  resumed_from_step={ev.get('resumed_from_step')}  source={ev.get('source','')}"
            elif event == "session_discarded":
                detail = f"  reason={ev.get('reason','')}"
            elif event == "checkpoint_resumed":
                detail = f"  resumed_from_step={ev.get('resumed_from_step')}"
            elif event in ("checkpoint_discarded",):
                detail = f"  reason={ev.get('reason','')}"
            label = event.upper().replace("SESSION_", "SESSION ").replace("CHECKPOINT_", "CHECKPOINT ")
            print(f"\n[{ts}] {label}  {detail}")

        elif event == "fan_out":
            print(f"\n[{ts}] FAN OUT    {step_id}  workers={ev.get('worker_count')}")
            print(f"  merge_strategy : {ev.get('merge_strategy')}")

        elif event == "fan_out_complete":
            print(f"\n[{ts}] FAN OUT COMPLETE  {step_id}")
            print(f"  selected       : {ev.get('selected')}")

        elif event == "run_end":
            print(f"\n[{ts}] RUN END")
            print(f"  status         : {ev.get('status')}")
            print(f"  total_steps    : {ev.get('total_steps')}")
            print(f"  total_ms       : {ev.get('total_duration_ms')}")
            answer = ev.get("final_answer", "") or ""
            print(f"  final_answer   : {_t(answer, truncate)}")

    print(f"\n{'─'*60}")
    print(f"  SUMMARY")
    print(f"  input tokens   : {total_input_tokens}")
    print(f"  output tokens  : {total_output_tokens}")
    print(f"  total tokens   : {total_input_tokens + total_output_tokens}")
    print(f"  llm+tool ms    : {total_duration_ms:.1f}")
    print(f"{'─'*60}\n")


def compare_traces(run_id_a: str, run_id_b: str, log_dir: str, truncate: int = 200) -> None:
    events_a = load_trace(run_id_a, log_dir)
    events_b = load_trace(run_id_b, log_dir)
    render_trace(events_a, title=f"BEFORE  ({run_id_a[:8]}...)", truncate=truncate)
    render_trace(events_b, title=f"AFTER   ({run_id_b[:8]}...)", truncate=truncate)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a Conductor trace")
    parser.add_argument("run_id", nargs="?", help="run_id to render")
    parser.add_argument("--log-dir", default="logs", help="directory containing .jsonl files")
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"),
                        help="compare two runs side by side")
    parser.add_argument("--no-truncate", action="store_true",
                        help="print full field values without length limits")
    args = parser.parse_args()

    truncate = 0 if args.no_truncate else 200

    if args.compare:
        compare_traces(args.compare[0], args.compare[1], args.log_dir, truncate=truncate)
    elif args.run_id:
        events = load_trace(args.run_id, args.log_dir)
        render_trace(events, title=f"RUN  {args.run_id[:8]}...", truncate=truncate)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
