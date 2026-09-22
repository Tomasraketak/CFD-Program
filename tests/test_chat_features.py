"""Conversation history, readable replies, live solve feedback and legends.

What an operator asked for after a week of use: pick a conversation up again
with the simulations it produced, see tables as tables, watch a solve
converge with its speed, see how long every step has been running, and read
the legend on a rendered image.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.models import FlowParams  # noqa: E402,F401  (pydantic before Qt)
from core.settings import AppSettings  # noqa: E402

pytest.importorskip("PySide6")

from PySide6 import QtCore  # noqa: E402

from core.chat_history import ChatHistory, Conversation, title_for  # noqa: E402
from gui.ai_panel import AITab  # noqa: E402
from gui.charts import ResidualChart, format_duration  # noqa: E402
from gui.main_window import build_application  # noqa: E402
from gui.markdown_render import markdown_to_html  # noqa: E402
from tests.test_ai_gui import make_assistant  # noqa: E402
from tests.test_ai_agent import text_reply  # noqa: E402

RESULT_TABLE = """Výsledky:

| | **Mach 1,3** | **Mach 0,7** |
|---|---|---|
| **F_z (osový odpor)** | **−527,7 N** | **−125,0 N** |
| C_d | 0,9965 | 0,8141 |
"""


@pytest.fixture(scope="module")
def qt_app():
    app = build_application([])
    yield app
    app.processEvents()


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    import core.credentials as credentials

    monkeypatch.setattr(credentials, "_keyring", lambda: None)


@pytest.fixture
def ai_tab(qt_app, isolated_data_root):
    tab = AITab(AppSettings(), isolated_data_root)
    yield tab
    tab.deleteLater()


# -- Markdown ----------------------------------------------------------------


def test_a_markdown_table_becomes_a_table():
    """The table the operator could not read, as rows of pipes."""
    rendered = markdown_to_html(RESULT_TABLE)
    assert "<table" in rendered
    assert rendered.count("<tr>") == 3
    assert "<th" in rendered and "<b>Mach 1,3</b>" in rendered
    assert "|---|" not in rendered
    # Numbers line up on the right.
    assert "text-align:right" in rendered


def test_markdown_from_the_model_cannot_inject_markup():
    rendered = markdown_to_html('<img src="x" onerror="boom"> **ok** <script>')
    assert "<img" not in rendered and "<script" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<b>ok</b>" in rendered


def test_headings_lists_and_code():
    rendered = markdown_to_html("## Síť\n- první\n- druhá\n\n1. a\n2. b\n\n`mesh-1`")
    assert "Síť" in rendered and "<ul>" in rendered and "<ol>" in rendered
    assert "<code" in rendered


# -- conversation history ------------------------------------------------------


def test_a_conversation_round_trips_and_lists_newest_first(tmp_path):
    history = ChatHistory(tmp_path)
    first = Conversation(messages=[{"role": "user", "content": "Vysíťuj raketu"}])
    history.save(first)
    second = Conversation(messages=[{"role": "user", "content": "Mach 1.3"}])
    history.save(second)

    listed = history.list()
    assert [c.chat_id for c in listed] == [second.chat_id, first.chat_id]
    assert history.load(first.chat_id).title == "Vysíťuj raketu"
    history.delete(first.chat_id)
    assert [c.chat_id for c in history.list()] == [second.chat_id]


def test_a_conversation_knows_the_runs_and_projects_it_touched():
    conversation = Conversation(
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "run", "arguments": '{"mesh_id": "mesh-20260922-182008-89d0e6"}'}}
                ],
            },
            {"role": "tool", "content": '{"sim_id": "aero-20260922-182119-3165c4", '
             '"project_path": "C:\\\\Users\\\\me\\\\SAPPHIRE_CFD.atsproj"}'},
        ]
    )
    assert conversation.run_ids() == [
        "mesh-20260922-182008-89d0e6",
        "aero-20260922-182119-3165c4",
    ]
    assert conversation.project_paths()[0].endswith("SAPPHIRE_CFD.atsproj")


def test_a_hostile_id_cannot_reach_outside_the_history_folder(tmp_path):
    with pytest.raises(ValueError):
        ChatHistory(tmp_path).load("../../settings")


def test_titles_come_from_the_first_request():
    long = "x" * 200
    assert title_for([{"role": "user", "content": long}]).endswith("…")
    assert title_for([]) == "New conversation"


def test_a_finished_reply_is_saved_and_can_be_continued(ai_tab, qt_app, isolated_data_root):
    """Pick a conversation up again: transcript back, and the model remembers."""
    make_assistant(ai_tab, [text_reply("Síť je hotová: mesh-20260922-182008-89d0e6")])
    ai_tab.input.setPlainText("Vysíťuj raketu")
    ai_tab.send()
    ai_tab.pool.waitForDone(10_000)
    qt_app.processEvents()

    saved = ChatHistory(isolated_data_root).list()
    assert len(saved) == 1
    chat_id = saved[0].chat_id

    ai_tab.new_conversation()
    assert ai_tab.assistant.history() == []
    ai_tab.open_conversation(chat_id)

    history = ai_tab.assistant.history()
    assert history[0]["content"] == "Vysíťuj raketu"
    transcript = ai_tab.transcript.toPlainText()
    assert "Síť je hotová" in transcript
    assert "mesh-20260922-182008-89d0e6" in transcript  # offered as a link

    opened = []
    ai_tab.openRun.connect(opened.append)
    ai_tab._on_link(QtCore.QUrl("ats-run:mesh-20260922-182008-89d0e6"))
    assert opened == ["mesh-20260922-182008-89d0e6"]


def test_a_conversation_opened_before_the_key_is_applied_later(ai_tab, isolated_data_root):
    history = ChatHistory(isolated_data_root)
    saved = Conversation(messages=[{"role": "user", "content": "a"},
                                   {"role": "assistant", "content": "b"}])
    history.save(saved)
    ai_tab.open_conversation(saved.chat_id)
    assert ai_tab._pending_messages == saved.messages
    make_assistant(ai_tab, [])
    assert ai_tab._ensure_assistant()
    assert ai_tab.assistant.history() == saved.messages


def test_replies_render_tables(ai_tab):
    ai_tab._append_assistant(RESULT_TABLE)
    assert "|---|" not in ai_tab.transcript.toPlainText()
    assert "<table" in ai_tab.transcript.toHtml()


# -- time and live solve --------------------------------------------------------


def test_durations_read_naturally():
    assert format_duration(7) == "7 s"
    assert format_duration(185) == "3:05"
    assert format_duration(3729) == "1:02:09"


def test_the_activity_line_shows_the_step_and_the_whole_request(ai_tab, monkeypatch):
    import gui.ai_panel as panel

    clock = [1000.0]
    monkeypatch.setattr(panel.time, "monotonic", lambda: clock[0])
    ai_tab._busy = True
    ai_tab._started_at = 1000.0
    clock[0] = 1100.0
    ai_tab._begin_step("Running set_geometry_and_mesh")
    clock[0] = 1142.0
    ai_tab._refresh_activity()
    text = ai_tab.activity.text()
    assert "Running set_geometry_and_mesh" in text
    assert "step 42 s" in text and "total 2:22" in text


class _Record:
    def __init__(self, iteration, rho):
        self.iteration = iteration
        self._values = {"rms_rho": rho, "cd": 0.9, "cl": 0.0}

    def get(self, name):
        return self._values.get(name)


def test_the_chart_reports_speed_and_trend(qt_app):
    chart = ResidualChart()
    for i in range(1, 41):
        chart.add_record(_Record(i, -2.0 - 0.02 * i), now=i * 0.5)
    assert chart.iterations_per_second() == pytest.approx(2.0)
    assert chart.residual_trend() == pytest.approx(-2.0)
    status = chart.status.text()
    assert "2.00 it/s" in status and "converging" in status


def test_a_climbing_residual_is_called_diverging(qt_app):
    chart = ResidualChart()
    for i in range(1, 30):
        chart.add_record(_Record(i, -4.0 + 0.01 * i), now=float(i))
    assert "diverging" in chart.status.text()


def test_solves_started_by_the_assistant_reach_its_chart(ai_tab, qt_app):
    import mcp_server

    mcp_server._broadcast_iteration(_Record(1, -1.0))
    mcp_server._broadcast_iteration(_Record(2, -1.1))
    qt_app.processEvents()
    assert ai_tab.solve_chart.iteration_count == 2
    assert not ai_tab.solve_chart.isHidden()


def test_a_broken_listener_cannot_stop_a_solve():
    import mcp_server

    def broken(kind, payload):
        raise RuntimeError("gone")

    mcp_server.add_solver_listener(broken)
    mcp_server._broadcast_line("line")  # must not raise
    assert broken not in mcp_server._solver_listeners


# -- legends -------------------------------------------------------------------


def test_legend_text_grows_with_the_image():
    import backend.visualizer as visualizer

    visualizer._CURRENT_WINDOW = (1920, 1080)
    hd = visualizer._scalar_bar_arguments("Mach")
    visualizer._CURRENT_WINDOW = (3840, 2160)
    uhd = visualizer._scalar_bar_arguments("Mach")
    assert uhd["label_font_size"] == 2 * hd["label_font_size"]
    assert hd["label_font_size"] >= 20
    assert uhd["unconstrained_font_size"] is True
