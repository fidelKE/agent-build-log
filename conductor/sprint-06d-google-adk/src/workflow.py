"""
Google ADK workflow graphs for Conductor's Setup and Onboarding modes -- Sprint 6d.

RULE-ADK03: Setup mode uses a Workflow graph so step order (read -> validate ->
configure -> enable) is enforced by graph structure, not prompt text. Each sub-agent
is scoped to exactly one tool -- the model cannot call write_connector_config from
SetupReadAgent because the tool does not exist in that agent's tools= list, and the
graph has no edge from START or SetupReadAgent to SetupConfigureAgent directly. This
is the direct fix for the "workaround, not structural" finding from Lab 6c: dedicated
agents per step, not one agent with prompt-injected sequencing.

RULE-ADK01: every leaf here is an LlmAgent; the Workflow graph is a structural
wrapper only, never an execution unit itself.

Migration note: SequentialAgent/ParallelAgent (both @deprecated in installed
google-adk==2.6.1, in favor of this Workflow graph engine) were the original
implementation. Confirmed live against the installed package -- not from docs, which
don't cover this yet -- that build_node() in
google.adk.workflow.utils._workflow_graph_utils accepts a plain LlmAgent directly as
a NodeLike edge endpoint: it clones the agent and casts it into the graph. A chain
tuple (START, a, b, c) parses into strict pairwise edges START->a, a->b, b->c
(google.adk.workflow.utils._graph_parser._process_chain) -- exactly the ordering
guarantee RULE-ADK03 needs, and inspectable directly on wf.graph.edges for tests.

Onboarding fans out from START to three independent LlmAgent nodes with no edges
between them -- Workflow schedules every node whose in-edges are satisfied
concurrently, so three edges all sourced from START run in parallel. Each branch
writes its result via output_key; the caller reads session.state after the run to
assemble the combined answer (see agent.py).

make_model: agent.py's zero-arg factory (GatewayGemini(model=MODEL)) -- called once
per LlmAgent here so each node gets its own model instance, not a shared string.
Routes through the existing LLM_GATEWAY_URL/ANTHROPIC_API_KEY; see agent.py.

Live-discovered constraint (not documented anywhere yet): a Workflow must have at
most one terminal node -- three independent parallel branches each producing their
own output raises "multiple terminal nodes produced output" at run end. Fixed with
a JoinNode (ADK's own primitive for "wait for all predecessors"): a single chain
tuple (START, (status_agent, catalog_agent, memory_agent), join) fans out from START
to all three, then fans every one of them into the join node, satisfying the
single-terminal-output rule while keeping the branches genuinely concurrent -- ADK
still runs all three at once, JoinNode just waits for all three before completing.
"""

from google.adk import Workflow
from google.adk.agents.llm_agent import LlmAgent
from google.adk.workflow import START, JoinNode


def build_setup_workflow(make_model, tool_functions: dict, before_tool_callback, after_tool_callback) -> Workflow:
    read_agent = LlmAgent(
        name="SetupReadAgent",
        model=make_model(),
        instruction=(
            "Call read_connector_config with the connector_id the user gave you. "
            "Report the current config back in one short sentence."
        ),
        tools=[tool_functions["read_connector_config"]],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        output_key="setup_read_result",
    )
    validate_agent = LlmAgent(
        name="SetupValidateAgent",
        model=make_model(),
        instruction=(
            "The prior step's result is in state['setup_read_result']. Call "
            "validate_credentials for the same connector_id. Report whether "
            "credentials are valid in one short sentence."
        ),
        tools=[tool_functions["validate_credentials"]],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        output_key="setup_validate_result",
    )
    configure_agent = LlmAgent(
        name="SetupConfigureAgent",
        model=make_model(),
        instruction=(
            "The prior step's result is in state['setup_validate_result']. If it "
            "reports credentials are invalid, do NOT call write_connector_config -- "
            "respond with exactly: 'HALTED: credentials invalid, write skipped.' "
            "Only when it reports valid credentials, call write_connector_config with "
            "the connector_id and the config changes the user asked for, then report "
            "what was written in one short sentence."
        ),
        tools=[tool_functions["write_connector_config"]],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        output_key="setup_configure_result",
    )
    enable_agent = LlmAgent(
        name="SetupEnableAgent",
        model=make_model(),
        instruction=(
            "The prior step's result is in state['setup_configure_result']. If it "
            "starts with 'HALTED', do NOT call check_connector_status -- relay that "
            "the setup did not complete and why, in one sentence. Only when the write "
            "actually happened, call check_connector_status for the same connector_id "
            "to confirm it is live, then summarize the whole setup outcome for the "
            "user in 1-2 sentences."
        ),
        tools=[tool_functions["check_connector_status"]],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        output_key="final_result",
    )
    return Workflow(
        name="ConductorSetupWorkflow",
        edges=[
            (START, read_agent, validate_agent, configure_agent, enable_agent),
        ],
    )


def build_onboarding_workflow(make_model, tool_functions: dict, before_tool_callback, after_tool_callback) -> Workflow:
    status_agent = LlmAgent(
        name="OnboardingStatusAgent",
        model=make_model(),
        instruction=(
            "Call check_connector_status for the connector_id mentioned by the user "
            "and summarize it in one sentence."
        ),
        tools=[tool_functions["check_connector_status"]],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        output_key="onboarding_status_result",
    )
    catalog_agent = LlmAgent(
        name="OnboardingCatalogAgent",
        model=make_model(),
        instruction=(
            "Call search_knowledge_base with a query built from the user's message "
            "and summarize the top result in one sentence."
        ),
        tools=[tool_functions["search_knowledge_base"]],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        output_key="onboarding_catalog_result",
    )
    memory_agent = LlmAgent(
        name="OnboardingMemoryAgent",
        model=make_model(),
        instruction=(
            "Call search_memory with query set to the user's message and user_id set "
            "to '{user_id}'. Summarize any relevant prior context in one sentence, or "
            "say there is none."
        ),
        tools=[tool_functions["search_memory"]],
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        output_key="onboarding_memory_result",
    )
    join = JoinNode(name="OnboardingJoin")
    return Workflow(
        name="ConductorOnboardingWorkflow",
        edges=[
            (START, (status_agent, catalog_agent, memory_agent), join),
        ],
    )
