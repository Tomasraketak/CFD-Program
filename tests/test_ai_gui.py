"""The AI assistant tab.

Built for real against Qt's offscreen platform. The properties checked are the
ones an operator depends on: a key is never left visible or echoed back, the
plain-text fallback is labelled as such, an expensive tool is confirmed before
it runs, and a failure lands in the transcript rather than an exception.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# pydantic must finish importing before Qt; see gui/__init__.py.
from core.models import FlowParams  # noqa: E402,F401
from core.settings import AppSettings  # noqa: E402

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtWidgets  # noqa: E402

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
    assert tab.model_combo.currentText() == "google/gemini-2.0-flash-001"
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
    # The Custom entry and its separator stay at the top; the ids are sorted.
    assert items[0] == ai_panel.CUSTOM_MODEL_LABEL
    assert [item for item in items[1:] if item] == ["a/first", "z/last"]
    assert "2 models" in ai_tab.model_status.text()


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
    ai_tab.model_combo.setCurrentText("openai/gpt-4o-mini")
    ai_tab.max_rounds.setValue(4)
    ai_tab._apply_settings_to_assistant()

    assert ai_tab.assistant.model == "openai/gpt-4o-mini"
    assert ai_tab.assistant.max_rounds == 4


def test_choices_are_remembered_between_sessions(ai_tab, isolated_data_root):
    ai_tab.model_combo.setCurrentText("deepseek/deepseek-r1")
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


def test_the_model_list_offers_a_custom_entry(ai_tab):
    """A shortlist is a convenience; any id must remain typeable."""
    items = [ai_tab.model_combo.itemText(i) for i in range(ai_tab.model_combo.count())]
    assert items[0] == ai_panel.CUSTOM_MODEL_LABEL
    assert ai_tab.model_combo.isEditable()
    for suggested in ai_panel.SUGGESTED_MODELS:
        assert suggested in items


def test_choosing_custom_clears_the_box_for_typing(ai_tab, qt_app):
    """Picking Custom should not send 'Custom' to OpenRouter as a model id."""
    ai_tab.model_combo.activated.emit(0)
    qt_app.processEvents()
    assert ai_tab.model_combo.currentText() == ""
    # And the placeholder never leaks into a request.
    assert ai_tab.selected_model() == ai_panel.DEFAULT_MODEL


def test_a_typed_model_id_is_used(ai_tab):
    """The whole point of an editable box."""
    ai_tab.model_combo.setCurrentText("someone/some-new-model")
    assert ai_tab.selected_model() == "someone/some-new-model"


def test_the_custom_placeholder_is_never_persisted(ai_tab, isolated_data_root):
    """Restarting must not restore a model id that is not one."""
    ai_tab.model_combo.setCurrentText(ai_panel.CUSTOM_MODEL_LABEL)
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
