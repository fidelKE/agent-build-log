"""
LangChain skills pattern for Conductor -- Sprint 6a.

The Claude Agent SDK (Sprint 6) loads SKILL.md files lazily via the Skills API.
LangGraph has no equivalent primitive, so we replicate the pattern with a
@tool-decorated function:

  1. Agent receives a tool definition for load_skill(skill_name: str)
  2. Agent calls it on demand (progressive disclosure -- not injected at startup)
  3. The function reads the corresponding SKILL.md body and returns it as a string
  4. Agent uses the returned instructions for the rest of the turn

This means: zero startup token cost. Skill content only enters context when called.
"""

import os
from pathlib import Path

from langchain_core.tools import tool

# Absolute path to the project .claude/skills/ directory
# Path depth verified live -- src/skills.py is 3 levels below the repo root
# (src -> sprint-06a-langgraph -> conductor -> repo root), so parents[3] is
# correct. parents[4] (the original value) resolved one level ABOVE the repo,
# to a different, unrelated .claude/skills/ directory that happened to exist --
# load_skill() never crashed, it silently returned "Skill file not found" for
# every call. Confirmed while investigating the same bug across 6a/6b/6c.
_SKILLS_ROOT = Path(__file__).resolve().parents[3] / ".claude" / "skills"

# Names that map to SKILL.md files in .claude/skills/
REGISTERED_SKILLS = frozenset({"conductor-troubleshoot-connector"})


@tool
def load_skill(skill_name: str) -> str:
    """
    Load the instructions for a named Conductor skill.

    Use this when the user's request matches a known skill:
    - conductor-troubleshoot-connector: diagnosing connector failures

    Returns the skill body (workflow steps and constraints).
    Call this before executing the skill's workflow.
    """
    if skill_name not in REGISTERED_SKILLS:
        return f"Unknown skill: {skill_name!r}. Available: {sorted(REGISTERED_SKILLS)}"

    skill_path = _SKILLS_ROOT / skill_name / "SKILL.md"
    if not skill_path.exists():
        return f"Skill file not found: {skill_path}"

    content = skill_path.read_text(encoding="utf-8")
    # Strip YAML frontmatter (between first --- delimiters) -- agent needs the body only
    lines = content.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = lines.index("---", 1)
            body = "\n".join(lines[end + 1:]).strip()
        except ValueError:
            body = content.strip()
    else:
        body = content.strip()

    return body
