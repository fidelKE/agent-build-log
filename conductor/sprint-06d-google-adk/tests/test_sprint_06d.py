"""
Tests for Sprint 6d -- Google ADK port.

Test requirements (Weeks 11+12 source agenda, sprint-6d spec):
  1. Same eval dataset -- conductor-v2.yaml runs against this harness (eval/runner.py,
     exercised live in Phase 4, not re-asserted here as a unit test)
  2. Workflow determinism -- the Setup graph's edges are a fixed, inspectable chain
     (no skip-forward path); the Onboarding graph fans out then joins deterministically
  3. IAM/service identity, not pasted keys -- GatewayGemini reads credentials from
     environment only, never a literal; no credential string ever reaches a log line
  4. Local vs deployed trace comparison -- N/A this lab. Cloud Run/Agent Engine
     deployment is explicitly out of scope (README "Out of Scope") -- there is no
     "deployed" trace to compare against a local one. Documented here rather than
     silently skipped.
  5. Same eval repeated 3x for flaky detection -- Phase 4 ran the 9-case adversarial
     subset live once (cost-bounded); repeat-3x flaky detection is deferred to
     Lab 9a (chaos engineering / pass^k), which is where this series formalizes it
     for every framework, not just this one.

Conductor-specific behavior asserted here (RULE-ADK01/02/03, RULE-A01, RULE-T02):
  - Every workflow leaf is an LlmAgent; the graph itself is a structural wrapper only
  - before_tool_callback blocks write_connector_config unless approved=True in state
  - before_tool_callback blocks path traversal and suspected-secret-file reads
  - after_tool_callback logs every completed call uniformly, independent of self-logging
  - RunStatus.LIMIT_REACHED exists for the RunConfig(max_llm_calls=) cap (RULE-A01)
  - ToolExecutor still validates via Pydantic and never raises past a missing secret
    (regression test for the live-discovered KeyError-crashes-the-graph bug)
  - agent-bom.yaml stays consistent with agent.py's MODEL constant and actual files
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src.agent import MODEL, MAX_ITERATIONS, GatewayGemini, _make_model, _format_history_context
from src.callback import ToolCallGuard, ALLOWED_ROOT
from src.state import RunStatus
from src.tools import ToolExecutor, build_adk_tool_functions, TOOL_SCHEMAS
from src.workflow import build_setup_workflow, build_onboarding_workflow
from src.skills import make_skills_toolset, _load_conductor_skill, _SKILLS_ROOT

BOM_PATH = Path(__file__).parents[1] / "agent-bom.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeLogger:
    def __init__(self):
        self.calls = []

    def log_tool_call(self, **kw):
        self.calls.append(kw)


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeToolContext:
    def __init__(self, state, function_call_id="fc-test"):
        self.state = state
        self.function_call_id = function_call_id


def _build_workflows():
    executor = ToolExecutor()
    tool_functions = build_adk_tool_functions(executor)
    guard = ToolCallGuard(_FakeLogger())
    setup_wf = build_setup_workflow(_make_model, tool_functions, guard.before_tool_callback, guard.after_tool_callback)
    onboarding_wf = build_onboarding_workflow(_make_model, tool_functions, guard.before_tool_callback, guard.after_tool_callback)
    return setup_wf, onboarding_wf


# ---------------------------------------------------------------------------
# 1 -- RULE-ADK03: Workflow structural guarantee (Success Criterion #1)
# ---------------------------------------------------------------------------

class TestSetupWorkflowStructure:
    """The Setup graph must make write-before-validate architecturally impossible,
    not merely disallowed by a runtime check -- this is the direct RULE-STM01
    supersession claim (see STANDARDS.md)."""

    def test_edges_form_strict_sequential_chain(self):
        setup_wf, _ = _build_workflows()
        edge_pairs = [(e.from_node.name, e.to_node.name) for e in setup_wf.graph.edges]
        assert edge_pairs == [
            ("__START__", "SetupReadAgent"),
            ("SetupReadAgent", "SetupValidateAgent"),
            ("SetupValidateAgent", "SetupConfigureAgent"),
            ("SetupConfigureAgent", "SetupEnableAgent"),
        ]

    def test_no_edge_skips_a_step(self):
        """Test case for Success Criterion #1: a step is architecturally blocked
        when the prior step hasn't completed -- proven by absence of any edge that
        would let execution reach configure/enable without going through read+validate."""
        setup_wf, _ = _build_workflows()
        edge_pairs = {(e.from_node.name, e.to_node.name) for e in setup_wf.graph.edges}
        forbidden_skips = [
            ("__START__", "SetupConfigureAgent"),
            ("__START__", "SetupEnableAgent"),
            ("SetupReadAgent", "SetupConfigureAgent"),
            ("SetupReadAgent", "SetupEnableAgent"),
            ("SetupValidateAgent", "SetupEnableAgent"),
        ]
        for skip in forbidden_skips:
            assert skip not in edge_pairs, f"Unexpected skip-forward edge: {skip}"

    def test_configure_agent_has_only_write_tool(self):
        """SetupConfigureAgent's tools= list must not contain read/validate tools --
        the model cannot call them even if it wanted to skip back."""
        setup_wf, _ = _build_workflows()
        configure = next(n for n in setup_wf.graph.nodes if n.name == "SetupConfigureAgent")
        tool_names = {getattr(t, "__name__", None) for t in configure.tools}
        assert tool_names == {"write_connector_config"}

    def test_every_leaf_is_llm_agent(self):
        """RULE-ADK01: every node except START is an LlmAgent -- the graph is a
        structural wrapper, never an execution unit itself."""
        from google.adk.agents.llm_agent import LlmAgent
        setup_wf, _ = _build_workflows()
        for node in setup_wf.graph.nodes:
            if node.name == "__START__":
                continue
            assert isinstance(node, LlmAgent), f"{node.name} is not an LlmAgent: {type(node)}"


class TestOnboardingWorkflowStructure:
    """Onboarding fans out from START to three independent branches, then joins --
    a JoinNode is required because a Workflow permits at most one terminal output
    (live-discovered constraint, see workflow.py docstring)."""

    def test_fans_out_from_start_then_joins(self):
        _, onboarding_wf = _build_workflows()
        edge_pairs = {(e.from_node.name, e.to_node.name) for e in onboarding_wf.graph.edges}
        for branch in ("OnboardingStatusAgent", "OnboardingCatalogAgent", "OnboardingMemoryAgent"):
            assert ("__START__", branch) in edge_pairs
            assert (branch, "OnboardingJoin") in edge_pairs

    def test_branches_have_no_edges_between_them(self):
        """Independence: no branch feeds into another -- they run concurrently, not sequentially."""
        _, onboarding_wf = _build_workflows()
        edge_pairs = {(e.from_node.name, e.to_node.name) for e in onboarding_wf.graph.edges}
        branches = ("OnboardingStatusAgent", "OnboardingCatalogAgent", "OnboardingMemoryAgent")
        for a in branches:
            for b in branches:
                if a != b:
                    assert (a, b) not in edge_pairs

    def test_has_single_terminal_join_node(self):
        from google.adk.workflow import JoinNode
        _, onboarding_wf = _build_workflows()
        join_nodes = [n for n in onboarding_wf.graph.nodes if isinstance(n, JoinNode)]
        assert len(join_nodes) == 1
        assert join_nodes[0].name == "OnboardingJoin"


# ---------------------------------------------------------------------------
# 2 -- RULE-ADK02: before/after_tool_callback (Success Criterion #4, safety)
# ---------------------------------------------------------------------------

class TestToolCallGuard:

    def test_blocks_write_connector_config_without_approval(self):
        logger = _FakeLogger()
        guard = ToolCallGuard(logger)
        result = guard.before_tool_callback(
            _FakeTool("write_connector_config"), {"connector_id": "x", "config_patch": {}},
            _FakeToolContext({"approved": False}),
        )
        assert result is not None
        assert result["error"] is True
        assert logger.calls[-1]["status"] == "error"

    def test_allows_write_connector_config_with_approval(self):
        guard = ToolCallGuard(_FakeLogger())
        result = guard.before_tool_callback(
            _FakeTool("write_connector_config"), {"connector_id": "x", "config_patch": {}},
            _FakeToolContext({"approved": True}),
        )
        assert result is None

    def test_ungated_tool_allowed_regardless_of_approval(self):
        guard = ToolCallGuard(_FakeLogger())
        result = guard.before_tool_callback(
            _FakeTool("check_connector_status"), {"connector_id": "x"},
            _FakeToolContext({"approved": False}),
        )
        assert result is None

    def test_blocks_path_outside_allowed_root(self):
        guard = ToolCallGuard(_FakeLogger())
        result = guard.before_tool_callback(
            _FakeTool("read_file"), {"file_path": "../../etc/passwd"},
            _FakeToolContext({"approved": True}),
        )
        assert result is not None
        assert "outside the allowed directory" in result["message"]

    def test_blocks_suspected_secret_file(self):
        """This is the exact 'fake secret file must not be read' safety check the
        five-way benchmark's Repo Triage scenario exercises."""
        guard = ToolCallGuard(_FakeLogger())
        result = guard.before_tool_callback(
            _FakeTool("read_file"), {"filename": ".env"},
            _FakeToolContext({"approved": True}),
        )
        assert result is not None
        assert "secret" in result["message"].lower()

    def test_allows_normal_args_with_no_path_keys(self):
        guard = ToolCallGuard(_FakeLogger())
        result = guard.before_tool_callback(
            _FakeTool("check_connector_status"), {"connector_id": "snowflake-prod"},
            _FakeToolContext({"approved": True}),
        )
        assert result is None

    def test_after_tool_callback_logs_uniformly(self):
        logger = _FakeLogger()
        guard = ToolCallGuard(logger)
        ctx = _FakeToolContext({"approved": True})
        guard.before_tool_callback(_FakeTool("check_connector_status"), {"connector_id": "x"}, ctx)
        guard.after_tool_callback(_FakeTool("check_connector_status"), {"connector_id": "x"}, ctx, {"status": "live"})
        assert logger.calls[-1]["status"] == "success"
        assert logger.calls[-1]["tool_name"] == "check_connector_status"
        assert logger.calls[-1]["duration_ms"] >= 0

    def test_guard_instances_do_not_share_state(self):
        """Regression guard: start-time tracking is per-instance, not a module-level
        global -- a long-lived process (eval/runner.py iterating a dataset) must not
        leak state across runs."""
        guard_a = ToolCallGuard(_FakeLogger())
        guard_b = ToolCallGuard(_FakeLogger())
        assert guard_a._start_times is not guard_b._start_times


# ---------------------------------------------------------------------------
# 3 -- IAM/service identity, not pasted keys (Week 11 test requirement)
# ---------------------------------------------------------------------------

class TestGatewayGemini:

    def test_reads_credentials_from_environment_not_literal(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-value")
        monkeypatch.setenv("LLM_GATEWAY_URL", "https://test-gateway.example")
        model = GatewayGemini(model="gemini-pro-latest")
        client = model.api_client
        # No literal credential is embedded in agent.py/workflow.py source -- the
        # class body itself only ever reads os.environ, asserted structurally:
        import inspect
        source = inspect.getsource(GatewayGemini)
        assert "test-key-value" not in source
        assert "os.environ" in source

    def test_missing_credential_raises_not_silently_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        model = GatewayGemini(model="gemini-pro-latest")
        with pytest.raises(KeyError):
            _ = model.api_client

    def test_make_model_uses_configured_model_name(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("LLM_GATEWAY_URL", "https://x")
        instance = _make_model()
        assert instance.model == MODEL

    def test_each_call_returns_a_fresh_instance(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("LLM_GATEWAY_URL", "https://x")
        a = _make_model()
        b = _make_model()
        assert a is not b


# ---------------------------------------------------------------------------
# 4 -- RULE-A01: hard iteration cap
# ---------------------------------------------------------------------------

class TestIterationCap:

    def test_limit_reached_status_exists(self):
        assert RunStatus.LIMIT_REACHED.value == "limit_reached"

    def test_max_iterations_is_positive_and_bounded(self):
        assert 0 < MAX_ITERATIONS <= 20


# ---------------------------------------------------------------------------
# 5 -- ToolExecutor: RULE-T01/T02/T03 + regression test for the live-discovered bug
# ---------------------------------------------------------------------------

class TestToolExecutorContract:

    def test_invalid_input_returns_tool_error_not_exception(self):
        executor = ToolExecutor()
        result = executor.execute("notes_search", {"query": ""})
        assert result.get("error") is True
        assert result["error_code"] == "INVALID_INPUT"

    def test_valid_call_returns_typed_dict(self):
        executor = ToolExecutor()
        result = executor.execute("notes_search", {"query": "snowflake timeout"})
        assert "results" in result
        assert "total_found" in result

    def test_missing_secret_returns_tool_error_not_keyerror(self, monkeypatch):
        """Regression test: SecretStore.get() raises KeyError on a missing secret
        (its documented contract) -- _search_knowledge_base must catch that and
        return ToolError, not let it crash the caller. Live-discovered sprint-6d
        when this crashed an entire Workflow graph run."""
        monkeypatch.delenv("CATALOG_API_TOKEN", raising=False)
        from src.secrets import LocalStubSecretStore
        executor = ToolExecutor(secret_store=LocalStubSecretStore(), catalog_base_url="https://x")
        result = executor.execute("search_knowledge_base", {"query": "test"})
        assert result.get("error") is True
        assert result["error_code"] == "MISSING_CREDENTIAL"

    def test_secret_key_default_matches_env_var_naming_convention(self):
        """catalog_api_token -> CATALOG_API_TOKEN, matching LocalStubSecretStore's
        documented key-mapping convention (secrets.py). The prior default
        ('catalog_token' -> CATALOG_TOKEN) never matched any .env variable name."""
        executor = ToolExecutor()
        assert executor._secret_key == "catalog_api_token"

    def test_unknown_tool_raises(self):
        executor = ToolExecutor()
        with pytest.raises(ValueError):
            executor.execute("not_a_real_tool", {})


# ---------------------------------------------------------------------------
# 6 -- ADK tool function adapters: real signatures for schema introspection
# ---------------------------------------------------------------------------

class TestAdkToolFunctions:

    def test_every_tool_schema_has_an_adapter(self):
        executor = ToolExecutor()
        adapters = build_adk_tool_functions(executor)
        schema_names = {s["name"] for s in TOOL_SCHEMAS}
        assert schema_names == set(adapters.keys())

    def test_adapters_have_explicit_named_parameters(self):
        """ADK introspects real function signatures to build tool schemas -- a
        **kwargs-only wrapper gives it nothing to introspect."""
        import inspect
        executor = ToolExecutor()
        adapters = build_adk_tool_functions(executor)
        for name, fn in adapters.items():
            params = inspect.signature(fn).parameters
            assert params, f"{name} adapter has no parameters"
            assert not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()), (
                f"{name} adapter uses **kwargs -- ADK cannot introspect a schema from this"
            )

    def test_adapter_forwards_to_executor(self):
        executor = ToolExecutor()
        adapters = build_adk_tool_functions(executor)
        result = adapters["notes_search"](query="snowflake timeout", max_results=2)
        assert "results" in result


# ---------------------------------------------------------------------------
# 7 -- Skills: real google.adk.tools.skill_toolset.SkillToolset wiring
# ---------------------------------------------------------------------------

class TestSkillsAdapter:

    def test_loads_the_shared_skill(self):
        skill = _load_conductor_skill(_SKILLS_ROOT / "conductor-troubleshoot-connector")
        assert skill is not None
        assert skill.frontmatter.name == "conductor-troubleshoot-connector"

    def test_shared_skill_uses_spec_compliant_space_separated_string(self):
        """agentskills.io/specification#allowed-tools-field: 'A space-separated
        string of tools that are pre-approved to run' -- not a YAML list, not
        comma-separated. Claude Code's own docs show only this bare-string form in
        every example (contrast the `arguments` field, which explicitly documents
        accepting either a string or a list -- allowed-tools has no such callout).
        The shared SKILL.md was fixed to this format so it works for every provider
        in this series, not just ADK."""
        import yaml
        skill_dir = _SKILLS_ROOT / "conductor-troubleshoot-connector"
        raw = (skill_dir / "SKILL.md").read_text()
        frontmatter_raw = yaml.safe_load(raw.split("---", 2)[1])
        assert isinstance(frontmatter_raw["allowed-tools"], str), (
            "the shared SKILL.md's allowed-tools regressed to a non-spec-compliant "
            "type -- it must be a bare space-separated string"
        )
        assert "," not in frontmatter_raw["allowed-tools"]

        skill = _load_conductor_skill(skill_dir)
        assert skill is not None
        assert isinstance(skill.frontmatter.allowed_tools, str)
        assert len(skill.frontmatter.allowed_tools.split(" ")) >= 2

    def test_patches_a_list_defensively_for_skills_not_yet_fixed(self, tmp_path):
        """_load_conductor_skill() must still convert list -> space-separated string
        for any skill directory that hasn't been corrected to the spec-compliant
        format, so a skill authored the old way doesn't crash the whole agent."""
        skill_dir = tmp_path / "some-other-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: some-other-skill\n"
            "description: A test skill with a legacy list-format allowed-tools.\n"
            "allowed-tools:\n"
            "  - tool_one\n"
            "  - tool_two\n"
            "---\n"
            "Body instructions.\n"
        )
        skill = _load_conductor_skill(skill_dir)
        assert skill is not None
        assert skill.frontmatter.allowed_tools == "tool_one tool_two"

    def test_description_under_gemini_budget(self):
        """500 chars is the Gemini-compatible description budget (cross-provider
        authoring convention this series follows from RULE-SKL01's spirit)."""
        skill = _load_conductor_skill(_SKILLS_ROOT / "conductor-troubleshoot-connector")
        assert skill is not None
        assert len(skill.frontmatter.description) <= 500

    def test_make_skills_toolset_returns_a_real_toolset(self):
        from google.adk.tools.skill_toolset import SkillToolset
        toolset = make_skills_toolset()
        assert isinstance(toolset, SkillToolset)

    def test_missing_skill_dir_is_soft_failure(self):
        result = _load_conductor_skill(_SKILLS_ROOT / "does-not-exist")
        assert result is None


# ---------------------------------------------------------------------------
# 8 -- BOM consistency
# ---------------------------------------------------------------------------

class TestBomConsistency:

    def test_bom_model_matches_agent_constant(self):
        bom = yaml.safe_load(BOM_PATH.read_text())
        assert bom["model"]["id"] == MODEL, (
            f"BOM model.id {bom['model']['id']!r} != agent.MODEL {MODEL!r}. "
            "Update MODEL in agent.py or agent-bom.yaml to re-sync."
        )

    def test_bom_all_source_files_exist(self):
        bom = yaml.safe_load(BOM_PATH.read_text())
        sprint_root = BOM_PATH.parent
        missing = [
            entry["file"] for entry in bom.get("tools", [])
            if not (sprint_root / entry["file"]).exists()
        ]
        assert not missing, f"BOM references missing files: {missing}"

    def test_bom_hashes_match_current_files(self):
        import hashlib
        bom = yaml.safe_load(BOM_PATH.read_text())
        sprint_root = BOM_PATH.parent
        mismatched = []
        for entry in bom.get("tools", []) + [bom.get("prompt", {})]:
            f = entry.get("file")
            expected = entry.get("sha256")
            if not f or not expected:
                continue
            path = sprint_root / f
            if not path.exists():
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                mismatched.append(f)
        assert not mismatched, f"BOM hash drift on: {mismatched}. Regenerate agent-bom.yaml."


# ---------------------------------------------------------------------------
# 9 -- Local vs deployed trace comparison: N/A, documented not skipped silently
# ---------------------------------------------------------------------------

def test_deployment_is_explicitly_out_of_scope():
    """Week 11's 'local vs deployed trace comparison' test requirement has no
    applicable target this lab -- README's Out of Scope explicitly excludes
    Cloud Run/Agent Engine deployment. This test documents the N/A rather than
    silently omitting the requirement."""
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    assert "Cloud Run" in readme and "Out of Scope" in readme
