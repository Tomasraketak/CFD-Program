"""In-application AI assistant, backed by OpenRouter.

Lets the operator type a request in ordinary language -- "mesh this rocket and
find the worst-case hinge torque between Mach 1 and 3" -- and have it carried
out. The assistant reaches the platform through exactly the twelve tools the
MCP server exposes, so it can do what a human operator can do and nothing
else. There is no shell access, no file-system access and no code execution:
the tool list *is* the boundary.

Two design points worth stating.

**Schemas come from the MCP server**, not a second copy. Tool descriptions and
parameter bounds are read from ``mcp_server.server.list_tools()`` and
converted to the OpenAI function-calling shape. A parameter added to a model
appears in the GUI form, the MCP schema and the assistant's tool list at once.

**HTTP sits behind an interface.** :class:`OpenRouterClient` makes the real
calls; :class:`FakeChatClient` replays scripted responses. That is the same
arrangement the SU2 solver uses, and it is what lets the whole agent loop --
tool dispatch, multi-step reasoning, error recovery, iteration limits -- be
tested without a network or an API key.
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

from backend.runner import cancel_all_runs

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Sent so OpenRouter can attribute traffic; both are optional in their API.
REFERER = "https://github.com/Tomasraketak/CFD-Program"
APPLICATION_TITLE = "AeroThermalStudio"

# Model used when the operator has not chosen one. OpenRouter's catalogue
# changes, so this is a starting point rather than a promise: the assistant
# lists live models from the API and the interface lets any id be typed.
DEFAULT_MODEL = "deepseek/deepseek-chat"

# Models offered in the interface before the live list has been fetched.
# OpenRouter's catalogue changes weekly and an id that is not on it is
# rejected at the first request, so this is a shortlist, not a guarantee --
# "Fetch available models" replaces it with what the account can reach, and
# any id at all can be typed into the box.
SUGGESTED_MODELS = (
    "deepseek/deepseek-v4.1-flash",
    "meta/muse-spark-1.3-contributor",
    "qwen/qwen3.7-flash",
    "openai/gpt-6-luna",
    "deepseek/deepseek-chat",
)

# How many tool-calling rounds one request may take before the loop stops.
# A confused model can otherwise call tools indefinitely at the user's expense.
MAX_TOOL_ROUNDS = 12

# Network timeouts in seconds. Generous, because a long prompt to a slow model
# legitimately takes a while: a reasoning model writing a long answer in one
# round can be silent for several minutes without anything being wrong.
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 600.0

# Server responses worth trying again. 408 and 425 are the server asking for
# a repeat; 429 is rate limiting; 5xx is the provider having a bad moment.
# Everything else -- a bad key, no credit, a malformed request -- will fail
# exactly the same way the second time.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# How many times one HTTP call may be attempted, and the gap before each
# retry. Short enough not to look like a hang, long enough for a provider to
# come back.
DEFAULT_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = (2.0, 8.0, 20.0)

# Never wait longer than this for a server's own Retry-After. Beyond it the
# operator would rather be told than left staring at a frozen window.
_MAX_RETRY_AFTER_S = 30.0

# The tool whose picture the operator must answer before work goes on.
PREVIEW_TOOL = "preview_orientation"

AWAITING_CONFIRMATION_TEXT = (
    "Here is how the air will meet the model. Is the direction of the air "
    "and the tilt axis what you want? Reply to confirm, or tell me what to "
    "change."
)


def tool_calls_run(invocations: list["ToolInvocation"], count: int) -> list["ToolInvocation"]:
    """The invocations made in the latest round."""
    return invocations[-count:] if count else []


SYSTEM_PROMPT = """\
You are the assistant built into AeroThermalStudio, a CFD and thermal
simulation program. You help the operator run rocket aerodynamics and sensor
microclimate studies by calling the tools provided.

Working rules:

- Units are SI: metres, m/s, newtons, pascals, kelvin. Angles are in degrees
  and some temperatures in Celsius, as each parameter's description states.
- Speed is always two arguments: velocity_type and a bare number in
  velocity_val (fixed_params "velocity_type"/"velocity_val" in a sweep).
  Mach: velocity_type "mach", velocity_val 0.8 (range 0.05-3.5). True
  airspeed: velocity_type "tas", velocity_val in m/s, e.g. 250. Never pass
  strings such as "250 m/s", "M0.8" or "Mach 2". Convert km/h (/ 3.6) and
  knots (x 0.5144) to m/s first, and say which you used.
- Angles of attack and sideslip go up to +/-90 deg. Beyond +/-20 the flow
  separates massively and steady RANS is only indicative; say so when you
  run there, and the result's notes will too.
- The pivot the model tilts about can be moved up or down with
  pivot_height_m (metres; + is the side a positive angle of attack lifts the
  model towards, - the other). It is drawn in preview_orientation and sets
  the point pitching moments are taken about; it does not change the flow,
  because the solver tilts the air rather than the model. To make positive
  angles pitch the other way, flip the sign of pitch_axis (e.g. '-Z').
- Meshing is expensive and does not depend on the flight condition, because
  angle of attack and Mach are applied by the solver. Reuse one mesh_id across
  simulations and sweeps; only remesh when the geometry, domain or mesh
  settings change.
- Start at 'coarse' resolution to validate a setup, then refine.
- Radiation-shield (thermometer screen) studies go through
  radiation_shield_study: a DoE over wind speed, top solar and bottom
  radiation, a response surface, and a Monte Carlo on that surface. Start
  with action 'analytic' (instant screening model); for real numbers use
  'prepare_cfd' (meshes the 2 x 2 x 1.44 m domain, writes SU2 cases and an
  ANSYS Fluent/Workbench package) then 'solve_cfd' (one SU2 solve per
  design point, 15 for the default CCD -- hours), or 'import' a CSV solved
  in Fluent. A dark vehicle roof is setup {"bottom_mode":
  "roof_temperature", "bottom_temperature_k": 343}. Report the worst case,
  its inputs, the reliability and the surface's leave-one-out error, and
  say which evaluator produced the numbers. SU2 has no standard k-epsilon:
  its cases use SST, the Fluent package uses k-epsilon as specified.
- Runtimes vary enormously: the analytical sensor model returns in
  milliseconds, a mesh takes minutes, and a sweep takes its point count times
  3-8 minutes. Tell the operator what a step will cost before starting it.
- Report what the tools actually returned. If 'converged' is false or
  'within_target_band' is false, say so rather than presenting the number as
  settled fact.
- A solve that diverges is rescued automatically. If even the rescue fails,
  do not remesh or resize the domain on a hunch: check the mesh's
  min_quality, then retry with convective_scheme 'ROE', cfl_number 0.5,
  cfl_growth 1.05 and a low cfl_max such as 10. A low cfl_number alone is
  not a low CFL, because the adaptive ramp decides where it goes.
- Before meshing or solving a setup the operator has not seen -- a new file,
  a changed nose direction, body kind or tilt axis, or a new angle of attack
  or sideslip -- call preview_orientation with exactly those settings (once
  per angle you will run), say in a sentence what the picture shows, ask the
  operator whether the air direction and tilt axis are what they want, and
  end your turn. The meshing and solving tools refuse until they have
  answered. If they correct something, change it and preview again.
- Models can be rockets or fins. A body whose longest side is more than 3.5
  times each of the others is a rocket; a thin plate is a fin, referenced to
  its chord and planform area. get_active_geometry says which, and for a fin
  which edge faces the air is the operator's to confirm. set_geometry_and_mesh
  takes body_kind and pitch_axis (the CAD axis the model tilts about for an
  angle of attack -- a fin's span); the interface draws both on the model.
- Every point of a sweep keeps its full solution. To draw one, pass the
  point's sim_id as the sweep lists it ("<sweep_id>-003") to
  generate_cfd_visualization; never re-run points just to draw them.
- Convergence is judged by how far the residual has fallen from its peak,
  never by its absolute level.
- Below Mach 0.3 runs are solved incompressible (the result's notes say so);
  compressible results there overstated drag several times, so do not
  compare them with older low-Mach numbers. Do not name a convective scheme
  for a low-Mach run unless asked: naming one forces the compressible solver.
- Every rendered image carries its Mach number, speed, angle of attack and
  sideslip in its caption.
- The nose tip is at the origin of every new mesh. Quote the centre of
  pressure from 'center_of_pressure_behind_nose_m' and its fraction of the
  length; a stable rocket has it well behind the centre of gravity. If a
  reply says the mesh predates this, say so and offer to remesh rather than
  guessing where the origin was.
- A centre of pressure of NaN at zero angle of attack is correct, not an
  error: without a transverse force there is no defined centre of pressure.
  Suggest 2-5 degrees instead of inventing a value.
- If a tool returns ok: false, read the error and act on it. Do not repeat the
  same call unchanged.
- When the operator refers to a file they imported, call get_active_geometry
  rather than asking for the path: the interface records whatever was opened
  in it, and set_geometry_and_mesh will use that file when given no path. Ask
  for a path only when that tool reports nothing loaded.
- CAD units and the direction the body points both come from the file itself
  and are reported back to you. Say which unit was used, how large the model
  turned out to be, and which end the nose is on. A millimetre model read as
  metres and a rocket meshed tail-first both return a full set of confident,
  plausible, entirely wrong numbers, and nothing downstream objects.
- When get_active_geometry returns 'ask_the_operator' entries, or meshing
  fails asking which end the nose is on, put that question to the user in
  their own language and wait. Do not pick a default: the CAD tells you which
  axis the body is longest along, and only the operator knows which end of it
  is the tip.
- The operator sees every rocket in ROCKET AXES: nose along +Z, as it
  stands on the pad and flies straight up. Quote forces from
  'forces_rocket_frame_n': drag on a rocket climbing nose-first is a
  negative F_z, lift from a positive angle of attack is +F_x, sideslip gives
  F_y. 'forces_solver_frame_n' is the solver's own frame, where the body lies
  along X; do not present it as the rocket's axes. Fin hinge points and
  directions you pass are in rocket axes too, so a fin near the tail of a
  1.3 m rocket has z close to -1.2.
- Rendered images appear in the program's Graphics tab and inline in this
  conversation, where the operator can open and export them. Tell them that
  rather than reading out a file path. To show a shock wave, render
  'schlieren' with camera_view 'side': on a slender rocket at low supersonic
  speed the shocks are weak and barely change the Mach number, but the
  density gradient shows them plainly.
- You cannot run shell commands or read arbitrary files. If a request needs
  something outside your tools, say so plainly.

Answer in the language the operator writes in.
"""


class AIAgentError(RuntimeError):
    """Raised when the assistant cannot complete a request."""


class AuthenticationError(AIAgentError):
    """Raised when the API key is missing, malformed or rejected."""


# ---------------------------------------------------------------------------
# Conversation data
# ---------------------------------------------------------------------------


@dataclass
class ToolInvocation:
    """One tool call the assistant made, with its outcome."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    declined: bool = False
    duration_s: float = 0.0

    @property
    def succeeded(self) -> bool:
        """True when the tool ran and reported success."""
        return bool(self.result and self.result.get("ok"))

    def summary(self) -> str:
        """One line describing the call, for the transcript."""
        rendered = ", ".join(
            f"{key}={_abbreviate(value)}" for key, value in self.arguments.items()
        )
        if self.declined:
            return f"{self.name}({rendered}) — declined"
        if self.error:
            return f"{self.name}({rendered}) — failed: {self.error}"
        if self.result and not self.result.get("ok"):
            return f"{self.name}({rendered}) — {self.result.get('error', 'failed')}"
        return f"{self.name}({rendered}) — ok ({self.duration_s:.1f}s)"


def _abbreviate(value: Any, limit: int = 40) -> str:
    """Render an argument compactly for display."""
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class UsageMetrics:
    """What one request has cost so far, in tokens, seconds and money.

    Reported after every round rather than only at the end, because the
    operator watching a long request wants to know whether it is worth
    letting run. Cost comes from OpenRouter's own accounting where the API
    supplies it; when it does not, the field stays None rather than being
    invented from a price list that may be out of date.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    generation_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    rounds: int = 0

    @property
    def total_tokens(self) -> int:
        """Prompt and completion tokens together."""
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_per_second(self) -> float:
        """Completion tokens per second of generation.

        Measured against time spent waiting on the model, not against the
        whole request: a request that spends four minutes meshing has not
        slowed the model down.
        """
        if self.generation_seconds <= 0.0:
            return 0.0
        return self.completion_tokens / self.generation_seconds

    def summary(self) -> str:
        """One line for a status label."""
        parts = [
            f"{self.total_tokens:,} tokens",
            f"{self.tokens_per_second:.1f} tok/s",
        ]
        if self.cost_usd is not None:
            parts.append(f"${self.cost_usd:.4f}")
        parts.append(f"{self.elapsed_seconds:.0f}s")
        return " · ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "generation_seconds": self.generation_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "tokens_per_second": self.tokens_per_second,
            "rounds": self.rounds,
        }


# What a progress event is telling the interface about.
EVENT_ROUND = "round"
EVENT_TEXT = "text"
EVENT_TOOL_STARTED = "tool_started"
EVENT_TOOL_FINISHED = "tool_finished"
EVENT_RETRY = "retry"
EVENT_LIMIT = "limit"


@dataclass
class ProgressEvent:
    """One thing the assistant did, as it happens.

    The plain progress string was enough for a one-line status label, but a
    request that spends a quarter of an hour calling tools needs a running
    account the operator can read afterwards: which round, what the model
    said, which tool with which arguments, how long it took. Carrying the
    parts separately lets the interface style them instead of parsing a
    sentence back apart.
    """

    kind: str
    message: str
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    round: int = 0
    failed: bool = False
    # A finished tool's reply, for an interface that wants to show more than
    # a summary line -- a rendered image, for one.
    result: Any = None

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.message


@dataclass
class AgentReply:
    """The outcome of one request to the assistant."""

    text: str
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    rounds: int = 0
    stopped_early: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    metrics: UsageMetrics = field(default_factory=UsageMetrics)

    @property
    def used_tools(self) -> bool:
        """True when at least one tool was invoked."""
        return bool(self.tool_calls)


# ---------------------------------------------------------------------------
# Chat transport
# ---------------------------------------------------------------------------


class ChatClient(ABC):
    """Sends chat completion requests to a model."""

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Return one assistant message, in OpenAI chat-completion shape."""

    @abstractmethod
    def list_models(self) -> list[dict[str, Any]]:
        """Return the models available to this account."""


class OpenRouterClient(ChatClient):
    """Talks to OpenRouter's OpenAI-compatible API.

    Parameters
    ----------
    api_key:
        The operator's OpenRouter key.
    base_url:
        Override for testing against a local stub.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = OPENROUTER_BASE_URL,
        max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        on_retry: ProgressCallback | None = None,
    ) -> None:
        if not (api_key or "").strip():
            raise AuthenticationError(
                "No OpenRouter API key is set. Open Settings > AI assistant and "
                "paste a key from https://openrouter.ai/keys"
            )
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max(1, int(max_attempts))
        self.on_retry = on_retry

    def _wait_before_retry(
        self, attempt: int, reason: str, response: Any
    ) -> None:
        """Pause before another try, and say so.

        Silence here is what a hang looks like, so the wait is announced
        before it is taken rather than after.
        """
        delay = _RETRY_BACKOFF_S[min(attempt - 1, len(_RETRY_BACKOFF_S) - 1)]
        if response is not None:
            # A server that says how long to wait knows better than we do,
            # within reason.
            header = getattr(response, "headers", None) or {}
            try:
                requested = float(header.get("Retry-After", ""))
            except (TypeError, ValueError):
                requested = 0.0
            if requested > 0.0:
                delay = min(requested, _MAX_RETRY_AFTER_S)
        if self.on_retry is not None:
            self.on_retry(
                f"OpenRouter did not answer ({reason}); "
                f"retrying in {delay:.0f} s"
            )
        time.sleep(delay)

    def _headers(self) -> dict[str, str]:
        """Authorisation and attribution headers."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": REFERER,
            "X-Title": APPLICATION_TITLE,
        }

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Request one completion, returning the assistant message."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            # Ask OpenRouter to account for the request. Without this the
            # reply carries token counts but no price, and the interface
            # would have to guess at a rate card that changes weekly.
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        data = self._post("/chat/completions", payload)
        elapsed = time.perf_counter() - started

        choices = data.get("choices") or []
        if not choices:
            raise AIAgentError(
                "the model returned no response; it may be overloaded or the "
                "model id may be wrong"
            )
        message = choices[0].get("message") or {}
        usage = dict(data.get("usage") or {})
        # Time spent waiting on the model, which is what tokens per second
        # is measured against.
        usage["generation_seconds"] = elapsed
        message["_usage"] = usage
        return message

    def list_models(self) -> list[dict[str, Any]]:
        """Fetch the live model catalogue.

        The catalogue changes constantly, so the interface offers what the API
        actually reports rather than a list hard-coded here that would go
        stale.
        """
        data = self._get("/models")
        models = data.get("data") or []
        return [
            {
                "id": entry.get("id", ""),
                "name": entry.get("name", entry.get("id", "")),
                "context_length": entry.get("context_length"),
                "pricing": entry.get("pricing", {}),
            }
            for entry in models
            if entry.get("id")
        ]

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON and return the decoded response."""
        return self._request("POST", path, payload)

    def _get(self, path: str) -> dict[str, Any]:
        """GET and return the decoded response."""
        return self._request("GET", path, None)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Perform an HTTP request, retrying the failures worth retrying.

        A dropped connection used to end the request, and with it whatever
        tool work the conversation had already paid for -- a quarter of an
        hour and thirty-five thousand tokens, in the case that prompted
        this. Network faults and the server's own transient refusals are
        therefore retried a few times with a widening gap.

        The honest caveat: a completion is not idempotent for billing. If
        the connection drops *after* the model has generated, retrying pays
        for that generation twice. Losing the whole session costs more, so
        retrying is the default -- but every retry is announced, and
        ``max_attempts=1`` turns it off for an operator who would rather
        not gamble.
        """
        try:
            import requests
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise AIAgentError(
                "the 'requests' package is required for the AI assistant; "
                "run Setup.bat to install it"
            ) from error

        url = f"{self.base_url}{path}"
        attempts = max(1, self.max_attempts)
        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=payload,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except Exception as error:
                if last:
                    raise AIAgentError(
                        f"could not reach OpenRouter: {error}. Check your "
                        "internet connection and any proxy or firewall."
                    ) from error
                self._wait_before_retry(
                    attempt, f"{type(error).__name__}: {error}", None
                )
                continue

            if response.status_code in _RETRY_STATUSES and not last:
                self._wait_before_retry(
                    attempt, f"HTTP {response.status_code}", response
                )
                continue
            break

        if response.status_code in (401, 403):
            raise AuthenticationError(
                "OpenRouter rejected the API key. Check it at "
                "https://openrouter.ai/keys and re-enter it in "
                "Settings > AI assistant."
            )
        if response.status_code == 402:
            raise AIAgentError(
                "OpenRouter reports insufficient credit for this model. Add "
                "credit, or choose a cheaper model."
            )
        if response.status_code == 429:
            raise AIAgentError(
                "OpenRouter is rate-limiting this key. Wait a moment and try "
                "again."
            )
        if response.status_code >= 400:
            raise AIAgentError(
                f"OpenRouter returned HTTP {response.status_code}: "
                f"{_error_detail(response)}"
            )

        try:
            return response.json()
        except ValueError as error:
            raise AIAgentError(
                f"OpenRouter returned a response that is not JSON: {error}"
            ) from error


def _error_detail(response: Any) -> str:
    """Extract a readable message from an error response."""
    try:
        payload = response.json()
    except Exception:
        return (getattr(response, "text", "") or "")[:200]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message", error))[:200]
    return str(error or payload)[:200]


class FakeChatClient(ChatClient):
    """Replays scripted assistant messages instead of calling a model.

    Each entry in ``responses`` is returned in turn, so a multi-step
    tool-calling conversation can be scripted end to end.
    """

    def __init__(
        self,
        responses: Sequence[dict[str, Any]],
        models: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.models = list(models or [{"id": DEFAULT_MODEL, "name": "Fake model"}])
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Return the next scripted message."""
        self.calls.append(
            {"messages": list(messages), "tools": tools, "model": model}
        )
        if not self.responses:
            return {"role": "assistant", "content": "(no further scripted reply)"}
        return dict(self.responses.pop(0))

    def list_models(self) -> list[dict[str, Any]]:
        """Return the scripted catalogue."""
        return list(self.models)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


def build_tool_schemas() -> list[dict[str, Any]]:
    """Convert the MCP server's tools into OpenAI function definitions.

    Reading from the live registration rather than a second hand-written list
    is what keeps the assistant in step with the GUI and MCP surfaces.
    """
    import asyncio

    import mcp_server

    try:
        tools = asyncio.run(mcp_server.server.list_tools())
    except RuntimeError:
        # Already inside an event loop (a GUI worker thread can be): run the
        # coroutine on a private loop instead of failing.
        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(mcp_server.server.list_tools())
        finally:
            loop.close()

    schemas: list[dict[str, Any]] = []
    for tool in tools:
        if tool.name not in mcp_server.TOOL_FUNCTIONS:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": (tool.description or "").strip(),
                    "parameters": tool.input_schema,
                },
            }
        )
    return schemas


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

# Called with a tool name and arguments; returns True to allow the call.
ApprovalCallback = Callable[[str, dict[str, Any]], bool]

# Called with human-readable progress lines.
ProgressCallback = Callable[[str], None]
MetricsCallback = Callable[["UsageMetrics"], None]
# Called with the same news in structured form. Both channels are served, so
# anything that only wants a line of text keeps working unchanged.
EventCallback = Callable[[ProgressEvent], None]


class AIAssistant:
    """Runs a conversation, calling platform tools on the operator's behalf.

    Parameters
    ----------
    client:
        Chat transport. Inject :class:`FakeChatClient` to test.
    model:
        Model id to request.
    approve:
        Called before each long-running tool. Returning False declines the
        call and tells the model so, which lets the operator stop a
        half-hour sweep they did not intend to start.
    on_progress:
        Receives status lines as the conversation proceeds.
    on_metrics:
        Receives the running token count, rate and cost after every round,
        so a long request can be watched rather than waited out.
    on_event:
        Receives the same news as ``on_progress`` in structured form, for an
        interface that wants to lay it out rather than print it.
    max_rounds:
        Cap on tool-calling rounds for one request.
    """

    def __init__(
        self,
        client: ChatClient,
        model: str = DEFAULT_MODEL,
        approve: ApprovalCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_metrics: MetricsCallback | None = None,
        on_event: EventCallback | None = None,
        max_rounds: int = MAX_TOOL_ROUNDS,
        system_prompt: str = SYSTEM_PROMPT,
        confirm_orientation: bool = False,
    ) -> None:
        self.client = client
        # When set, a setup must be previewed and answered by the operator
        # before it is meshed or solved (see mcp_server's orientation gate).
        self.confirm_orientation = confirm_orientation
        self.model = model
        self.approve = approve
        self.on_progress = on_progress
        self.on_metrics = on_metrics
        self.on_event = on_event
        self.max_rounds = max(1, max_rounds)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        """Ask the assistant to stop after the round in flight.

        This also cancels any solve currently running, which makes the tool
        call return an error and unwinds the round cleanly. Without it the
        only way out of a wedged solve was closing the program.
        """
        self._stop_requested.set()
        cancel_all_runs()

    def clear_stop(self) -> None:
        """Forget an earlier stop request, before a new conversation turn."""
        self._stop_requested.clear()

    @property
    def stop_requested(self) -> bool:
        """True once someone has asked the assistant to stop."""
        return self._stop_requested.is_set()

    # -- conversation ------------------------------------------------------

    def reset(self) -> None:
        """Start a fresh conversation, keeping the system prompt."""
        self.messages = self.messages[:1]
        import mcp_server

        mcp_server.reset_orientation_confirmations()

    def history(self) -> list[dict[str, Any]]:
        """The conversation so far, excluding the system prompt."""
        return self.messages[1:]

    def _report(self, message: str) -> None:
        """Emit a plain progress line, if anyone is listening."""
        if self.on_progress is not None:
            self.on_progress(message)

    def report_retry(self, message: str) -> None:
        """Announce a transport retry. Called by the HTTP client."""
        self._emit(ProgressEvent(kind=EVENT_RETRY, message=message))

    def _stopped_reply(
        self,
        invocations: list[ToolInvocation],
        rounds: int,
        usage: dict[str, Any],
        metrics: UsageMetrics,
        started: float,
    ) -> AgentReply:
        """The reply given when the operator pressed Stop."""
        metrics.elapsed_seconds = time.perf_counter() - started
        self._emit(
            ProgressEvent(kind=EVENT_LIMIT, message="Stopped at your request.")
        )
        text = (
            "I stopped at your request. Any solver that was running has been "
            "cancelled; whatever finished before then is still in the project."
        )
        self.messages.append({"role": "assistant", "content": text})
        return AgentReply(
            text=text,
            tool_calls=invocations,
            rounds=rounds,
            stopped_early=True,
            usage=usage,
            metrics=metrics,
        )

    def _emit(self, event: ProgressEvent) -> None:
        """Announce one step on both channels.

        The plain-text channel is kept because it is what a status bar and
        the existing callers want; the structured one carries the same news
        with its parts still separate.
        """
        self._report(event.message)
        if self.on_event is not None:
            self.on_event(event)

    def _report_metrics(self, metrics: UsageMetrics) -> None:
        """Emit a snapshot of the running cost and rate.

        A copy, not the live object: the loop keeps mutating its own and a
        listener that stores what it was handed -- a log, a chart, a widget
        on another thread -- would otherwise find every past reading had
        silently become the latest one.
        """
        if self.on_metrics is not None:
            self.on_metrics(replace(metrics))

    def ask(self, prompt: str) -> AgentReply:
        """Send one operator request and run it to completion.

        The model may call tools repeatedly; this loops until it produces a
        final answer, the round cap is reached, or a tool call is declined.

        Parameters
        ----------
        prompt:
            What the operator typed.

        Returns
        -------
        AgentReply
            The final text plus every tool call made along the way.
        """
        if not (prompt or "").strip():
            raise AIAgentError("the request is empty")

        # A stop applies to the request it interrupted, not to the next one.
        self._stop_requested.clear()

        import mcp_server

        self.messages.append({"role": "user", "content": prompt})
        tools = build_tool_schemas()
        mcp_server.require_orientation_confirmation(self.confirm_orientation)
        # This message is the operator's answer to any picture shown last
        # turn; what they were shown is now theirs to have approved.
        mcp_server.acknowledge_orientation_previews()

        invocations: list[ToolInvocation] = []
        usage: dict[str, Any] = {}
        metrics = UsageMetrics()
        started = time.perf_counter()
        stopped_early = False
        rounds = 0

        for rounds in range(1, self.max_rounds + 1):
            if self._stop_requested.is_set():
                return self._stopped_reply(invocations, rounds, usage, metrics, started)
            self._emit(
                ProgressEvent(
                    kind=EVENT_ROUND,
                    message=f"Thinking (round {rounds}) …",
                    round=rounds,
                )
            )
            message = self.client.complete(
                self.messages, tools=tools, model=self.model
            )
            usage = _merge_usage(usage, message.pop("_usage", {}))
            _apply_usage(metrics, usage, rounds, time.perf_counter() - started)
            self._report_metrics(metrics)

            tool_calls = message.get("tool_calls") or []
            # Keep the assistant turn verbatim: the API requires each
            # tool result to follow the message that requested it.
            self.messages.append(_assistant_message(message))

            # A model that is about to call tools usually says why first.
            # That sentence is the best account of what is happening that
            # anyone gets, and it used to be stored and never shown.
            commentary = (message.get("content") or "").strip()
            if commentary and tool_calls:
                self._emit(
                    ProgressEvent(
                        kind=EVENT_TEXT, message=commentary, round=rounds
                    )
                )

            if not tool_calls:
                metrics.elapsed_seconds = time.perf_counter() - started
                return AgentReply(
                    text=(message.get("content") or "").strip(),
                    tool_calls=invocations,
                    rounds=rounds,
                    stopped_early=stopped_early,
                    usage=usage,
                    metrics=metrics,
                )

            for call in tool_calls:
                invocation = self._run_tool_call(call, mcp_server)
                invocations.append(invocation)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": invocation.name,
                        "content": _tool_result_text(invocation),
                    }
                )
                if invocation.declined:
                    stopped_early = True
                if self._stop_requested.is_set():
                    break
            metrics.elapsed_seconds = time.perf_counter() - started
            self._report_metrics(metrics)
            if self._stop_requested.is_set():
                return self._stopped_reply(invocations, rounds, usage, metrics, started)
            if self.confirm_orientation and any(
                item.name == PREVIEW_TOOL and item.succeeded for item in tool_calls_run(invocations, len(tool_calls))
            ):
                return self._await_operator(invocations, rounds, usage, metrics, started)

        self._emit(
            ProgressEvent(kind=EVENT_LIMIT, message="Reached the tool-call limit.")
        )
        return AgentReply(
            text=(
                "I stopped after "
                f"{self.max_rounds} rounds of tool calls without reaching a "
                "final answer. Try narrowing the request into smaller steps."
            ),
            tool_calls=invocations,
            rounds=rounds,
            stopped_early=True,
            usage=usage,
            metrics=metrics,
        )

    def _await_operator(
        self,
        invocations: list[ToolInvocation],
        rounds: int,
        usage: dict[str, Any],
        metrics: UsageMetrics,
        started: float,
    ) -> AgentReply:
        """End the turn after a preview, so the operator can answer it.

        The model gets one more call to describe the picture and ask. Any
        tool it tries to call in the same breath is answered with "not run",
        because nothing may be meshed or solved until the operator replies.
        """
        message = self.client.complete(self.messages, tools=build_tool_schemas(), model=self.model)
        usage = _merge_usage(usage, message.pop("_usage", {}))
        _apply_usage(metrics, usage, rounds + 1, time.perf_counter() - started)
        self.messages.append(_assistant_message(message))
        for call in message.get("tool_calls") or []:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": (call.get("function") or {}).get("name", ""),
                    "content": json.dumps(
                        {"ok": False, "error": "not run: waiting for the operator to answer the preview"}
                    ),
                }
            )
        text = (message.get("content") or "").strip() or AWAITING_CONFIRMATION_TEXT
        if message.get("tool_calls"):
            # The transcript must end on an assistant turn the operator can
            # answer, not on tool results.
            self.messages.append({"role": "assistant", "content": text})
        metrics.elapsed_seconds = time.perf_counter() - started
        self._report_metrics(metrics)
        return AgentReply(
            text=text,
            tool_calls=invocations,
            rounds=rounds + 1,
            stopped_early=False,
            usage=usage,
            metrics=metrics,
        )

    def _run_tool_call(self, call: dict[str, Any], mcp_server: Any) -> ToolInvocation:
        """Execute one tool call, honouring the approval callback."""
        function = call.get("function") or {}
        name = function.get("name", "")
        raw_arguments = function.get("arguments") or "{}"

        try:
            arguments = (
                raw_arguments
                if isinstance(raw_arguments, dict)
                else json.loads(raw_arguments or "{}")
            )
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as error:
            invocation = ToolInvocation(
                name=name,
                arguments={},
                error=f"the model sent malformed arguments: {error}",
            )
            self._emit(
                ProgressEvent(
                    kind=EVENT_TOOL_FINISHED,
                    message=invocation.summary(),
                    tool=name,
                    failed=True,
                )
            )
            return invocation

        invocation = ToolInvocation(name=name, arguments=arguments)

        if (
            self.approve is not None
            and mcp_server.is_long_running(name, arguments)
            and not self.approve(name, arguments)
        ):
            invocation.declined = True
            invocation.error = "the operator declined this call"
            self._emit(
                ProgressEvent(
                    kind=EVENT_TOOL_FINISHED,
                    message=f"Declined: {name}",
                    tool=name,
                    arguments=arguments,
                    failed=True,
                )
            )
            return invocation

        # Announce the arguments *before* the call. A four-minute mesh
        # should not keep the operator guessing which file and which
        # settings it is chewing on.
        self._emit(
            ProgressEvent(
                kind=EVENT_TOOL_STARTED,
                message=f"Running {name} …",
                tool=name,
                arguments=arguments,
            )
        )
        started = time.perf_counter()
        invocation.result = mcp_server.call_tool(name, arguments)
        invocation.duration_s = time.perf_counter() - started
        self._emit(
            ProgressEvent(
                kind=EVENT_TOOL_FINISHED,
                message=invocation.summary(),
                tool=name,
                arguments=arguments,
                duration_s=invocation.duration_s,
                failed=not invocation.succeeded,
                result=invocation.result,
            )
        )
        return invocation


def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """Normalise an assistant message for the next request.

    Only the fields the API expects are kept, and content is coerced to a
    string because some models return null alongside tool calls.
    """
    normalised: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") or "",
    }
    if message.get("tool_calls"):
        normalised["tool_calls"] = message["tool_calls"]
    return normalised


def _tool_result_text(invocation: ToolInvocation) -> str:
    """Render a tool outcome as the message content the model reads."""
    if invocation.declined:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "The operator declined to run this step. Ask what they "
                    "would prefer instead; do not retry it unchanged."
                ),
            }
        )
    if invocation.error:
        return json.dumps({"ok": False, "error": invocation.error})
    return json.dumps(invocation.result, default=str)


def _merge_usage(total: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    """Accumulate token usage across rounds."""
    merged = dict(total)
    for key, value in (addition or {}).items():
        if isinstance(value, (int, float)):
            merged[key] = merged.get(key, 0) + value
    return merged


def _apply_usage(
    metrics: UsageMetrics,
    usage: dict[str, Any],
    rounds: int,
    elapsed: float,
) -> None:
    """Fold accumulated API usage into the live metrics.

    Providers vary in what they report and some report nothing at all, so
    every field is read defensively and a missing cost stays missing rather
    than becoming a zero the operator might trust.
    """
    metrics.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    metrics.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    metrics.generation_seconds = float(usage.get("generation_seconds", 0.0) or 0.0)
    metrics.rounds = rounds
    metrics.elapsed_seconds = elapsed
    cost = usage.get("cost")
    metrics.cost_usd = float(cost) if isinstance(cost, (int, float)) else None


def create_assistant(
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    approve: ApprovalCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_metrics: MetricsCallback | None = None,
    on_event: EventCallback | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> AIAssistant:
    """Build an assistant using the stored API key.

    Raises
    ------
    AuthenticationError
        If no key is available, with instructions for setting one.
    """
    from core.credentials import load_api_key

    key = api_key or load_api_key("openrouter")
    if not key:
        raise AuthenticationError(
            "No OpenRouter API key is set. Get one from "
            "https://openrouter.ai/keys and paste it into "
            "Settings > AI assistant, or set the OPENROUTER_API_KEY "
            "environment variable."
        )
    assistant = AIAssistant(
        OpenRouterClient(key, max_attempts=retry_attempts),
        model=model,
        approve=approve,
        on_progress=on_progress,
        on_metrics=on_metrics,
        on_event=on_event,
        confirm_orientation=True,
    )
    # The transport cannot reach the assistant's emitter on its own, so a
    # retry is handed to it here and lands in the transcript like everything
    # else the request does.
    assistant.client.on_retry = assistant.report_retry
    return assistant
