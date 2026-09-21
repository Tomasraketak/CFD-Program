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
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

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
    "openai/gpt-5.6-luna",
    "deepseek/deepseek-chat",
)

# How many tool-calling rounds one request may take before the loop stops.
# A confused model can otherwise call tools indefinitely at the user's expense.
MAX_TOOL_ROUNDS = 12

# Network timeouts in seconds. Generous, because a long prompt to a slow model
# legitimately takes a while.
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 180.0

SYSTEM_PROMPT = """\
You are the assistant built into AeroThermalStudio, a CFD and thermal
simulation program. You help the operator run rocket aerodynamics and sensor
microclimate studies by calling the tools provided.

Working rules:

- Units are SI: metres, m/s, newtons, pascals, kelvin. Angles are in degrees
  and some temperatures in Celsius, as each parameter's description states.
- Meshing is expensive and does not depend on the flight condition, because
  angle of attack and Mach are applied by the solver. Reuse one mesh_id across
  simulations and sweeps; only remesh when the geometry, domain or mesh
  settings change.
- Start at 'coarse' resolution to validate a setup, then refine.
- Runtimes vary enormously: the analytical sensor model returns in
  milliseconds, a mesh takes minutes, and a sweep takes its point count times
  3-8 minutes. Tell the operator what a step will cost before starting it.
- Report what the tools actually returned. If 'converged' is false or
  'within_target_band' is false, say so rather than presenting the number as
  settled fact.
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

    def __init__(self, api_key: str, base_url: str = OPENROUTER_BASE_URL) -> None:
        if not (api_key or "").strip():
            raise AuthenticationError(
                "No OpenRouter API key is set. Open Settings > AI assistant and "
                "paste a key from https://openrouter.ai/keys"
            )
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")

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
        """Perform an HTTP request, turning failures into readable errors."""
        try:
            import requests
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise AIAgentError(
                "the 'requests' package is required for the AI assistant; "
                "run Setup.bat to install it"
            ) from error

        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers(),
                json=payload,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except Exception as error:
            raise AIAgentError(
                f"could not reach OpenRouter: {error}. Check your internet "
                "connection and any proxy or firewall."
            ) from error

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
        max_rounds: int = MAX_TOOL_ROUNDS,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.client = client
        self.model = model
        self.approve = approve
        self.on_progress = on_progress
        self.on_metrics = on_metrics
        self.max_rounds = max(1, max_rounds)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    # -- conversation ------------------------------------------------------

    def reset(self) -> None:
        """Start a fresh conversation, keeping the system prompt."""
        self.messages = self.messages[:1]

    def history(self) -> list[dict[str, Any]]:
        """The conversation so far, excluding the system prompt."""
        return self.messages[1:]

    def _report(self, message: str) -> None:
        """Emit a progress line, if anyone is listening."""
        if self.on_progress is not None:
            self.on_progress(message)

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

        import mcp_server

        self.messages.append({"role": "user", "content": prompt})
        tools = build_tool_schemas()

        invocations: list[ToolInvocation] = []
        usage: dict[str, Any] = {}
        metrics = UsageMetrics()
        started = time.perf_counter()
        stopped_early = False
        rounds = 0

        for rounds in range(1, self.max_rounds + 1):
            self._report(f"Thinking (round {rounds}) …")
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
            metrics.elapsed_seconds = time.perf_counter() - started
            self._report_metrics(metrics)

        self._report("Reached the tool-call limit.")
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
            return ToolInvocation(
                name=name,
                arguments={},
                error=f"the model sent malformed arguments: {error}",
            )

        invocation = ToolInvocation(name=name, arguments=arguments)

        if (
            self.approve is not None
            and name in mcp_server.LONG_RUNNING_TOOLS
            and not self.approve(name, arguments)
        ):
            invocation.declined = True
            invocation.error = "the operator declined this call"
            self._report(f"Declined: {name}")
            return invocation

        self._report(f"Running {name} …")
        started = time.perf_counter()
        invocation.result = mcp_server.call_tool(name, arguments)
        invocation.duration_s = time.perf_counter() - started
        self._report(invocation.summary())
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
    return AIAssistant(
        OpenRouterClient(key),
        model=model,
        approve=approve,
        on_progress=on_progress,
        on_metrics=on_metrics,
    )
