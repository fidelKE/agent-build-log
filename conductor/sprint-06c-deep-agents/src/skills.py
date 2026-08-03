"""
Skills adapter for Conductor -- Sprint 6b.

Builds a SkillsMiddleware instance pointing at the shared .claude/skills directory.
SkillsMiddleware uses progressive disclosure: injects skill name + description into
the system prompt at session start, then the agent calls read_file to get the full
body when a skill is needed. Full body is NOT injected upfront -- zero startup token
cost beyond the metadata block.

Import correction from initial implementation: SkillsMiddleware lives in
deepagents.middleware, not at the top-level deepagents package.
"""

from pathlib import Path

_SKILLS_ROOT = Path(__file__).resolve().parents[4] / ".claude" / "skills"


def make_skills_middleware():
    """
    Return a configured SkillsMiddleware instance, or None if unavailable.
    Sources point at the shared skills directory with a 'Conductor' label.
    Soft failure: agent runs without skills if the directory is missing.
    """
    try:
        from deepagents.middleware import SkillsMiddleware
        from deepagents.backends.filesystem import FilesystemBackend
    except ImportError:
        return None

    if not _SKILLS_ROOT.exists():
        return None

    backend = FilesystemBackend(root_dir=str(_SKILLS_ROOT), virtual_mode=False)
    return SkillsMiddleware(
        backend=backend,
        sources=[(str(_SKILLS_ROOT), "Conductor")],
    )
