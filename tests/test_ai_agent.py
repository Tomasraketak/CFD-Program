"""The in-application AI assistant.

The whole agent loop is exercised without a network or an API key by injecting
:class:`FakeChatClient`, the same dependency-injection arrangement the SU2
solver uses. What is checked here is the behaviour that matters when a model
misbehaves: malformed arguments, unknown tools, runaway tool calling, and the
operator declining an expensive step.
"""

from __future__ import annotations

import json

import pytest

import mcp_server
from backend.ai_agent import (
    AIAgentError,
    AIAssistant,
    AuthenticationError,
    DEFAULT_MODEL,
    FakeChatClient,
    OpenRouterClient,
    ToolInvocation,
    build_tool_schemas,
    create_assistant,
)


def text_reply(content: str) -> dict[str, object]:
    """A scripted assistant message with no tool calls."""
    return {"role": "assistant", "content": content}


def tool_reply(name: str, arguments: dict[str, object] | str, call_id: str = "c1"):
    """A scripted assistant message requesting one tool call."""
    rendered = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": rendered},
            }
        ],
    }


# -- tool schemas -----------------------------------------------------------


def test_schemas_come_from_the_mcp_server():
    """One registration drives the GUI, MCP and the assistant alike."""
    schemas = build_tool_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert names == set(mcp_server.TOOL_FUNCTIONS)


def test_every_schema_is_a_usable_function_definition():
    for schema in build_tool_schemas():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["description"].strip()
        assert function["parameters"]["type"] == "object"


def test_long_running_tools_are_all_real_tools():
    """A stale name here would silently disable a confirmation prompt."""
    assert mcp_server.LONG_RUNNING_TOOLS <= set(mcp_server.TOOL_FUNCTIONS)


# -- the conversation loop --------------------------------------------------


def test_a_plain_answer_needs_no_tools():
    client = FakeChatClient([text_reply("Mach 0.8 is the transonic threshold.")])
    reply = AIAssistant(client).ask("What is the transonic threshold?")

    assert reply.text.startswith("Mach 0.8")
    assert reply.used_tools is False
    assert reply.rounds == 1


def test_a_tool_call_runs_and_its_result_reaches_the_model():
    client = FakeChatClient(
        [
            tool_reply(
                "run_sensor_thermal_simulation",
                {"analytic_only": True, "solar_flux_w_m2": 900.0},
            ),
            text_reply("The sensor reads about 2 K above ambient."),
        ]
    )
    reply = AIAssistant(client).ask("How far off will the BMP580 read at 900 W/m²?")

    assert reply.rounds == 2
    assert len(reply.tool_calls) == 1
    invocation = reply.tool_calls[0]
    assert invocation.succeeded
    assert invocation.result["ok"] is True

    # The second request must carry the tool result back to the model.
    second = client.calls[1]["messages"]
    tool_messages = [m for m in second if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["ok"] is True


def test_the_system_prompt_leads_every_conversation():
    client = FakeChatClient([text_reply("ok")])
    assistant = AIAssistant(client)
    assistant.ask("hello")
    assert client.calls[0]["messages"][0]["role"] == "system"


def test_tools_are_offered_on_every_request():
    client = FakeChatClient([text_reply("ok")])
    AIAssistant(client).ask("hello")
    assert client.calls[0]["tools"]


def test_the_chosen_model_is_requested():
    client = FakeChatClient([text_reply("ok")])
    AIAssistant(client, model="google/gemini-2.0-flash-001").ask("hi")
    assert client.calls[0]["model"] == "google/gemini-2.0-flash-001"


def test_token_usage_accumulates_across_rounds():
    first = tool_reply("check_environment", {})
    first["_usage"] = {"total_tokens": 100}
    second = text_reply("SU2 was not found.")
    second["_usage"] = {"total_tokens": 40}

    reply = AIAssistant(FakeChatClient([first, second])).ask("Is SU2 installed?")
    assert reply.usage["total_tokens"] == 140


def test_history_excludes_the_system_prompt_and_reset_keeps_it():
    client = FakeChatClient([text_reply("one"), text_reply("two")])
    assistant = AIAssistant(client)
    assistant.ask("first")
    assert len(assistant.history()) == 2  # the question and the answer

    assistant.reset()
    assert assistant.history() == []
    assert assistant.messages[0]["role"] == "system"


def test_an_empty_request_is_refused():
    with pytest.raises(AIAgentError):
        AIAssistant(FakeChatClient([])).ask("   ")


# -- misbehaving models -----------------------------------------------------


def test_malformed_tool_arguments_are_reported_not_raised():
    """A model that emits broken JSON must get an error it can recover from."""
    client = FakeChatClient(
        [
            tool_reply("check_environment", "{not json"),
            text_reply("Sorry, let me try that again."),
        ]
    )
    reply = AIAssistant(client).ask("check the environment")

    invocation = reply.tool_calls[0]
    assert invocation.error is not None
    assert invocation.succeeded is False
    assert json.loads(client.calls[1]["messages"][-1]["content"])["ok"] is False


def test_an_unknown_tool_returns_a_structured_error():
    client = FakeChatClient(
        [tool_reply("delete_everything", {}), text_reply("That is not available.")]
    )
    reply = AIAssistant(client).ask("delete everything")

    result = reply.tool_calls[0].result
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


def test_wrong_arguments_do_not_crash_the_loop():
    client = FakeChatClient(
        [
            tool_reply("check_environment", {"nonexistent": 1}),
            text_reply("Corrected."),
        ]
    )
    reply = AIAssistant(client).ask("check")
    assert reply.tool_calls[0].result["ok"] is False


def test_runaway_tool_calling_stops_at_the_round_cap():
    """A model that never finishes must not spend the operator's credit."""
    client = FakeChatClient([tool_reply("check_environment", {})] * 20)
    reply = AIAssistant(client, max_rounds=3).ask("loop forever")

    assert reply.stopped_early is True
    assert reply.rounds == 3
    assert len(reply.tool_calls) == 3
    assert "3 rounds" in reply.text


def test_max_rounds_is_at_least_one():
    assert AIAssistant(FakeChatClient([]), max_rounds=0).max_rounds == 1


# -- operator control -------------------------------------------------------


def test_declining_a_long_tool_stops_it_and_tells_the_model():
    asked: list[str] = []

    def refuse(name, arguments):
        asked.append(name)
        return False

    client = FakeChatClient(
        [
            tool_reply("run_parametric_sweep", {"parameter": "mach", "values": [1, 2]}),
            text_reply("Understood — what would you prefer?"),
        ]
    )
    reply = AIAssistant(client, approve=refuse).ask("sweep Mach 1 to 2")

    assert asked == ["run_parametric_sweep"]
    invocation = reply.tool_calls[0]
    assert invocation.declined is True
    assert invocation.result is None
    assert reply.stopped_early is True

    content = json.loads(client.calls[1]["messages"][-1]["content"])
    assert content["ok"] is False
    assert "declined" in content["error"]


def test_cheap_tools_are_not_gated_by_approval():
    """Confirming an instant analytical answer would only be an annoyance."""
    asked: list[str] = []

    client = FakeChatClient(
        [
            tool_reply("run_sensor_thermal_simulation", {"analytic_only": True}),
            text_reply("done"),
        ]
    )
    approve = lambda name, arguments: (asked.append(name), True)[1]  # noqa: E731
    reply = AIAssistant(client, approve=approve).ask("estimate the bias")

    assert asked == []
    assert reply.tool_calls[0].succeeded


def test_approval_granted_lets_the_tool_run():
    client = FakeChatClient(
        [tool_reply("check_environment", {}), text_reply("done")]
    )
    reply = AIAssistant(
        client, approve=lambda name, arguments: True
    ).ask("check the environment")
    assert reply.tool_calls[0].succeeded


def test_progress_lines_describe_each_step():
    lines: list[str] = []
    client = FakeChatClient(
        [tool_reply("check_environment", {}), text_reply("done")]
    )
    AIAssistant(client, on_progress=lines.append).ask("check")

    assert any(line.startswith("Thinking") for line in lines)
    assert any("check_environment" in line for line in lines)


# -- invocation rendering ---------------------------------------------------


def test_invocation_summary_covers_each_outcome():
    ok = ToolInvocation("check_environment", {}, result={"ok": True}, duration_s=1.5)
    assert "ok (1.5s)" in ok.summary()

    declined = ToolInvocation("run_parametric_sweep", {}, declined=True)
    assert "declined" in declined.summary()

    failed = ToolInvocation("check_environment", {}, error="boom")
    assert "boom" in failed.summary()

    refused = ToolInvocation(
        "check_environment", {}, result={"ok": False, "error": "bad angle"}
    )
    assert "bad angle" in refused.summary()


def test_long_arguments_are_abbreviated_for_display():
    invocation = ToolInvocation("m", {"path": "C:/" + "x" * 200})
    assert len(invocation.summary()) < 120


# -- transport --------------------------------------------------------------


def test_a_client_without_a_key_refuses_to_be_built():
    with pytest.raises(AuthenticationError):
        OpenRouterClient("")


def test_create_assistant_without_a_key_explains_how_to_set_one(
    isolated_data_root, monkeypatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import core.credentials as credentials

    monkeypatch.setattr(credentials, "_keyring", lambda: None)

    with pytest.raises(AuthenticationError) as excinfo:
        create_assistant()
    assert "openrouter.ai/keys" in str(excinfo.value)


def test_create_assistant_uses_an_explicit_key():
    assistant = create_assistant(api_key="sk-or-test", model="x/y")
    assert assistant.model == "x/y"
    assert isinstance(assistant.client, OpenRouterClient)


def test_the_fake_client_reports_a_catalogue():
    models = FakeChatClient([]).list_models()
    assert models[0]["id"] == DEFAULT_MODEL


class FakeResponse:
    """Minimal stand-in for a ``requests`` response."""

    def __init__(self, status_code: int, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def install_response(monkeypatch, response: FakeResponse) -> None:
    """Make every HTTP request return one canned response."""
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(
        requests, "request", lambda *args, **kwargs: response
    )


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, "rejected the API key"),
        (403, "rejected the API key"),
        (402, "insufficient credit"),
        (429, "rate-limiting"),
        (500, "HTTP 500"),
    ],
)
def test_http_failures_become_readable_messages(monkeypatch, status, expected):
    install_response(
        monkeypatch, FakeResponse(status, {"error": {"message": "upstream"}})
    )
    client = OpenRouterClient("sk-or-test")

    error_type = AuthenticationError if status in (401, 403) else AIAgentError
    with pytest.raises(error_type) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert expected in str(excinfo.value)


def test_a_network_failure_suggests_checking_the_connection(monkeypatch):
    requests = pytest.importorskip("requests")

    def explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(requests, "request", explode)
    with pytest.raises(AIAgentError) as excinfo:
        OpenRouterClient("sk-or-test").complete([])
    assert "could not reach OpenRouter" in str(excinfo.value)


def test_an_empty_choice_list_is_reported(monkeypatch):
    install_response(monkeypatch, FakeResponse(200, {"choices": []}))
    with pytest.raises(AIAgentError):
        OpenRouterClient("sk-or-test").complete([])


def test_a_completion_is_returned_with_its_usage(monkeypatch):
    install_response(
        monkeypatch,
        FakeResponse(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"total_tokens": 7},
            },
        ),
    )
    message = OpenRouterClient("sk-or-test").complete([])
    assert message["content"] == "hello"
    assert message["_usage"]["total_tokens"] == 7


def test_the_model_catalogue_is_read_live(monkeypatch):
    """The catalogue changes constantly, so it is fetched rather than pinned."""
    install_response(
        monkeypatch,
        FakeResponse(
            200,
            {
                "data": [
                    {"id": "a/b", "name": "A B", "context_length": 32000},
                    {"name": "no id — skipped"},
                ]
            },
        ),
    )
    models = OpenRouterClient("sk-or-test").list_models()
    assert [model["id"] for model in models] == ["a/b"]
    assert models[0]["context_length"] == 32000


def test_the_api_key_is_sent_as_a_bearer_token():
    headers = OpenRouterClient("sk-or-test")._headers()
    assert headers["Authorization"] == "Bearer sk-or-test"


# ---------------------------------------------------------------------------
# What a request cost
# ---------------------------------------------------------------------------


def usage_reply(text, prompt=0, completion=0, cost=None, seconds=0.0):
    """A scripted reply carrying usage accounting."""
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "generation_seconds": seconds,
    }
    if cost is not None:
        usage["cost"] = cost
    message = text_reply(text)
    message["_usage"] = usage
    return message


def test_tokens_rate_and_cost_are_reported(tmp_path):
    """The operator should see what a request spent, not just its answer."""
    assistant = AIAssistant(
        FakeChatClient([usage_reply("done", 1200, 300, cost=0.0041, seconds=6.0)])
    )
    reply = assistant.ask("how much?")

    assert reply.metrics.prompt_tokens == 1200
    assert reply.metrics.completion_tokens == 300
    assert reply.metrics.total_tokens == 1500
    assert reply.metrics.tokens_per_second == pytest.approx(50.0)
    assert reply.metrics.cost_usd == pytest.approx(0.0041)


def test_a_missing_cost_stays_missing(tmp_path):
    """Not every provider reports a price, and zero is not the same as unknown."""
    assistant = AIAssistant(FakeChatClient([usage_reply("done", 10, 5, seconds=1.0)]))
    reply = assistant.ask("how much?")
    assert reply.metrics.cost_usd is None
    assert "$" not in reply.metrics.summary()


def test_the_rate_is_measured_against_generation_not_tool_time():
    """A request that spends four minutes meshing has not slowed the model.

    Tokens per second is a property of the model; dividing by wall time
    would make every request that touches the solver look glacial.
    """
    from backend.ai_agent import UsageMetrics

    metrics = UsageMetrics(
        prompt_tokens=100,
        completion_tokens=200,
        generation_seconds=4.0,
        elapsed_seconds=240.0,
    )
    assert metrics.tokens_per_second == pytest.approx(50.0)


def test_metrics_accumulate_across_tool_rounds():
    """A multi-step request reports what the whole thing cost."""
    first = tool_reply("check_environment", {})
    first["_usage"] = {
        "prompt_tokens": 500,
        "completion_tokens": 50,
        "cost": 0.001,
        "generation_seconds": 1.0,
    }
    assistant = AIAssistant(
        FakeChatClient([first, usage_reply("ready", 700, 100, cost=0.002, seconds=2.0)])
    )
    reply = assistant.ask("is the solver ready?")

    assert reply.metrics.prompt_tokens == 1200
    assert reply.metrics.completion_tokens == 150
    assert reply.metrics.cost_usd == pytest.approx(0.003)
    assert reply.metrics.rounds == 2


def test_metrics_are_reported_while_the_request_runs():
    """Waiting for the end to learn the cost is too late to act on it."""
    first = tool_reply("check_environment", {})
    first["_usage"] = {"prompt_tokens": 500, "completion_tokens": 50}
    seen = []
    assistant = AIAssistant(
        FakeChatClient([first, usage_reply("ready", 700, 100)]),
        on_metrics=seen.append,
    )
    assistant.ask("is the solver ready?")

    assert len(seen) >= 2, "the meter only updated once, at the end"
    assert seen[0].total_tokens == 550


def test_a_model_that_reports_nothing_does_not_break_the_meter():
    """Usage is optional in the API and its absence is not an error."""
    assistant = AIAssistant(FakeChatClient([text_reply("done")]))
    reply = assistant.ask("hello")
    assert reply.metrics.total_tokens == 0
    assert reply.metrics.tokens_per_second == 0.0


def test_cost_accounting_is_requested_from_openrouter(monkeypatch):
    """OpenRouter only prices a request when asked to."""
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return FakeResponse(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.1},
            },
        )

    monkeypatch.setattr("requests.request", fake_request)
    message = OpenRouterClient("sk-or-v1-test").complete([{"role": "user", "content": "x"}])

    assert captured.get("usage") == {"include": True}
    assert message["_usage"]["cost"] == pytest.approx(0.1)
    # Time spent waiting on the model is measured here, not guessed later.
    assert message["_usage"]["generation_seconds"] >= 0.0


# ---------------------------------------------------------------------------
# A running account of what the assistant is doing
# ---------------------------------------------------------------------------


def test_every_step_is_announced_as_it_happens():
    """A long request must narrate itself, not report at the end.

    The operator's seventeen-minute request showed nothing at all while it
    ran. These events are what the transcript is built from.
    """
    from backend.ai_agent import (
        EVENT_ROUND,
        EVENT_TEXT,
        EVENT_TOOL_FINISHED,
        EVENT_TOOL_STARTED,
    )

    thinking = tool_reply("check_environment", {})
    thinking["content"] = "Checking the toolchain first."
    events = []
    assistant = AIAssistant(
        FakeChatClient([thinking, text_reply("Ready.")]), on_event=events.append
    )
    assistant.ask("can I solve?")

    kinds = [event.kind for event in events]
    assert kinds == [
        EVENT_ROUND,
        EVENT_TEXT,
        EVENT_TOOL_STARTED,
        EVENT_TOOL_FINISHED,
        EVENT_ROUND,
    ]


def test_the_models_own_commentary_is_surfaced():
    """The model explains itself before calling a tool; show that.

    It was being stored in the message list and never displayed, which
    threw away the best account of what was happening that anyone gets --
    and it had already been paid for.
    """
    from backend.ai_agent import EVENT_TEXT

    thinking = tool_reply("check_environment", {})
    thinking["content"] = "I will look at the environment before meshing."
    events = []
    AIAssistant(
        FakeChatClient([thinking, text_reply("done")]), on_event=events.append
    ).ask("go")

    said = [event.message for event in events if event.kind == EVENT_TEXT]
    assert said == ["I will look at the environment before meshing."]


def test_a_tools_arguments_are_announced_before_it_runs():
    """Four minutes into a mesh, "Running set_geometry_and_mesh" is not enough."""
    from backend.ai_agent import EVENT_TOOL_STARTED

    events = []
    AIAssistant(
        FakeChatClient(
            [
                tool_reply("run_sensor_thermal_simulation", {"analytic_only": True}),
                text_reply("done"),
            ]
        ),
        on_event=events.append,
    ).ask("how far off is it?")

    started = [e for e in events if e.kind == EVENT_TOOL_STARTED]
    assert len(started) == 1
    assert started[0].tool == "run_sensor_thermal_simulation"
    assert started[0].arguments == {"analytic_only": True}


def test_a_finished_tool_reports_how_long_it_took():
    """Cost is time as much as money, and the operator is paying both."""
    from backend.ai_agent import EVENT_TOOL_FINISHED

    events = []
    AIAssistant(
        FakeChatClient(
            [tool_reply("check_environment", {}), text_reply("done")]
        ),
        on_event=events.append,
    ).ask("check")

    finished = [e for e in events if e.kind == EVENT_TOOL_FINISHED]
    assert len(finished) == 1
    assert finished[0].duration_s >= 0.0
    assert finished[0].failed is False


def test_a_declined_tool_is_announced_as_a_failure():
    """Declining must be visible; a silent skip looks like a hang."""
    from backend.ai_agent import EVENT_TOOL_FINISHED

    events = []
    AIAssistant(
        FakeChatClient(
            [
                tool_reply("run_parametric_sweep", {"parameter": "mach"}),
                text_reply("stopped"),
            ]
        ),
        approve=lambda name, arguments: False,
        on_event=events.append,
    ).ask("sweep")

    declined = [
        e for e in events if e.kind == EVENT_TOOL_FINISHED and e.failed
    ]
    assert declined and "Declined" in declined[0].message


def test_the_plain_progress_channel_still_carries_the_same_lines():
    """Adding the structured channel must not break anything reading text."""
    lines = []
    events = []
    AIAssistant(
        FakeChatClient(
            [tool_reply("check_environment", {}), text_reply("done")]
        ),
        on_progress=lines.append,
        on_event=events.append,
    ).ask("check")

    assert any("Thinking (round 1)" in line for line in lines)
    assert any("Running check_environment" in line for line in lines)
    # Every event reached the text channel too.
    assert [event.message for event in events] == lines


# ---------------------------------------------------------------------------
# Surviving a dropped connection
# ---------------------------------------------------------------------------


@pytest.fixture
def instant_backoff(monkeypatch):
    """Retry without the wait, so these tests are about behaviour not patience."""
    import backend.ai_agent as module

    monkeypatch.setattr(module, "_RETRY_BACKOFF_S", (0.0, 0.0, 0.0))


def test_a_dropped_connection_is_retried(monkeypatch, instant_backoff):
    """The failure that cost the operator a seventeen-minute request.

    Windows resets a connection mid-request and the whole conversation,
    tool work and tokens already paid for, used to go with it.
    """
    attempts = {"n": 0}

    def flaky(method, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionResetError(10054, "forcibly closed by the remote host")
        return FakeResponse(
            200, {"choices": [{"message": {"role": "assistant", "content": "back"}}]}
        )

    monkeypatch.setattr("requests.request", flaky)
    reported = []
    client = OpenRouterClient("sk-or-v1-test", on_retry=reported.append)

    message = client.complete([{"role": "user", "content": "hi"}])

    assert attempts["n"] == 2
    assert message["content"] == "back"
    assert reported and "retrying" in reported[0]


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transient_server_failures_are_retried(monkeypatch, instant_backoff, status):
    """A provider having a bad second should not end the session."""
    attempts = {"n": 0}

    def flaky(method, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse(status, {"error": {"message": "later"}})
        return FakeResponse(
            200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    monkeypatch.setattr("requests.request", flaky)
    message = OpenRouterClient("sk-or-v1-test").complete(
        [{"role": "user", "content": "hi"}]
    )
    assert attempts["n"] == 2
    assert message["content"] == "ok"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_permanent_failures_are_not_retried(monkeypatch, instant_backoff, status):
    """A bad key or an empty balance will not improve by asking again.

    Retrying these would delay a clear answer and, for a completion, could
    bill the operator more than once for the same mistake.
    """
    attempts = {"n": 0}

    def denied(method, url, **kwargs):
        attempts["n"] += 1
        return FakeResponse(status, {"error": {"message": "no"}})

    monkeypatch.setattr("requests.request", denied)
    with pytest.raises(AIAgentError):
        OpenRouterClient("sk-or-v1-test").complete([{"role": "user", "content": "x"}])
    assert attempts["n"] == 1


def test_retrying_can_be_turned_off(monkeypatch, instant_backoff):
    """A completion is not idempotent for billing, so this is the operator's call."""
    attempts = {"n": 0}

    def flaky(method, url, **kwargs):
        attempts["n"] += 1
        raise ConnectionResetError(10054, "forcibly closed")

    monkeypatch.setattr("requests.request", flaky)
    with pytest.raises(AIAgentError):
        OpenRouterClient("sk-or-v1-test", max_attempts=1).complete(
            [{"role": "user", "content": "x"}]
        )
    assert attempts["n"] == 1


def test_a_server_that_says_when_to_come_back_is_obeyed(monkeypatch):
    """Retry-After is the server telling us what it wants; honour it."""
    import backend.ai_agent as module

    waited = []
    monkeypatch.setattr(module.time, "sleep", waited.append)
    attempts = {"n": 0}

    def limited(method, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            response = FakeResponse(429, {"error": {"message": "slow down"}})
            response.headers = {"Retry-After": "7"}
            return response
        return FakeResponse(
            200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    monkeypatch.setattr("requests.request", limited)
    OpenRouterClient("sk-or-v1-test").complete([{"role": "user", "content": "x"}])
    assert waited == [7.0]


def test_an_absurd_retry_after_is_capped(monkeypatch):
    """A server asking for an hour must not freeze the interface for one."""
    import backend.ai_agent as module

    waited = []
    monkeypatch.setattr(module.time, "sleep", waited.append)
    attempts = {"n": 0}

    def limited(method, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            response = FakeResponse(429, {"error": {"message": "much later"}})
            response.headers = {"Retry-After": "3600"}
            return response
        return FakeResponse(
            200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    monkeypatch.setattr("requests.request", limited)
    OpenRouterClient("sk-or-v1-test").complete([{"role": "user", "content": "x"}])
    assert waited == [module._MAX_RETRY_AFTER_S]


def test_a_retry_reaches_the_transcript(monkeypatch, instant_backoff):
    """A silent retry is indistinguishable from a hang."""
    from backend.ai_agent import EVENT_RETRY

    attempts = {"n": 0}

    def flaky(method, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionResetError(10054, "forcibly closed")
        return FakeResponse(
            200, {"choices": [{"message": {"role": "assistant", "content": "back"}}]}
        )

    monkeypatch.setattr("requests.request", flaky)
    monkeypatch.setattr("core.credentials.load_api_key", lambda *a, **k: "sk-or-v1-x")

    events = []
    assistant = create_assistant(api_key="sk-or-v1-test", on_event=events.append)
    assistant.ask("hello")

    assert any(event.kind == EVENT_RETRY for event in events)
