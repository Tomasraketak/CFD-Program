"""The AI assistant tab.

Built for real against Qt's offscreen platform. The properties checked are the
ones an operator depends on: a key is never left visible or echoed back, the
plain-text fallback is labelled as such, an expensive tool is confirmed before
it runs, and a failure lands in the transcript rather than an exception.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# pydantic must finish importing before Qt; see gui/__init__.py.
from core.models import FlowParams  # noqa: E402,F401
from core.settings import AppSettings  # noqa: E402

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from backend.ai_agent import FakeChatClient  # noqa: E402
from core.credentials import save_api_key  # noqa: E402
from gui import ai_panel  # noqa: E402
from gui.ai_panel import AITab  # noqa: E402
from gui.main_window import MainWindow, build_application  # noqa: E402
from tests.test_ai_agent import text_reply, tool_reply  # noqa: E402

KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def qt_app():
    """One QApplication for the module."""
    app = build_application([])
    yield app
    app.processEvents()


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    """Use the file fallback, so nothing touches the real keystore."""
    import core.credentials as credentials

    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture
def ai_tab(qt_app, isolated_data_root):
    """An assistant tab rooted in the isolated data directory."""
    tab = AITab(AppSettings(), isolated_data_root)
    yield tab
    tab.deleteLater()


# -- construction -----------------------------------------------------------


def test_the_tab_is_reachable_from_the_main_window(qt_app, store):
    window = MainWindow(store)
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "AI Assistant" in titles
    window.deleteLater()


def test_an_empty_transcript_shows_example_requests(ai_tab):
    """A blank box tells a first-time operator nothing."""
    text = ai_tab.transcript.toPlainText()
    assert "AI assistant" in text
    assert "BMP580" in text


def test_the_settings_are_loaded_into_the_widgets(qt_app, isolated_data_root):
    settings = AppSettings(
        ai_model="google/gemini-2.0-flash-001",
        ai_confirm_long_tools=False,
        ai_max_tool_rounds=5,
    )
    tab = AITab(settings, isolated_data_root)
    # Not on the shortlist, so it arrives in the Custom box.
    assert tab.selected_model() == "google/gemini-2.0-flash-001"
    assert tab.model_combo.currentText() == ai_panel.CUSTOM_MODEL_LABEL
    assert tab.custom_model.text() == "google/gemini-2.0-flash-001"
    assert tab.confirm_long.isChecked() is False
    assert tab.max_rounds.value() == 5
    tab.deleteLater()


# -- the API key ------------------------------------------------------------


def test_with_no_key_the_status_says_the_assistant_is_disabled(ai_tab):
    assert "No key stored" in ai_tab.key_status.text()


def test_saving_a_key_clears_the_box_and_never_echoes_it(ai_tab, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    ai_tab.key_input.setText(KEY)
    ai_tab.save_key()

    assert ai_tab.key_input.text() == ""
    assert KEY not in ai_tab.key_status.text()
    assert ai_tab.key_status.text().startswith("sk-or-")


def test_the_key_input_is_masked_on_screen(ai_tab):
    assert (
        ai_tab.key_input.echoMode() != QtWidgets.QLineEdit.EchoMode.Normal
    )


def test_the_plain_text_fallback_is_announced(ai_tab, monkeypatch):
    """Storage that is not secure must say so rather than imply safety."""
    shown: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "information",
        lambda parent, title, text, *a, **k: shown.append(text),
    )
    ai_tab.key_input.setText(KEY)
    ai_tab.save_key()

    assert shown and "plain-text" in shown[0]


def test_saving_an_empty_key_warns_and_stores_nothing(ai_tab, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda parent, title, text, *a, **k: warned.append(text),
    )
    ai_tab.key_input.setText("   ")
    ai_tab.save_key()

    assert warned
    assert "No key stored" in ai_tab.key_status.text()


def test_removing_a_key_disables_the_assistant_again(ai_tab, monkeypatch):
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    ai_tab.key_input.setText(KEY)
    ai_tab.save_key()
    ai_tab.clear_key()

    assert "No key stored" in ai_tab.key_status.text()
    assert ai_tab.assistant is None


def test_fetching_models_without_a_key_warns(ai_tab, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda parent, title, text, *a, **k: warned.append(text),
    )
    ai_tab.fetch_models()
    assert warned


def test_fetching_models_fills_the_combo(ai_tab, isolated_data_root, monkeypatch):
    save_api_key(KEY, root=isolated_data_root)

    class StubClient:
        def __init__(self, key, *args, **kwargs):
            pass

        def list_models(self):
            return [{"id": "z/last"}, {"id": "a/first"}]

    monkeypatch.setattr(ai_panel, "OpenRouterClient", StubClient)
    ai_tab.fetch_models()

    items = [ai_tab.model_combo.itemText(i) for i in range(ai_tab.model_combo.count())]
    # Custom first, then the shortlist, then the fetched ids, sorted -- the
    # shortlist must not be buried under the whole catalogue.
    assert items[0] == ai_panel.CUSTOM_MODEL_LABEL
    listed = [item for item in items[1:] if item]
    shortlist = list(ai_panel.SUGGESTED_MODELS)
    assert listed[: len(shortlist)] == shortlist
    assert listed[len(shortlist):] == [
        ai_panel.FETCHED_MODELS_LABEL, "a/first", "z/last"
    ]
    assert "2 models" in ai_tab.model_status.text()


def test_fetching_keeps_the_chosen_model(ai_tab, isolated_data_root, monkeypatch):
    """Refreshing the catalogue must not silently switch models."""
    save_api_key(KEY, root=isolated_data_root)

    class StubClient:
        def __init__(self, key, *args, **kwargs):
            pass

        def list_models(self):
            return [{"id": "qwen/qwen3.7-flash"}, {"id": "a/first"}]

    monkeypatch.setattr(ai_panel, "OpenRouterClient", StubClient)
    ai_tab.set_model("qwen/qwen3.7-flash")
    ai_tab.fetch_models()
    assert ai_tab.selected_model() == "qwen/qwen3.7-flash"


# -- conversation -----------------------------------------------------------


def make_assistant(tab, responses, **kwargs):
    """Attach an assistant driven by a scripted client."""
    from backend.ai_agent import AIAssistant

    tab.assistant = AIAssistant(
        FakeChatClient(responses), approve=tab._approve_tool, **kwargs
    )
    return tab.assistant


def test_a_request_is_rendered_into_the_transcript(ai_tab, qt_app):
    make_assistant(ai_tab, [text_reply("Mach 0.8 is the threshold.")])
    ai_tab.input.setPlainText("What is the transonic threshold?")
    ai_tab.send()

    ai_tab.pool.waitForDone(10_000)
    qt_app.processEvents()

    text = ai_tab.transcript.toPlainText()
    assert "transonic threshold" in text
    assert "Mach 0.8 is the threshold." in text
    assert ai_tab.input.toPlainText() == ""


def test_tool_calls_are_shown_not_hidden(ai_tab, qt_app):
    """An assistant that acts silently is worse than none at all."""
    make_assistant(
        ai_tab,
        [
            tool_reply("run_sensor_thermal_simulation", {"analytic_only": True}),
            text_reply("About 2 K high."),
        ],
    )
    ai_tab.input.setPlainText("How far off is the sensor?")
    ai_tab.send()
    ai_tab.pool.waitForDone(20_000)
    qt_app.processEvents()

    assert "run_sensor_thermal_simulation" in ai_tab.transcript.toPlainText()


def test_an_empty_request_does_nothing(ai_tab):
    ai_tab.input.setPlainText("   ")
    before = ai_tab.transcript.toPlainText()
    ai_tab.send()
    assert ai_tab.transcript.toPlainText() == before


def test_without_a_key_sending_reports_how_to_set_one(ai_tab):
    ai_tab.input.setPlainText("mesh my rocket")
    ai_tab.send()
    assert "openrouter.ai/keys" in ai_tab.transcript.toPlainText()


def test_a_failure_reaches_the_transcript_and_re_enables_input(ai_tab):
    ai_tab._set_busy(True)
    ai_tab._on_failed("OpenRouter rejected the API key.")

    assert "rejected the API key" in ai_tab.transcript.toPlainText()
    assert ai_tab.send_button.isEnabled()
    assert ai_tab.input.isReadOnly() is False


def test_a_new_conversation_forgets_the_history(ai_tab):
    assistant = make_assistant(ai_tab, [text_reply("hi")])
    assistant.messages.append({"role": "user", "content": "old"})
    ai_tab.new_conversation()

    assert assistant.history() == []
    assert "AI assistant" in ai_tab.transcript.toPlainText()


# -- operator control -------------------------------------------------------


def test_an_expensive_tool_is_confirmed_before_it_runs(ai_tab, monkeypatch):
    """The dialog must name the tool and say what it costs."""
    questions: list[str] = []

    def answer(parent, title, text, *args, **kwargs):
        questions.append(text)
        return QtWidgets.QMessageBox.StandardButton.No

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", answer)
    allowed = ai_tab._approve_tool("run_parametric_sweep", {"parameter": "mach"})

    assert allowed is False
    assert "run_parametric_sweep" in questions[0]
    assert "3 to 8 minutes per point" in questions[0]
    assert "parameter = mach" in questions[0]


def test_approval_is_bypassed_when_the_operator_turns_it_off(ai_tab):
    ai_tab.confirm_long.setChecked(False)
    make_assistant(ai_tab, [text_reply("ok")])
    ai_tab._apply_settings_to_assistant()
    assert ai_tab.assistant.approve is None


def test_interface_choices_are_pushed_onto_the_assistant(ai_tab):
    make_assistant(ai_tab, [text_reply("ok")])
    ai_tab.set_model("openai/gpt-4o-mini")
    ai_tab.max_rounds.setValue(4)
    ai_tab._apply_settings_to_assistant()

    assert ai_tab.assistant.model == "openai/gpt-4o-mini"
    assert ai_tab.assistant.max_rounds == 4


def test_choices_are_remembered_between_sessions(ai_tab, isolated_data_root):
    ai_tab.set_model("deepseek/deepseek-r1")
    ai_tab.confirm_long.setChecked(False)
    ai_tab.max_rounds.setValue(7)
    ai_tab._persist_settings()

    reloaded = AppSettings.load(isolated_data_root)
    assert reloaded.ai_model == "deepseek/deepseek-r1"
    assert reloaded.ai_confirm_long_tools is False
    assert reloaded.ai_max_tool_rounds == 7


def test_a_second_request_while_busy_is_refused(ai_tab, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda parent, title, text, *a, **k: warned.append(text),
    )
    make_assistant(ai_tab, [text_reply("ok")])
    ai_tab._set_busy(True)
    ai_tab.input.setPlainText("another request")
    ai_tab.send()

    assert warned
    ai_tab._set_busy(False)


def test_the_confirmation_dialog_works_from_the_worker_thread(ai_tab, qt_app, monkeypatch):
    """The approval callback runs off the GUI thread; the dialog must not.

    A plain timer started in the worker thread would never fire, hanging the
    request forever, so the cross-thread marshalling is exercised for real.
    """
    seen: list[str] = []

    def answer(parent, title, text, *args, **kwargs):
        seen.append(QtCore.QThread.currentThread().objectName() or "gui")
        return QtWidgets.QMessageBox.StandardButton.No

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", answer)
    make_assistant(
        ai_tab,
        [
            tool_reply("run_parametric_sweep", {"parameter": "mach"}),
            text_reply("Stopped, as you asked."),
        ],
    )
    ai_tab.input.setPlainText("sweep Mach")
    ai_tab.send()

    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while ai_tab._busy and deadline.elapsed() < 20_000:
        qt_app.processEvents()

    assert seen, "the dialog never appeared — it was queued to a dead thread"
    assert "declined" in ai_tab.transcript.toPlainText()


# -- model choice and the running meter -------------------------------------


def test_the_model_list_is_the_requested_drop_down(ai_tab):
    """Custom plus the requested models, in a list that is not a text box."""
    items = [ai_tab.model_combo.itemText(i) for i in range(ai_tab.model_combo.count())]
    assert items[0] == ai_panel.CUSTOM_MODEL_LABEL
    # An editable combo looks like a line edit once styled, which is how the
    # operator came to report the list missing.
    assert not ai_tab.model_combo.isEditable()
    for requested in (
        "deepseek/deepseek-v4.1-flash",
        "meta/muse-spark-1.3-contributor",
        "qwen/qwen3.7-flash",
        "openai/gpt-5.6-luna",
    ):
        assert requested in items


def test_the_drop_down_draws_its_arrow(qt_app):
    """Styling the drop-down button hides Qt's own arrow; ours must be there."""
    from gui.main_window import _arrow_rules

    rules = _arrow_rules()
    assert "QComboBox::down-arrow" in rules
    path = rules.split('url("', 1)[1].split('")', 1)[0]
    assert Path(path).is_file()

    combo = QtWidgets.QComboBox()
    combo.addItems(["one", "two"])
    combo.resize(200, 32)
    combo.show()
    qt_app.processEvents()
    image = combo.grab().toImage()
    bright = sum(
        1
        for x in range(image.width() - 24, image.width() - 2)
        for y in range(4, image.height() - 4)
        if QtGui.QColor(image.pixel(x, y)).lightness() > 90
    )
    combo.deleteLater()
    assert bright > 0, "no arrow in the drop-down button"


def test_choosing_custom_reveals_a_box_for_any_id(ai_tab):
    """Picking Custom should not send 'Custom' to OpenRouter as a model id."""
    ai_tab.set_model("qwen/qwen3.7-flash")
    assert ai_tab.custom_model.isHidden()

    ai_tab.model_combo.setCurrentIndex(0)
    assert not ai_tab.custom_model.isHidden()
    # An empty box never leaks the label into a request.
    ai_tab.custom_model.clear()
    assert ai_tab.selected_model() == ai_panel.DEFAULT_MODEL


def test_a_typed_model_id_is_used(ai_tab):
    """Any id OpenRouter knows remains usable."""
    ai_tab.model_combo.setCurrentIndex(0)
    ai_tab.custom_model.setText("someone/some-new-model")
    assert ai_tab.selected_model() == "someone/some-new-model"


def test_a_listed_model_is_used(ai_tab):
    ai_tab.set_model("openai/gpt-5.6-luna")
    assert ai_tab.model_combo.currentText() == "openai/gpt-5.6-luna"
    assert ai_tab.selected_model() == "openai/gpt-5.6-luna"


def test_the_custom_placeholder_is_never_persisted(ai_tab, isolated_data_root):
    """Restarting must not restore a model id that is not one."""
    ai_tab.model_combo.setCurrentIndex(0)
    ai_tab.custom_model.clear()
    ai_tab._persist_settings()
    assert ai_tab.settings.ai_model == ai_panel.DEFAULT_MODEL


def test_the_meter_reports_tokens_rate_and_cost(ai_tab):
    """Cost and throughput are shown while the request is still running."""
    from backend.ai_agent import UsageMetrics

    ai_tab._start_meter()
    ai_tab._on_metrics(
        UsageMetrics(
            prompt_tokens=1200,
            completion_tokens=300,
            cost_usd=0.00412,
            generation_seconds=6.0,
            rounds=1,
        )
    )
    text = ai_tab.meter.text()
    assert "1,500 tokens" in text
    assert "50.0 tok/s" in text
    assert "$0.0041" in text


def test_the_meter_omits_a_cost_nobody_reported(ai_tab):
    """A provider that reports no price must not produce a fabricated zero."""
    from backend.ai_agent import UsageMetrics

    ai_tab._start_meter()
    ai_tab._on_metrics(
        UsageMetrics(prompt_tokens=10, completion_tokens=5, generation_seconds=1.0)
    )
    assert "$" not in ai_tab.meter.text()


def test_the_meter_clock_runs_between_rounds(ai_tab, monkeypatch):
    """Token counts only move per round; the elapsed time must not look stuck."""
    from backend.ai_agent import UsageMetrics

    clock = {"now": 100.0}
    monkeypatch.setattr(ai_panel.time, "monotonic", lambda: clock["now"])

    ai_tab._start_meter()
    ai_tab._on_metrics(UsageMetrics(prompt_tokens=10, completion_tokens=5))
    clock["now"] = 137.0
    ai_tab._refresh_meter()
    assert "37s" in ai_tab.meter.text()


def test_a_finished_request_leaves_its_totals_on_screen(ai_tab, qt_app):
    """After the reply, the meter shows what that request cost."""
    make_assistant(
        ai_tab,
        [
            {
                "role": "assistant",
                "content": "done",
                "_usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 100,
                    "cost": 0.002,
                    "generation_seconds": 2.0,
                },
            }
        ],
    )
    ai_tab.input.setPlainText("what is the environment?")
    ai_tab.send()
    ai_tab.pool.waitForDone(10_000)
    qt_app.processEvents()

    assert "1,000 tokens" in ai_tab.meter.text()
    assert "$0.0020" in ai_tab.meter.text()
    assert "50.0 tok/s" in ai_tab.meter.text()


def test_a_new_conversation_clears_the_meter(ai_tab):
    """A fresh conversation starts from zero, not from the last one's total."""
    from backend.ai_agent import UsageMetrics

    ai_tab._start_meter()
    ai_tab._on_metrics(UsageMetrics(prompt_tokens=10, completion_tokens=5))
    ai_tab.new_conversation()
    assert ai_tab.meter.text() == ""


# -- the running account ----------------------------------------------------


def test_the_transcript_fills_in_while_the_request_runs(ai_tab, qt_app):
    """Seventeen minutes of a blank panel is what this prevents.

    Nothing used to reach the transcript until the whole request finished.
    Everything the assistant does now lands there as it happens.
    """
    thinking = tool_reply("check_environment", {})
    thinking["content"] = "Checking the toolchain before I mesh anything."
    make_assistant(ai_tab, [thinking, text_reply("SU2 is ready.")])

    ai_tab.input.setPlainText("can I run a solve?")
    ai_tab.send()
    ai_tab.pool.waitForDone(20_000)
    qt_app.processEvents()

    text = ai_tab.transcript.toPlainText()
    assert "round 1" in text
    assert "Checking the toolchain before I mesh anything." in text
    assert "check_environment()" in text
    assert "SU2 is ready." in text


def test_the_tool_list_is_not_repeated_at_the_end(ai_tab, qt_app):
    """Calls are shown as they happen; listing them again shows the run twice."""
    make_assistant(
        ai_tab,
        [tool_reply("check_environment", {}), text_reply("done")],
    )
    ai_tab.input.setPlainText("check the environment")
    ai_tab.send()
    ai_tab.pool.waitForDone(20_000)
    qt_app.processEvents()

    assert ai_tab.transcript.toPlainText().count("check_environment") == 1


def test_a_tools_arguments_are_visible_in_the_transcript(ai_tab, qt_app):
    """Which file, which resolution — not just which tool."""
    make_assistant(
        ai_tab,
        [
            tool_reply("run_sensor_thermal_simulation", {"analytic_only": True}),
            text_reply("about 2 K high"),
        ],
    )
    ai_tab.input.setPlainText("how far off is the sensor?")
    ai_tab.send()
    ai_tab.pool.waitForDone(20_000)
    qt_app.processEvents()

    assert "analytic_only=True" in ai_tab.transcript.toPlainText()


def test_a_retry_is_written_into_the_transcript(ai_tab):
    """A silent retry looks exactly like a hang."""
    from backend.ai_agent import EVENT_RETRY, ProgressEvent

    ai_tab._on_event(
        ProgressEvent(
            kind=EVENT_RETRY,
            message="OpenRouter did not answer (reset); retrying in 2 s",
        )
    )
    assert "retrying in 2 s" in ai_tab.transcript.toPlainText()


def test_a_failed_tool_is_marked_as_failed(ai_tab):
    """A failure that reads like a success is worse than no message."""
    from backend.ai_agent import EVENT_TOOL_FINISHED, ProgressEvent

    ai_tab._on_event(
        ProgressEvent(
            kind=EVENT_TOOL_FINISHED,
            message="set_geometry_and_mesh(...) — failed: no such file",
            tool="set_geometry_and_mesh",
            failed=True,
        )
    )
    text = ai_tab.transcript.toPlainText()
    assert "failed" in text and "no such file" in text


def test_a_long_argument_is_clipped_not_dumped(ai_tab):
    """A full Windows path per line would push the conversation off screen."""
    from backend.ai_agent import EVENT_TOOL_STARTED, ProgressEvent

    ai_tab._on_event(
        ProgressEvent(
            kind=EVENT_TOOL_STARTED,
            message="Running set_geometry_and_mesh …",
            tool="set_geometry_and_mesh",
            arguments={"step_file_path": "C:\\" + "very-long-folder\\" * 12 + "r.step"},
        )
    )
    line = ai_tab.transcript.toPlainText().strip().splitlines()[-1]
    assert len(line) < 120
    assert line.endswith("…)")


# -- stopping ---------------------------------------------------------------


def test_stop_cancels_the_running_solve_and_unlocks_the_panel(ai_tab, qt_app):
    """Without this the only way out of a wedged solve was closing the program.

    The tab's Cancel button never knew about the runner the MCP layer builds
    for an assistant-run solve, so a fifteen-minute hang locked the panel for
    good.
    """
    from backend.runner import FakeRunner, _register_runner, _unregister_runner

    assistant = make_assistant(ai_tab, [text_reply("ok")])
    solving = FakeRunner(lines=[])
    _register_runner(solving)
    try:
        ai_tab._set_busy(True)
        assert ai_tab.stop_button.isEnabled()

        ai_tab.stop()

        assert assistant.stop_requested
        assert solving._cancelled.is_set(), "the running solve was not cancelled"
    finally:
        _unregister_runner(solving)

    # The worker unlocks the panel when it reports back.
    ai_tab._set_busy(False)
    assert not ai_tab.stop_button.isEnabled()
    assert ai_tab.send_button.isEnabled()


def test_stop_does_nothing_when_nothing_is_running(ai_tab, qt_app):
    """Pressing Stop on an idle panel must not disturb it."""
    make_assistant(ai_tab, [text_reply("ok")])
    ai_tab.stop()
    assert not ai_tab.assistant.stop_requested
    assert ai_tab.send_button.isEnabled()


def test_the_live_chart_goes_away_when_the_iterations_end(ai_tab):
    """It is there while the solver works, and gone once it has finished."""
    from backend.su2_parser import IterationRecord

    record = IterationRecord(iteration=1, values={"rms_rho": -2.0, "cd": 0.5, "cl": 0.0})
    ai_tab._on_solver_event("iteration", record)
    assert not ai_tab.solve_chart.isHidden()
    ai_tab._on_solver_event("finished", None)
    assert ai_tab.solve_chart.isHidden()
