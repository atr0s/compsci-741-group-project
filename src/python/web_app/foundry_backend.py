"""
Foundry Agent Service backend for web_app.py, plus the shared-secret gate.

Replaces the proxy to agent_service.py: instead of running a tool loop locally,
this hands the turn to the agent provisioned by scripts/provision_agent.py and
lets Foundry orchestrate the MCP calls server-side.

A turn is a *response* against a *conversation*, streamed. The per-turn values
the agent left as placeholders - the store context, the MCP secret, the RLS
identity - are supplied as structured inputs, which is what keeps one published
agent serving every store without the secret entering its definition.

The SSE contract is unchanged - chat.js:195 consumes {"content": ...},
{"error": ...} and a literal [DONE], so nothing in the frontend moves. What
changed is that the content frames are now real model deltas rather than a
finished answer cut into fixed-size pieces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from base64 import b64decode
from binascii import Error as BinasciiError
from dataclasses import dataclass, field
from secrets import compare_digest
from typing import AsyncGenerator, Dict, List, Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

import agents as agent_registry
from agents import Agent
from stores import is_known as is_known_store
from stores import resolve as resolve_store

# Paths that must answer without credentials: App Service pings the root of a
# site to decide whether the container came up, and /health is the probe.
_PUBLIC_PATHS = {"/health"}

# How long the response stream may go quiet before a comment frame is sent. A
# long MCP call is the one place it still can.
_KEEPALIVE_SECONDS = 10

# Queued by the stream pump when there is nothing more to come.
_END = object()


@dataclass(frozen=True)
class User:
    """One sign-in, and the stores it entitles.

    `stores` is the entitlement, not a preference: the first entry is the
    default, and a request naming anything outside the list is refused. It is
    the only place the store→user binding exists, which is what keeps the
    tenant out of the request.
    """

    name: str
    password: str
    stores: List[str] = field(default_factory=list)


def load_users(raw: Optional[str] = None) -> Dict[str, User]:
    """Parse WEB_AUTH_USERS into the credential table, or refuse to start.

    Format, one record per user, comma-separated:

        name:password:store|store|store

    Every failure here raises. This function used to be permissive, and a
    missing app setting silently published the retail database to the internet
    (docs/auth-poc-notes.md) - so an unset, empty or malformed table is a
    startup error, not a warning. Failing closed is the whole point.
    """
    raw = os.environ.get("WEB_AUTH_USERS", "") if raw is None else raw
    if not raw.strip():
        raise RuntimeError(
            "WEB_AUTH_USERS is not set. The chat app will not start without a "
            "credential table - see .env.example for the format."
        )

    users: Dict[str, User] = {}
    for record in (r.strip() for r in raw.split(",")):
        if not record:
            continue
        name, _, rest = record.partition(":")
        password, _, store_list = rest.partition(":")
        stores = [s.strip().lower() for s in store_list.split("|") if s.strip()]

        if not name or not password or not stores:
            raise RuntimeError(
                f"WEB_AUTH_USERS entry {record.split(':')[0]!r} is malformed; "
                "expected name:password:store|store"
            )
        unknown = [s for s in stores if not is_known_store(s)]
        if unknown:
            raise RuntimeError(
                f"WEB_AUTH_USERS entry {name!r} names unknown store(s) {unknown}; "
                "store keys must match src/python/web_app/stores.py"
            )
        if name in users:
            raise RuntimeError(f"WEB_AUTH_USERS lists {name!r} more than once")

        users[name] = User(name=name, password=password, stores=stores)

    if not users:
        raise RuntimeError("WEB_AUTH_USERS parsed to zero users")
    return users


def run_context(agent: Agent, store_label: str) -> str:
    """Per-run context the model cannot choose for itself.

    For a per-store agent the store is supplied, because letting the model pick
    its own tenant would defeat the authorisation design.

    For an all-stores agent there is nothing to supply: the shopper has not said
    which store they will use yet, and the model's job is to ask. What it is
    told instead is that no scoping has been applied - otherwise it assumes the
    numbers it gets back are already one store's and reports them as such.

    The date is deliberately NOT supplied in either case: it is fetched with
    get_current_utc_date so the temporal reasoning appears in the run trace as a
    real step. Stating it here would make the tool call pointless - a model told
    the date has no reason to look it up - so the two are mutually exclusive and
    this picks the observable one.
    """
    if agent.all_stores:
        return (
            "You are advising a retail customer, not a store manager.\n"
            "Nothing you retrieve is scoped to a store unless you scope it "
            "yourself: stock figures cover all 8 Zava stores until you pass a "
            "store_id to filter_candidates. Establish which store the customer "
            "will shop at before you present any stock figure.\n"
            "You do not know today's date and must not guess it."
        )
    return (
        f"You are advising the manager of the {store_label} store. "
        "Every figure you retrieve is already scoped to that store.\n"
        "You do not know today's date and must not guess it."
    )


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic over the whole app, resolving WHICH user signed in.

    Basic rather than a bearer token or a custom header because chat.js talks to
    /chat/stream through an EventSource, which cannot set request headers. The
    browser caches Basic credentials per origin and attaches them to the SSE
    request on its own; any scheme that needs a header would authenticate the
    page and then silently fail the stream.

    On success the authenticated User is attached to request.state. Everything
    downstream reads the store from there - never from the query string - which
    is what makes the store a consequence of signing in rather than a request
    parameter anyone can type.
    """

    # Compared against when the username is unknown, so a valid username does
    # not answer measurably faster than an invalid one.
    _DUMMY = "x" * 32

    def __init__(self, app, users: Dict[str, User]) -> None:
        super().__init__(app)
        self._users = users

    def _authenticate(self, header: str) -> Optional[User]:
        if not header.startswith("Basic "):
            return None
        try:
            name, _, password = b64decode(header[6:]).decode("utf-8").partition(":")
        except (BinasciiError, UnicodeDecodeError):
            return None

        user = self._users.get(name)
        # Always run a comparison, even on a miss, so timing does not reveal
        # which usernames exist.
        if compare_digest(password, user.password if user else self._DUMMY) and user:
            return user
        return None

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        user = self._authenticate(request.headers.get("authorization", ""))
        if user is not None:
            request.state.user = user
            return await call_next(request)

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Zava DIY Assistant"'},
        )


class FoundryAgentBackend:
    """One turn of conversation against a Foundry agent, streamed as SSE."""

    def __init__(self) -> None:
        # No single agent name: it is resolved per turn from the agent the caller
        # picked, and the agent decides the identity the run carries (agents.py).
        self.project_endpoint = os.environ["PROJECT_ENDPOINT"]
        self.mcp_server_url = os.environ["MCP_SERVER_URL"]
        self.mcp_api_key = os.environ["MCP_API_KEY"]
        self._conversations: Dict[str, str] = {}
        self._openai = None

    def _ensure_client(self):
        """Build the SDK client lazily so import never blocks container start.

        Conversations and responses are the OpenAI-compatible surface Foundry
        exposes under the project endpoint, so the client is obtained from the
        project client rather than constructed directly - that is what gives it
        the managed identity this app signs in with.
        """
        if self._openai is None:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential

            project = AIProjectClient(
                endpoint=self.project_endpoint,
                credential=DefaultAzureCredential(),
            )
            self._openai = project.get_openai_client()
        return self._openai

    def _structured_inputs(self, rls_user_id: str, context: str) -> Dict[str, str]:
        """The values the stored agent version left as placeholders.

        Built fresh per turn rather than cached, because x-rls-user-id differs
        per user. Caching one set would pin every conversation to whichever
        store happened to ask first - the bug this method exists to avoid.

        These belong to the turn, not the stored agent, which is what keeps the
        shared secret out of the agent definition. The
        names must match scripts/provision_agent.py.
        """
        return {
            "run_context": context,
            "mcp_api_key": self.mcp_api_key,
            "rls_user_id": rls_user_id,
        }

    async def _warm_mcp(self) -> bool:
        """Wait for the MCP server to actually answer before handing off the turn.

        The MCP endpoint has no Always On, so it idles out and the next caller
        pays a cold start. Foundry's connector will not wait that long: it fails
        the whole turn with `MCP Connector error. Http status: 424`, which the
        user sees as a broken agent rather than a slow one.

        This used to be a single best-effort GET, which returned as soon as the
        platform answered *anything* - including while the worker was still
        starting - and the run failed anyway (observed 2026-09-14). Poll until
        the server really returns 200, or until we have waited longer than the
        cold start should take.
        """
        health = self.mcp_server_url.rsplit("/mcp", 1)[0] + "/healthz"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 150

        async with httpx.AsyncClient(timeout=30.0) as client:
            while loop.time() < deadline:
                try:
                    if (await client.get(health)).status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)

        print("MCP warm-up never reached /healthz 200; turn may fail with 424")
        return False

    def _conversation_for(self, session_id: str, agent_key: str, store_key: str) -> str:
        """Map a browser session to a Foundry conversation.

        In-memory on purpose: F1 runs a single instance, so there is no replica
        to lose affinity with. Moving off F1, or adding an instance, means moving
        this to Postgres or every other request lands on a worker that has never
        heard of the conversation.
        """
        client = self._ensure_client()
        # Keyed by agent and store as well as session. Store, because switching
        # store must start a fresh conversation or the previous store's figures
        # stay in context and the model carries them across the boundary in its
        # next answer. Agent, because the two have different instructions and
        # different visibility - replaying a manager's conversation into the
        # customer-facing agent would hand it figures it should have had to ask
        # for.
        key = f"{session_id}:{agent_key}:{store_key}"
        if key not in self._conversations:
            self._conversations[key] = client.conversations.create().id
        return self._conversations[key]

    def reset(self, session_id: str) -> None:
        prefix = f"{session_id}:"
        for key in [k for k in self._conversations if k.startswith(prefix)]:
            self._conversations.pop(key, None)

    async def stream(self, message: str, session_id: str, store_key: str = "",
                     agent_key: str = "") -> AsyncGenerator[str, None]:
        loop = asyncio.get_running_loop()

        agent = agent_registry.resolve(agent_key)
        agent_name = agent_registry.agent_name_for(agent)
        if agent_name is None:
            detail = (f"{agent.label} is not provisioned. "
                      f"Run: python scripts/provision_agent.py {agent.key}")
            yield f"data: {json.dumps({'error': detail})}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Emitted before the warm-up so the browser sees the stream open rather
        # than a silent connection for the length of a cold start.
        yield ": warming tools\n\n"
        if not await self._warm_mcp():
            yield f"data: {json.dumps({'error': 'The tool server did not come up in time. Try again in a moment.'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        try:
            store = resolve_store(store_key)
            conversation_id = await loop.run_in_executor(
                None, self._conversation_for, session_id, agent.key, store["key"]
            )
            # For an all-stores agent this discards the signed-in user's store
            # and runs as super-manager; for a per-store agent it is the store
            # the sign-in entitled them to. Either way the model has no say.
            #
            # A per-store agent is told WHICH store it advises for, but never
            # gets a say in it: the label is per-turn context, the identity that
            # actually filters rows is the x-rls-user-id header the same inputs
            # render. Telling it the name costs nothing - a label is not a
            # capability. An all-stores agent is told the opposite: nothing is
            # scoped yet, so it must ask.
            inputs = self._structured_inputs(
                agent_registry.rls_user_id_for(agent, store["rls_user_id"]),
                run_context(agent, store["label"]),
            )

            # The OpenAI client is synchronous, so the response stream is drained
            # in a worker thread and finished SSE frames come back over a queue.
            # Waiting on the queue with a timeout is what lets this emit keep-alive
            # frames during a long tool call without a polling loop.
            queue: asyncio.Queue = asyncio.Queue()
            pump = loop.run_in_executor(
                None, self._pump, loop, queue, agent_name, conversation_id, message, inputs
            )

            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # EventSource ignores lines starting with ':'.
                    yield ": ping\n\n"
                    continue
                if frame is _END:
                    break
                yield frame

            await pump
            yield "data: [DONE]\n\n"

        except Exception as exc:  # surfaced in the UI rather than a blank stream
            yield f"data: {json.dumps({'error': f'{type(exc).__name__}: {exc}'})}\n\n"
            yield "data: [DONE]\n\n"

    def _pump(self, loop, queue: asyncio.Queue, agent_name: str, conversation_id: str,
              message: str, inputs: Dict[str, str]) -> None:
        """Drain one response stream in a worker thread, emitting SSE frames.

        Translation happens here rather than in the caller so that only finished
        frames cross the thread boundary. Every exit path queues _END, including
        the failure paths - a stream that ends without it would leave the caller
        emitting keep-alives forever.
        """
        def emit(frame) -> None:
            # The browser can close the stream mid-turn, and the loop may be gone
            # by the time this runs. Nobody is listening at that point, so a dead
            # loop ends the pump quietly rather than raising out of `finally`.
            try:
                loop.call_soon_threadsafe(queue.put_nowait, frame)
            except RuntimeError:
                pass

        try:
            events = self._ensure_client().responses.create(
                conversation=conversation_id,
                input=message,
                stream=True,
                extra_body={
                    "agent_reference": {"name": agent_name, "type": "agent_reference"},
                    "structured_inputs": inputs,
                },
            )
            for event in events:
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    emit(f"data: {json.dumps({'content': event.delta})}\n\n")
                elif kind == "response.output_item.added":
                    label = self._narration_for(getattr(event, "item", None))
                    if label:
                        emit(f"data: {json.dumps({'status': label})}\n\n")
                elif kind in ("response.failed", "response.incomplete"):
                    emit(f"data: {json.dumps({'error': self._failure_detail(event)})}\n\n")
                elif kind == "error":
                    detail = getattr(event, "message", None) or "stream error"
                    emit(f"data: {json.dumps({'error': f'Agent stream error: {detail}'})}\n\n")
        except Exception as exc:  # noqa: BLE001 - reported to the browser, not raised
            logging.debug("response stream failed", exc_info=True)
            emit(f"data: {json.dumps({'error': f'{type(exc).__name__}: {exc}'})}\n\n")
        finally:
            emit(_END)

    # What each tool is doing, in the customer's terms. A tool missing from the
    # map still yields a line, so a new server-side tool degrades to something
    # readable rather than to silence.
    _TOOL_NARRATION = {
        "get_stores": "Looking up Zava's stores\u2026",
        "get_taxonomy": "Checking the catalogue's categories\u2026",
        "search_by_text": "Searching the catalogue by description\u2026",
        "search_by_keyword": "Searching by product name and trade terms\u2026",
        "filter_candidates": "Checking live prices and stock\u2026",
        "get_current_utc_date": "Checking today's date\u2026",
    }

    @classmethod
    def _narration_for(cls, item) -> Optional[str]:
        """Narration for a tool call the model has just started.

        Driven by the stream rather than re-read from the turn's steps, so each
        call is announced exactly once and filter_candidates announced while
        narrowing and again while verifying still reads as two steps.
        """
        if getattr(item, "type", None) != "mcp_call":
            return None
        name = getattr(item, "name", None) or "tool"
        return cls._TOOL_NARRATION.get(name, f"Running {name}\u2026")

    @staticmethod
    def _failure_detail(event) -> str:
        """Why the turn did not finish, in as much detail as the event carries."""
        response = getattr(event, "response", None)
        error = getattr(response, "error", None)
        if error is not None:
            return f"Agent run failed: {getattr(error, 'message', None) or error}"
        incomplete = getattr(response, "incomplete_details", None)
        reason = getattr(incomplete, "reason", None) or getattr(event, "type", "unknown")
        return f"Agent run did not finish: {reason}"
