"""Agent registry: which Foundry agent a turn runs against, and how it is scoped.

The only thing the app tier needs to know about an agent is how it scopes
data, because that decides the identity the run carries:

  diy       A customer-facing project advisor. A shopper is not a tenant, so the
            run carries the super-manager identity and every store's stock is
            visible. The store the shopper names becomes a `store_id` FILTER the
            model passes to filter_candidates - a query argument, not an
            authorisation claim.

`diy` deliberately widens visibility, and what bounds it is the agent's
allowed_tools rather than trust: every tool it may call reads products and
inventory only. Orders and customers stay unreachable because nothing in its
tool list queries them. Adding an order-reading tool to that agent would turn a
catalogue widening into a data breach, so treat the two lists as one decision.

Names are written here by scripts/provision_agent.py, one app setting per agent.
A name, not an id: Foundry agents are addressed by name and version, and naming
the agent alone means a re-provisioned prompt takes effect on the next turn
without an app-settings change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from stores import SUPER_MANAGER_UUID


@dataclass(frozen=True)
class Agent:
    key: str
    label: str
    # App setting provision_agent.py writes the published agent's name into.
    env_var: str
    # True: run as super-manager, store chosen in conversation as a filter.
    # False: run as the signed-in user's store, enforced by RLS.
    all_stores: bool
    blurb: str


AGENTS: list[Agent] = [
    Agent(
        key="diy",
        label="DIY project advisor",
        env_var="AGENT_NAME_DIY",
        all_stores=True,
        blurb="Turns a project into a costed list of real Zava products. Asks which store you will shop at.",
    ),
]

_BY_KEY = {agent.key: agent for agent in AGENTS}

DEFAULT_AGENT_KEY = "diy"


def resolve(agent_key: Optional[str]) -> Agent:
    """Look up an agent, falling back to the default.

    Falls back to `diy` rather than to whichever agent was asked for, because an
    unrecognised key must not silently land on the manager-facing agent.
    """
    return _BY_KEY.get((agent_key or "").strip().lower(), _BY_KEY[DEFAULT_AGENT_KEY])


def agent_name_for(agent: Agent) -> Optional[str]:
    """The provisioned Foundry agent name, or None if it was never provisioned."""
    return (os.getenv(agent.env_var) or "").strip().strip('"') or None


def available() -> list[Agent]:
    """Agents that actually have an id configured.

    An agent nobody provisioned is not offered, so the picker cannot put the
    user on a turn that will fail the moment the agent is resolved.
    """
    return [agent for agent in AGENTS if agent_name_for(agent)]


def rls_user_id_for(agent: Agent, store_rls_user_id: str) -> str:
    """The identity this run carries.

    For an all-stores agent the signed-in user's store is deliberately ignored:
    the shopper is not a tenant, and the store they name is applied later as a
    filter the model passes. For everything else the store's own id is the whole
    authorisation story.
    """
    return SUPER_MANAGER_UUID if agent.all_stores else store_rls_user_id
