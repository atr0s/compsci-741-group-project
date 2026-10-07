"""
Create (or update) the Zava agents as Foundry Agent Service agents.

Each agent in AGENTS below has its own prompt, tool surface and data scope (see
also src/python/web_app/agents.py), and writes its name to its own .env key.

The agent is not infrastructure - it is a data-plane object with its own
lifecycle, so it lives here rather than in Bicep.

Agent versions are immutable: running this twice does not mutate the agent, it
publishes a NEW version under the same name. The app tier references the agent
by name and therefore picks up the new version on its next turn, with no app
setting to update - so re-running this script is the whole deploy step for a
prompt or tool-surface change.

Foundry runs the tool loop inside Azure and calls the MCP server *outbound over
the public internet*, so MCP_SERVER_URL must be a public HTTPS
endpoint. The shared secret in MCP_API_KEY is what stands between that endpoint
and the database - RLS is no defence, because x-rls-user-id is chosen by the
caller. That secret is NOT needed to provision: it is a
structured input the app supplies per turn, so it never enters a stored version.

Usage:
    python scripts/provision_agent.py                  # diy (the default)
    python scripts/provision_agent.py all              # every agent in AGENTS
    python scripts/provision_agent.py diy --smoke      # ... then run a test turn
    python scripts/provision_agent.py diy --delete     # remove it
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from azure.ai.projects.models import (
    MCPTool,
    PromptAgentDefinition,
    StructuredInputDefinition,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
PROMPT_DIR = REPO_ROOT / "src" / "python" / "workshop" / "prompts"

SERVER_LABEL = "zava_catalog"

# The three values that belong to a TURN rather than to the agent, declared here
# as handlebar placeholders and supplied per response by the app tier
# (src/python/web_app/foundry_backend.py).
#
# This is what replaces the old per-run `tool_resources`: a stored version holds
# only the shape of the MCP call, never the secret and never a tenant, which is
# what lets one agent serve every store and keeps MCP_API_KEY out of the
# persisted definition.
RUN_CONTEXT_INPUT = "run_context"
API_KEY_INPUT = "mcp_api_key"
RLS_USER_INPUT = "rls_user_id"

# Fallback identity for --smoke only. Matches stores.SUPER_MANAGER_UUID; not
# imported, because this script must run without the web app on the path.
DEFAULT_SMOKE_RLS_USER_ID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class AgentSpec:
    """One provisionable agent: its prompt, its tool surface, where its name goes.

    `allowed_tools` is declared per agent rather than per server, so a tool added
    server-side does not silently enter an agent's reach. Keep each list to the
    tools its prompt actually names - a tool the instructions never mention is a
    surface the prompt cannot govern.

    For `diy` the list is also a containment boundary, not just scoping: that
    agent runs with all-stores visibility (src/python/web_app/agents.py), and
    what keeps the widening to the catalogue is that none of these tools reads
    orders or customers. Adding one that does changes the blast radius.
    """

    key: str
    name: str
    prompt: str
    env_var: str
    allowed_tools: list[str]


AGENTS: dict[str, AgentSpec] = {
    "diy": AgentSpec(
        key="diy",
        name="zava-diy-advisor",
        prompt="diy_assistant.md",
        env_var="AGENT_NAME_DIY",
        allowed_tools=[
            # Grounding
            "get_taxonomy",
            "get_stores",
            "get_stock_by_store",
            "get_current_utc_date",
            # Retrieval - the two search arms the prompt's recipe runs
            "search_by_text",
            "search_by_keyword",
            # Constraint, and the verification pass before the final answer
            "filter_candidates",
        ],
    ),
}


def require(name: str) -> str:
    value = os.getenv(name, "").strip().strip('"')
    if not value:
        sys.exit(f"{name} is not set. Add it to {ENV_PATH} and retry.")
    return value


def build_mcp_tool(spec: AgentSpec) -> MCPTool:
    """The MCP tool declaration, with both headers left as placeholders.

    The header NAMES are part of the contract with the MCP server and are fixed
    here; only their values are templated, which is the supported shape for MCP
    structured inputs. The server URL stays literal, because one deployment
    talks to one catalogue and a templated URL would let a caller redirect the
    tool loop at a server of their choosing.
    """
    return MCPTool(
        server_label=SERVER_LABEL,
        server_url=require("MCP_SERVER_URL"),
        allowed_tools=spec.allowed_tools,
        headers={
            "x-api-key": f"{{{{{API_KEY_INPUT}}}}}",
            "x-rls-user-id": f"{{{{{RLS_USER_INPUT}}}}}",
        },
        # Without this every tool call returns an mcp_approval_request that
        # nobody answers, and the turn ends with no answer and no error.
        require_approval="never",
    )


def build_instructions(spec: AgentSpec) -> str:
    """The prompt file, plus the slot the per-turn context is rendered into.

    The placeholder goes last so the rendered instructions read exactly as the
    old `additional_instructions` did - prompt first, then the facts about this
    particular turn (which store, whether anything is scoped at all).
    """
    prompt_path = PROMPT_DIR / spec.prompt
    if not prompt_path.exists():
        sys.exit(f"prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").rstrip() + f"\n\n{{{{{RUN_CONTEXT_INPUT}}}}}\n"


def structured_inputs() -> dict[str, StructuredInputDefinition]:
    """Schemas for the per-turn values. All required, none defaulted.

    No default on `rls_user_id` on purpose: a missing tenant must fail the turn,
    not quietly fall back to whatever identity happened to be convenient. The
    widest identity is the one a default would reach for, and that is exactly
    the failure mode worth refusing.
    """
    return {
        RUN_CONTEXT_INPUT: StructuredInputDefinition(
            description="Per-turn context: which store is being advised, or that nothing is scoped yet.",
            required=True,
            schema={"type": "string"},
        ),
        API_KEY_INPUT: StructuredInputDefinition(
            description="Shared secret for the Zava MCP server (x-api-key).",
            required=True,
            schema={"type": "string"},
        ),
        RLS_USER_INPUT: StructuredInputDefinition(
            description="Postgres RLS identity this turn runs as (x-rls-user-id).",
            required=True,
            schema={"type": "string"},
        ),
    }


def publish_version(project, spec: AgentSpec, model: str):
    """Publish a new immutable version of this agent."""
    agent = project.agents.create_version(
        agent_name=spec.name,
        definition=PromptAgentDefinition(
            model=model,
            instructions=build_instructions(spec),
            tools=[build_mcp_tool(spec)],
            structured_inputs=structured_inputs(),
        ),
        description=f"Zava {spec.key} agent, provisioned from scripts/provision_agent.py",
    )
    print(f"published {agent.name} version {agent.version}")
    return agent


def persist_agent_name(spec: AgentSpec, agent_name: str, env_path: Path = ENV_PATH) -> None:
    """Record this agent's name in .env under its own key.

    The name, not an id: agents no longer have a GUID, they are addressed by
    name and version, and referencing the name alone means the app runs the
    latest published version without an app-settings change.

    One variable per agent rather than a shared one, because both agents are
    provisioned and the app tier resolves which to run per turn
    (src/python/web_app/agents.py). The variable's presence is also how the app
    decides an agent is available at all, so an agent nobody provisioned is
    never offered in the picker.
    """
    line = f'{spec.env_var}="{agent_name}"'
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if re.search(rf"^{spec.env_var}=.*$", text, flags=re.M):
        text = re.sub(rf"^{spec.env_var}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n\n# Foundry agent, created by scripts/provision_agent.py\n{line}\n"
    env_path.write_text(text, encoding="utf-8")
    print(f"wrote {spec.env_var} to {env_path}")


def smoke_test(openai_client, spec: AgentSpec) -> int:
    """One real turn that must reach the MCP server to answer correctly.

    Runs with the RLS identity in .env rather than a store's, because this is
    checking the wiring - agent resolves, structured inputs render, the MCP
    connector authenticates - not the authorisation design.
    """
    rls_user_id = os.getenv("RLS_USER_ID", "").strip().strip('"') or DEFAULT_SMOKE_RLS_USER_ID
    conversation = openai_client.conversations.create()

    response = openai_client.responses.create(
        conversation=conversation.id,
        input="What product categories does Zava carry? Use your tools and list a few.",
        extra_body={
            "agent_reference": {"name": spec.name, "type": "agent_reference"},
            "structured_inputs": {
                RUN_CONTEXT_INPUT: "This is a provisioning smoke test. Nothing is scoped to a store.",
                API_KEY_INPUT: require("MCP_API_KEY"),
                RLS_USER_INPUT: rls_user_id,
            },
        },
    )

    print(f"response status: {response.status}")
    if getattr(response, "error", None):
        print(f"error: {response.error}")

    calls = [item for item in response.output if getattr(item, "type", None) == "mcp_call"]
    print(f"tool calls made: {len(calls)}")
    for call in calls:
        detail = f"  - {call.server_label} {call.name}"
        if getattr(call, "error", None):
            detail += f"  ERROR: {call.error}"
        print(detail)

    # An approval request here means require_approval did not carry through -
    # the turn will look like it simply stopped.
    if any(getattr(item, "type", None) == "mcp_approval_request" for item in response.output):
        print("run parked on an approval request - approval mode did not apply")

    text = response.output_text or ""
    if text:
        print("\n--- assistant ---")
        print(text[:1200])

    return 0 if response.status == "completed" and text else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", nargs="?", choices=sorted(AGENTS) + ["all"], default="diy",
                        help="which agent to provision (default: diy; 'all' does both)")
    parser.add_argument("--smoke", action="store_true", help="run a test turn after provisioning")
    parser.add_argument("--delete", action="store_true", help="delete the agent and exit")
    parser.add_argument("--env-file", type=Path, default=ENV_PATH,
                        help="environment file to read settings from and write agent names to "
                             "(default: the repo's .env; deploy/deploy.sh passes its own)")
    args = parser.parse_args()

    load_dotenv(args.env_file, override=False)

    from azure.ai.projects import AIProjectClient

    project = AIProjectClient(
        endpoint=require("PROJECT_ENDPOINT"),
        credential=DefaultAzureCredential(),
    )

    specs = list(AGENTS.values()) if args.agent == "all" else [AGENTS[args.agent]]

    if args.delete:
        for spec in specs:
            try:
                project.agents.delete(spec.name)
            except ResourceNotFoundError:
                print(f"no agent named {spec.name}")
                continue
            print(f"deleted {spec.name} (all versions)")
        return 0

    model = require("GPT_MODEL_DEPLOYMENT_NAME")
    rc = 0
    for spec in specs:
        agent = publish_version(project, spec, model)
        persist_agent_name(spec, agent.name, args.env_file)

        print(f"\nagent   : {agent.name} (version {agent.version})")
        print(f"model   : {model}")
        print(f"prompt  : {spec.prompt}")
        print(f"mcp     : {require('MCP_SERVER_URL')}")
        print(f"tools   : {len(spec.allowed_tools)} allowed")
        print(f"inputs  : {', '.join(sorted(structured_inputs()))}\n")

        if args.smoke:
            rc |= smoke_test(project.get_openai_client(), spec)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
