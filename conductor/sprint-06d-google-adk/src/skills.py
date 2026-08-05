"""
Skills adapter for Conductor -- Sprint 6d (Google ADK).

google.adk.skills is real: Skill/Frontmatter/Resources models, SkillToolset
(list_skills/load_skill/load_skill_resource/search_skills/run_skill_script), and a
SKILL.md + references/assets/scripts directory convention -- confirmed live against
the installed package, not from the public docs, which don't cover it. It is
progressive disclosure: SkillToolset.process_llm_request() injects only name+
description per skill into the request; the full body loads on demand via
load_skill. Same shape as every framework port in this series (Claude Skills API,
LangGraph/LangChain's load_skill tool, Deep Agents' SkillsMiddleware) -- ADK's
version is just more granular (five tools instead of one).

allowed-tools is part of the cross-provider Agent Skills open standard
(agentskills.io/specification#allowed-tools-field) that Claude Code also implements
-- confirmed against both the standard and Claude Code's own docs. The canonical
format is a SPACE-separated string (e.g. "Bash(git:*) Read"), not a YAML list and
not comma-separated. The shared SKILL.md (.claude/skills/conductor-troubleshoot-
connector/) originally wrote it as a YAML list -- non-compliant with the spec, and
every Claude Code doc example uses the bare string form with no list variant (unlike
the `arguments` field, which explicitly documents accepting either). Fixed at the
source: the shared file now uses the correct space-separated string, usable by every
provider in this series, not just ADK.

_load_conductor_skill() below still converts a list to a string if it encounters
one, as defensive handling for any skill file that hasn't been corrected -- cheap
insurance, not required for the shared skill anymore.

In ADK specifically, the field is functionally inert regardless of format: grep of
the entire installed google.adk.skills and google.adk.tools.skill_toolset source
finds zero reads of allowed_tools anywhere -- it round-trips through Frontmatter
validation and is never used to gate which tools a loaded skill's instructions may
reference. (Contrast Claude Code, where it grants turn-scoped auto-approval for
listed tools; even there it does not restrict the model to only those tools -- every
tool stays callable, permission settings govern the rest.) Correct format is kept
for spec fidelity and forward-compatibility, not because ADK acts on it today.
"""

from pathlib import Path
from typing import Any

import yaml

_SKILLS_ROOT = Path(__file__).resolve().parents[3] / ".claude" / "skills"
_SKILL_DIRS = ["conductor-troubleshoot-connector"]


def _load_conductor_skill(skill_dir: Path):
    """Load one skill directory as a google.adk.skills.Skill, patching the one
    incompatible frontmatter field. Returns None on any parse/validation failure
    (soft failure -- the agent runs without skills rather than crashing)."""
    from google.adk.skills import Frontmatter, Skill
    from google.adk.skills.models import Resources, Script

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    try:
        parsed = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None

    tools = parsed.get("allowed-tools") or parsed.get("allowed_tools")
    if isinstance(tools, list):
        # agentskills.io spec: allowed-tools is a space-separated string.
        parsed = {**parsed, "allowed-tools": " ".join(tools)}

    try:
        frontmatter = Frontmatter.model_validate(parsed)
    except Exception:
        return None
    if frontmatter.name != skill_dir.name:
        return None

    def _load_subdir(name: str) -> dict[str, str]:
        sub = skill_dir / name
        if not sub.is_dir():
            return {}
        out = {}
        for f in sub.rglob("*"):
            if f.is_file():
                try:
                    out[str(f.relative_to(sub))] = f.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        return out

    resources = Resources(
        references=_load_subdir("references"),
        assets=_load_subdir("assets"),
        scripts={n: Script(src=c) for n, c in _load_subdir("scripts").items()},
    )
    return Skill(frontmatter=frontmatter, instructions=parts[2].strip(), resources=resources)


def make_skills_toolset() -> Any | None:
    """Return a SkillToolset over this series' shared skill, or None if unavailable.
    Soft failure: agent runs without skills if the directory is missing or every
    skill fails to load."""
    try:
        from google.adk.tools.skill_toolset import SkillToolset
    except ImportError:
        return None

    skills = []
    for name in _SKILL_DIRS:
        skill_dir = _SKILLS_ROOT / name
        if not skill_dir.exists():
            continue
        skill = _load_conductor_skill(skill_dir)
        if skill is not None:
            skills.append(skill)

    if not skills:
        return None
    return SkillToolset(skills=skills)
