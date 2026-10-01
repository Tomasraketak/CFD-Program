"""The in-application AI assistant tab.

An operator types what they want in ordinary language and the assistant
carries it out by calling the platform's own tools. The panel shows every tool
call as it happens, because an assistant that silently starts a half-hour
sweep is worse than no assistant at all.

The API key is entered here and handed straight to :mod:`core.credentials`,
which prefers the operating system's credential manager. The key is never
written into a project file and never shown after it has been saved — only a
masked form and a statement of where it is stored.
"""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from backend.ai_agent import (
    DEFAULT_MODEL,
    EVENT_LIMIT,
    EVENT_RETRY,
    EVENT_ROUND,
    EVENT_TEXT,
    EVENT_TOOL_FINISHED,
    EVENT_TOOL_STARTED,
    SUGGESTED_MODELS,
    AIAgentError,
    AuthenticationError,
    OpenRouterClient,
)
from core.credentials import (
    CredentialError,
    delete_api_key,
    describe_storage,
    load_api_key,
    mask_api_key,
    save_api_key,
)
from core.chat_history import ChatHistory, Conversation
from core.settings import AppSettings, load_settings, save_settings
from gui.charts import ResidualChart, format_duration
from gui.markdown_render import markdown_to_html
from gui.theme import ACCENT, DANGER, SUCCESS, TEXT_MUTED, WARNING

OPENROUTER_KEYS_URL = "https://openrouter.ai/keys"

# Link scheme for "show this image in the Graphics tab" in the transcript.
GRAPHICS_SCHEME = "ats-graphics"
# Link schemes for opening a stored run, or a saved project, from a
# conversation picked up again.
RUN_SCHEME = "ats-run"
PROJECT_SCHEME = "ats-project"
# A click on a picture in the transcript opens it full size in a viewer.
IMAGE_SCHEME = "ats-image"

# Width of a rendered image shown inline in the transcript, in pixels.
TRANSCRIPT_IMAGE_WIDTH = 420

# How long a worker thread waits for the operator to answer a confirmation
# dialog before giving up and declining. Long enough to fetch a coffee, short
# enough that a lost dialog cannot strand the thread forever.
APPROVAL_TIMEOUT_MS = 10 * 60 * 1000

# First entry in the model list. Choosing it reveals a box to type any id
# into; it is never itself a model id.
CUSTOM_MODEL_LABEL = "Custom…"

# Heading above the ids fetched from the operator's own OpenRouter account,
# so the shortlist and the full catalogue are visibly two different things.
FETCHED_MODELS_LABEL = "— all models on your account —"

# How often the live cost meter refreshes while a request runs. The figures
# themselves only change when a round completes; this is what keeps the
# elapsed time moving so the display does not look frozen.
METER_INTERVAL_MS = 500

# Example requests shown on an empty transcript, so a first-time user can see
# the shape of a useful instruction rather than facing a blank box.
EXAMPLE_PROMPTS = (
    "Jak přesný bude BMP580 na střeše tramvaje při 900 W/m² a rychlosti 3 m/s?",
    "Zkontroluj prostředí a řekni mi, jestli můžu spustit výpočet.",
    "Porovnej chybu senzoru pro bílou a černou krabičku.",
    "Vysíťuj naimportovanou raketu, rozlišení coarse.",
    "Spusť mi na naimportovaném modelu sweep přes Mach 0.3, 0.6 a 1.3.",
)


def _render_call(tool: str, arguments: dict[str, Any] | None) -> str:
    """Render a tool call the way an operator would write it.

    Long paths and long strings are clipped: the point is to recognise the
    call, not to reproduce it.
    """
    rendered = ", ".join(
        f"{key}={_clip(value)}" for key, value in (arguments or {}).items()
    )
    return f"{tool}({rendered})"


def _clip(value: Any, limit: int = 44) -> str:
    """Shorten one argument for display."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class AIWorker(QtCore.QRunnable):
    """Runs one assistant request off the GUI thread.

    Network calls and tool execution both block for a long time, so neither
    may happen on the Qt event loop.
    """

    class Signals(QtCore.QObject):
        """Signals emitted as the request proceeds."""

        progress = QtCore.Signal(str)
        event = QtCore.Signal(object)
        metrics = QtCore.Signal(object)
        finished = QtCore.Signal(object)
        failed = QtCore.Signal(str)
        approval = QtCore.Signal(str, object)

    def __init__(self, assistant: Any, prompt: str) -> None:
        super().__init__()
        self.assistant = assistant
        self.prompt = prompt
        self.signals = self.Signals()

    @QtCore.Slot()
    def run(self) -> None:
        """Execute the request and report the outcome."""
        try:
            self.assistant.on_progress = self.signals.progress.emit
            self.assistant.on_event = self.signals.event.emit
            self.assistant.on_metrics = self.signals.metrics.emit
            reply = self.assistant.ask(self.prompt)
            self.signals.finished.emit(reply)
        except AuthenticationError as error:
            self.signals.failed.emit(str(error))
        except AIAgentError as error:
            self.signals.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 - surfaced to the operator
            self.signals.failed.emit(f"{type(error).__name__}: {error}")


class AITab(QtWidgets.QWidget):
    """Chat with the assistant, plus its credentials and model settings."""

    statusMessage = QtCore.Signal(str)
    # The assistant rendered an image; carries its path.
    imageProduced = QtCore.Signal(str)
    # The operator clicked through to the Graphics tab from the transcript.
    showGraphics = QtCore.Signal(str)
    # Open a stored mesh or simulation, or a saved project, in the program.
    openRun = QtCore.Signal(str)
    openProject = QtCore.Signal(str)
    # Solver events from a tool call, crossing from the solver's thread.
    _solverEvent = QtCore.Signal(str, object)

    def __init__(
        self,
        settings: AppSettings,
        data_root: Any = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.data_root = data_root
        self.assistant: Any = None
        self.pool = QtCore.QThreadPool.globalInstance()
        self._busy = False
        self._metrics: Any = None
        self._started_at: float | None = None
        self._meter_timer = QtCore.QTimer(self)
        self._meter_timer.timeout.connect(self._refresh_meter)
        # What the assistant is doing right now, and since when -- a tool
        # call can run for many minutes and the clock must keep moving.
        self._step_text = ""
        self._step_started: float | None = None
        self.chat_history = ChatHistory(data_root)
        self.conversation = Conversation()
        self._pending_messages: list[dict[str, Any]] | None = None
        self._chart_active = False

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_settings_panel())
        splitter.addWidget(self._build_chat_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 940])

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(splitter)

        self.refresh_key_status()
        self._show_welcome()
        self.refresh_history()

        # Solves the assistant starts report their iterations here, for the
        # live chart. The listener runs on the solver thread; the signal
        # carries the event across to this one.
        self._solverEvent.connect(self._on_solver_event)
        try:
            import mcp_server

            mcp_server.add_solver_listener(self._solverEvent.emit)
        except Exception:  # pragma: no cover - MCP stack unavailable
            pass

    # -- construction ------------------------------------------------------

    def _build_settings_panel(self) -> QtWidgets.QWidget:
        """Credentials, model choice and safety options."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setSpacing(10)

        # --- credentials ---
        credentials = QtWidgets.QGroupBox("OpenRouter API key")
        form = QtWidgets.QFormLayout(credentials)

        self.key_input = QtWidgets.QLineEdit()
        self.key_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("sk-or-v1-…")
        self.key_input.returnPressed.connect(self.save_key)
        form.addRow("Key", self.key_input)

        buttons = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save key")
        save.setObjectName("primary")
        save.clicked.connect(self.save_key)
        clear = QtWidgets.QPushButton("Remove")
        clear.clicked.connect(self.clear_key)
        buttons.addWidget(save)
        buttons.addWidget(clear)
        form.addRow("", _wrap(buttons))

        self.key_status = QtWidgets.QLabel()
        self.key_status.setObjectName("hint")
        self.key_status.setWordWrap(True)
        form.addRow("", self.key_status)

        link = QtWidgets.QLabel(
            f'<a href="{OPENROUTER_KEYS_URL}" style="color:{ACCENT};">'
            "Get a key at openrouter.ai/keys</a>"
        )
        link.setOpenExternalLinks(True)
        link.setObjectName("hint")
        form.addRow("", link)
        layout.addWidget(credentials)

        # --- model ---
        model_box = QtWidgets.QGroupBox("Model")
        model_form = QtWidgets.QFormLayout(model_box)

        # A real drop-down, not an editable box. The editable version looked
        # exactly like a text field once the theme hid its arrow, and an
        # operator reasonably concluded the list did not exist. Any other id
        # goes in the Custom box, which only appears when Custom is chosen.
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setMaxVisibleItems(20)
        self.model_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._fill_model_combo(())
        self.model_combo.currentIndexChanged.connect(self._on_model_chosen)
        self.model_combo.setToolTip(
            "Pick a model, or choose Custom… and type any OpenRouter id. "
            "Fetch adds every model your account can reach."
        )
        model_form.addRow("Model", self.model_combo)

        self.custom_model = QtWidgets.QLineEdit()
        self.custom_model.setPlaceholderText(
            "provider/model-id, e.g. deepseek/deepseek-chat"
        )
        self.custom_model.setClearButtonEnabled(True)
        self._model_completer = QtWidgets.QCompleter([], self.custom_model)
        self._model_completer.setCaseSensitivity(
            QtCore.Qt.CaseSensitivity.CaseInsensitive
        )
        self._model_completer.setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
        self.custom_model.setCompleter(self._model_completer)
        self.custom_model_label = QtWidgets.QLabel("Custom id")
        model_form.addRow(self.custom_model_label, self.custom_model)

        self.set_model(self.settings.ai_model or DEFAULT_MODEL)

        fetch = QtWidgets.QPushButton("Fetch available models")
        fetch.clicked.connect(self.fetch_models)
        model_form.addRow("", fetch)

        self.model_status = QtWidgets.QLabel()
        self.model_status.setObjectName("hint")
        self.model_status.setWordWrap(True)
        model_form.addRow("", self.model_status)
        layout.addWidget(model_box)

        # --- safety ---
        safety = QtWidgets.QGroupBox("Safety")
        safety_layout = QtWidgets.QVBoxLayout(safety)

        self.confirm_long = QtWidgets.QCheckBox(
            "Ask before meshing, solving or sweeping"
        )
        self.confirm_long.setChecked(self.settings.ai_confirm_long_tools)
        self.confirm_long.setToolTip(
            "These take minutes to hours. With this off, the assistant starts "
            "them without asking."
        )
        safety_layout.addWidget(self.confirm_long)

        rounds = QtWidgets.QHBoxLayout()
        rounds_label = QtWidgets.QLabel("Max tool rounds")
        rounds_label.setObjectName("hint")
        self.max_rounds = QtWidgets.QSpinBox()
        self.max_rounds.setRange(1, 50)
        self.max_rounds.setValue(self.settings.ai_max_tool_rounds)
        self.max_rounds.setToolTip(
            "Caps what one request can spend if the model gets confused."
        )
        rounds.addWidget(rounds_label)
        rounds.addWidget(self.max_rounds)
        rounds.addStretch(1)
        safety_layout.addLayout(rounds)

        iterations = QtWidgets.QHBoxLayout()
        iterations_label = QtWidgets.QLabel("Iteration limit")
        iterations_label.setObjectName("hint")
        self.iteration_limit = QtWidgets.QSpinBox()
        self.iteration_limit.setRange(100, 100_000)
        self.iteration_limit.setSingleStep(250)
        self.iteration_limit.setValue(self.settings.solver_max_iterations)
        self.iteration_limit.setToolTip(
            "Solves the assistant runs stop here at the latest, and the result "
            "is taken as final. The assistant can change it too."
        )
        # Saved at once: the solver reads it when a solve starts.
        self.iteration_limit.valueChanged.connect(self._persist_settings)
        iterations.addWidget(iterations_label)
        iterations.addWidget(self.iteration_limit)
        iterations.addStretch(1)
        safety_layout.addLayout(iterations)

        note = QtWidgets.QLabel(
            "The assistant can only use this program's own simulation tools. "
            "It cannot run shell commands or read arbitrary files.\n\n"
            "Your requests and the simulation parameters in them are sent to "
            "OpenRouter and the model provider you choose."
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        safety_layout.addWidget(note)
        layout.addWidget(safety)

        layout.addStretch(1)
        return panel

    def _build_chat_panel(self) -> QtWidgets.QWidget:
        """Transcript, input box and controls."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setSpacing(6)

        history_row = QtWidgets.QHBoxLayout()
        history_label = QtWidgets.QLabel("Conversation")
        history_label.setObjectName("hint")
        self.history_combo = QtWidgets.QComboBox()
        self.history_combo.setToolTip(
            "Earlier conversations, newest first. Picking one brings back the "
            "transcript and what the assistant knew, so you can carry on -- "
            "with the same meshes and simulations. Stored on this computer only."
        )
        self.history_combo.activated.connect(self._on_history_chosen)
        self.delete_chat_button = QtWidgets.QPushButton("Delete")
        self.delete_chat_button.setToolTip("Delete the selected saved conversation")
        self.delete_chat_button.clicked.connect(self.delete_conversation)
        history_row.addWidget(history_label)
        history_row.addWidget(self.history_combo, 1)
        history_row.addWidget(self.delete_chat_button)
        layout.addLayout(history_row)

        self.transcript = QtWidgets.QTextBrowser()
        # Links are handled here rather than by the browser, which would
        # try to navigate to them: an image link opens the Graphics tab.
        self.transcript.setOpenLinks(False)
        self.transcript.anchorClicked.connect(self._on_link)
        layout.addWidget(self.transcript, 1)

        # The live convergence chart, shown while a solve the assistant
        # started is running.
        self.solve_chart = ResidualChart()
        self.solve_chart.setMinimumHeight(170)
        self.solve_chart.setMaximumHeight(240)
        self.solve_chart.setVisible(False)
        self._chart_active = False
        layout.addWidget(self.solve_chart)

        status_row = QtWidgets.QHBoxLayout()
        self.activity = QtWidgets.QLabel()
        self.activity.setObjectName("hint")
        status_row.addWidget(self.activity, 1)

        # Cost and throughput, updated while the request runs rather than
        # only at the end: a model that is slow or expensive should be
        # visible in time to stop it.
        self.meter = QtWidgets.QLabel()
        self.meter.setObjectName("hint")
        self.meter.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.meter.setToolTip(
            "Tokens used, generation rate and cost for the request running "
            "now. Cost is OpenRouter's own figure; some providers do not "
            "report one, and then it is left blank rather than guessed."
        )
        status_row.addWidget(self.meter)
        layout.addLayout(status_row)

        self.input = QtWidgets.QPlainTextEdit()
        self.input.setPlaceholderText(
            "Ask for what you want — for example: "
            "„O kolik přestřelí senzor při 900 W/m²?\"   (Ctrl+Enter to send)"
        )
        self.input.setMaximumHeight(96)
        layout.addWidget(self.input)

        controls = QtWidgets.QHBoxLayout()
        self.send_button = QtWidgets.QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.send)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setToolTip(
            "Cancel the running solve and stop the assistant after the "
            "current step."
        )
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.new_chat_button = QtWidgets.QPushButton("New conversation")
        self.new_chat_button.clicked.connect(self.new_conversation)
        controls.addWidget(self.send_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.new_chat_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        send_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence("Ctrl+Return"), self.input
        )
        send_shortcut.activated.connect(self.send)

        return panel

    # -- credentials -------------------------------------------------------

    def refresh_key_status(self) -> None:
        """Show whether a key is stored, and how securely."""
        report = describe_storage("openrouter", self.data_root)
        key = load_api_key("openrouter", self.data_root)

        if report.backend == "none":
            self.key_status.setText("No key stored — the assistant is disabled.")
            self.key_status.setStyleSheet(f"color: {WARNING};")
            return

        colour = SUCCESS if report.secure else WARNING
        self.key_status.setText(f"{mask_api_key(key)} — {report.detail}")
        self.key_status.setStyleSheet(f"color: {colour};")

    def save_key(self) -> None:
        """Store the entered key, then clear the input box."""
        entered = self.key_input.text().strip()
        if not entered:
            self._warn("Paste your OpenRouter API key first.")
            return
        try:
            report = save_api_key(entered, "openrouter", self.data_root)
        except CredentialError as error:
            self._warn(str(error))
            return

        # Never leave the key visible in the widget once it is stored.
        self.key_input.clear()
        self.assistant = None
        self.refresh_key_status()

        if not report.secure:
            QtWidgets.QMessageBox.information(
                self, "API key stored", report.detail
            )
        self.statusMessage.emit("OpenRouter key saved")

    def clear_key(self) -> None:
        """Forget the stored key."""
        if delete_api_key("openrouter", self.data_root):
            self.statusMessage.emit("OpenRouter key removed")
        self.assistant = None
        self.refresh_key_status()

    # -- model -------------------------------------------------------------

    def fetch_models(self) -> None:
        """Load the live model catalogue from OpenRouter."""
        key = load_api_key("openrouter", self.data_root)
        if not key:
            self._warn("Save an API key before fetching the model list.")
            return

        self.model_status.setText("Fetching …")
        QtWidgets.QApplication.processEvents()
        try:
            models = OpenRouterClient(key).list_models()
        except AIAgentError as error:
            self.model_status.setText(str(error))
            self.model_status.setStyleSheet(f"color: {DANGER};")
            return

        current = self.selected_model()
        fetched = sorted(
            {str(entry["id"]) for entry in models if entry.get("id")}
        )
        self._fill_model_combo(fetched)
        self._model_completer.setModel(
            QtCore.QStringListModel(fetched, self._model_completer)
        )
        self.set_model(current)
        self.model_status.setText(f"{len(fetched)} models available.")
        self.model_status.setStyleSheet(f"color: {TEXT_MUTED};")

    def _fill_model_combo(self, fetched: Any) -> None:
        """Rebuild the list: Custom, the shortlist, then anything fetched.

        The shortlist stays on top after a fetch. Sorting three hundred ids
        alphabetically would bury the handful the operator actually chose.
        """
        blocked = self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            self.model_combo.addItem(CUSTOM_MODEL_LABEL, None)
            self.model_combo.insertSeparator(self.model_combo.count())
            for model in SUGGESTED_MODELS:
                self.model_combo.addItem(model, model)
            extra = [model for model in fetched if model not in SUGGESTED_MODELS]
            if extra:
                self.model_combo.insertSeparator(self.model_combo.count())
                self.model_combo.addItem(FETCHED_MODELS_LABEL, None)
                heading = self.model_combo.model().item(self.model_combo.count() - 1)
                if heading is not None:
                    heading.setEnabled(False)
                for model in extra:
                    self.model_combo.addItem(model, model)
        finally:
            self.model_combo.blockSignals(blocked)

    # -- conversation ------------------------------------------------------

    def _show_welcome(self) -> None:
        """Explain what the assistant can do, with example requests."""
        examples = "".join(
            f"<li style='margin-bottom:4px;'>{html.escape(prompt)}</li>"
            for prompt in EXAMPLE_PROMPTS
        )
        self.transcript.setHtml(
            f"""
            <div style="color:{TEXT_MUTED};">
              <p><b style="color:{ACCENT};">AI assistant</b></p>
              <p>Ask for what you want and I will use this program's
                 simulation tools to do it — meshing, aerodynamic and thermal
                 simulation, sweeps, rendering, projects and settings.</p>
              <p>For example:</p>
              <ul>{examples}</ul>
              <p>A STEP file opened in the Rocket Aerodynamics tab is
                 already known to me, so you can say "mesh the model I just
                 imported" without typing its path.</p>
              <p>Images I draw appear right here and in the Graphics tab,
                 where you can view them full size and export them.</p>
              <p>I cannot run shell commands or read files outside the
                 program's own tools.</p>
            </div>
            """
        )

    def new_conversation(self) -> None:
        """Forget the conversation so far, and the cost of it."""
        self._stop_meter()
        self._metrics = None
        self.meter.clear()
        if self.assistant is not None:
            self.assistant.reset()
        self._pending_messages = None
        self.conversation = Conversation()
        self.solve_chart.clear()
        self.solve_chart.setVisible(False)
        self._chart_active = False
        self._show_welcome()
        self.activity.clear()
        self.refresh_history()
        self.statusMessage.emit("Started a new conversation")

    def _ensure_assistant(self) -> bool:
        """Build the assistant if needed, reporting any problem."""
        if self.assistant is not None:
            self._apply_settings_to_assistant()
            if self._pending_messages is not None:
                self.assistant.messages = (
                    self.assistant.messages[:1] + self._pending_messages
                )
                self._pending_messages = None
            return True

        from backend.ai_agent import create_assistant

        try:
            self.assistant = create_assistant(
                model=self.selected_model(),
                approve=self._approve_tool,
                retry_attempts=self.settings.ai_retry_attempts,
            )
        except AuthenticationError as error:
            self._append_error(str(error))
            return False
        except AIAgentError as error:
            self._append_error(str(error))
            return False

        self._apply_settings_to_assistant()
        if self._pending_messages is not None:
            self.assistant.messages = self.assistant.messages[:1] + self._pending_messages
            self._pending_messages = None
        return True

    def selected_model(self) -> str:
        """The chosen model id; the Custom box when Custom is selected."""
        chosen = self.model_combo.currentData()
        if chosen:
            return str(chosen)
        typed = self.custom_model.text().strip()
        return typed or DEFAULT_MODEL

    def set_model(self, model_id: str) -> None:
        """Select a model by id, falling back to Custom for an unlisted one."""
        model_id = (model_id or "").strip()
        index = self.model_combo.findData(model_id) if model_id else -1
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        else:
            self.model_combo.setCurrentIndex(0)
            self.custom_model.setText(model_id)
        self._show_custom_box()

    def _on_model_chosen(self, _index: int) -> None:
        """Show the Custom box only when Custom is the choice."""
        self._show_custom_box()
        if self.model_combo.currentData() is None and self.isVisible():
            self.custom_model.setFocus()

    def _show_custom_box(self) -> None:
        """Hide or reveal the Custom id box to match the selection."""
        custom = self.model_combo.currentData() is None
        self.custom_model.setVisible(custom)
        self.custom_model_label.setVisible(custom)

    def _apply_settings_to_assistant(self) -> None:
        """Push the current interface settings onto the assistant."""
        if self.assistant is None:
            return
        self.assistant.model = self.selected_model()
        self.assistant.max_rounds = self.max_rounds.value()
        self.assistant.approve = (
            self._approve_tool if self.confirm_long.isChecked() else None
        )

    def _reload_iteration_limit(self) -> None:
        """Show a limit the assistant set through update_settings."""
        try:
            stored = load_settings(self.data_root, refresh=True).solver_max_iterations
        except Exception:  # noqa: BLE001 - keep what is on screen
            return
        self.settings.solver_max_iterations = stored
        blocked = self.iteration_limit.blockSignals(True)
        self.iteration_limit.setValue(stored)
        self.iteration_limit.blockSignals(blocked)

    def _persist_settings(self) -> None:
        """Remember the model and safety choices between sessions."""
        self.settings.ai_model = self.selected_model()
        self.settings.ai_confirm_long_tools = self.confirm_long.isChecked()
        self.settings.ai_max_tool_rounds = self.max_rounds.value()
        if hasattr(self, "iteration_limit"):
            # The assistant may have changed it through update_settings.
            self.settings.solver_max_iterations = self.iteration_limit.value()
        save_settings(self.settings, self.data_root)

    def _approve_tool(self, name: str, arguments: dict[str, Any]) -> bool:
        """Ask the operator before a long-running tool runs.

        Called from the worker thread, so the dialog is marshalled onto the
        GUI thread and waited on.
        """
        estimate = {
            "set_geometry_and_mesh": "typically 30 s to 3 minutes",
            "run_aerodynamic_simulation": "typically 3 to 8 minutes",
            "run_parametric_sweep": "3 to 8 minutes per point",
        }.get(name, "a while")

        rendered = "\n".join(
            f"    {key} = {value}" for key, value in sorted(arguments.items())
        )
        question = (
            f"The assistant wants to run:\n\n  {name}\n{rendered}\n\n"
            f"This takes {estimate}. Run it?"
        )

        answer = _ask_on_gui_thread(self, "AI assistant", question)
        return answer

    def send(self) -> None:
        """Send the typed request to the assistant."""
        prompt = self.input.toPlainText().strip()
        if not prompt:
            return
        if self._busy:
            self._warn("The assistant is still working on the previous request.")
            return
        if not self._ensure_assistant():
            return

        self._persist_settings()
        self.input.clear()
        self._append_user(prompt)
        self.solve_chart.setVisible(False)
        self._chart_active = False
        self._set_busy(True)

        worker = AIWorker(self.assistant, prompt)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.event.connect(self._on_event)
        worker.signals.metrics.connect(self._on_metrics)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        self._start_meter()
        self.pool.start(worker)

    def stop(self) -> None:
        """Cancel the running solve and stop the assistant.

        The panel is not unlocked here: the worker is still unwinding, and
        it unlocks itself when it reports back. Pressing Stop twice does no
        harm.
        """
        if not self._busy or self.assistant is None:
            return
        self._append_note("Stopping — cancelling any running solver …")
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping …")
        self.assistant.request_stop()

    def _set_busy(self, busy: bool) -> None:
        """Enable or disable input while a request is in flight."""
        self._busy = busy
        self.send_button.setEnabled(not busy)
        self.send_button.setText("Working …" if busy else "Send")
        self.stop_button.setEnabled(busy)
        self.stop_button.setText("Stop")
        self.input.setReadOnly(busy)

    def _start_meter(self) -> None:
        """Begin timing a request and show a live counter."""
        self._metrics = None
        self._started_at = time.monotonic()
        self.meter.setText("0 tokens · 0s")
        self._meter_timer.start(METER_INTERVAL_MS)

    def _on_metrics(self, metrics: Any) -> None:
        """Take the running totals from the worker thread."""
        self._metrics = metrics
        self._refresh_meter()

    def _begin_step(self, text: str) -> None:
        """Start timing one step of the request."""
        self._step_text = text
        self._step_started = time.monotonic()
        self._refresh_activity()

    def _refresh_activity(self) -> None:
        """'Running X — step 42 s · total 3:05', kept ticking by the timer."""
        if not self._busy or self._started_at is None:
            return
        now = time.monotonic()
        total = format_duration(now - self._started_at)
        if self._step_text and self._step_started is not None:
            step = format_duration(now - self._step_started)
            self.activity.setText(f"{self._step_text} — step {step} · total {total}")
        else:
            self.activity.setText(f"Working — total {total}")

    def _refresh_meter(self) -> None:
        """Redraw the meter, advancing the clock between rounds.

        The token and cost figures only move when a round completes, so
        without this the display would sit still through a four-minute mesh
        and look broken.
        """
        self._refresh_activity()
        elapsed = (
            0.0 if self._started_at is None else time.monotonic() - self._started_at
        )
        if self._metrics is None:
            self.meter.setText(f"0 tokens · {elapsed:.0f}s")
            return
        self._metrics.elapsed_seconds = elapsed
        self.meter.setText(self._metrics.summary())

    def _stop_meter(self) -> None:
        """Freeze the meter on the final figures."""
        self._meter_timer.stop()
        self._refresh_meter()
        self._started_at = None

    def _on_progress(self, message: str) -> None:
        """Pass progress on to the status bar; the activity line has its clock."""
        self.statusMessage.emit(message)

    def _on_event(self, event: Any) -> None:
        """Write one step into the transcript as it happens.

        The transcript used to stay empty until the whole request finished,
        which on a seventeen-minute request meant seventeen minutes of a
        blank panel and one status line being overwritten. Everything the
        assistant does now lands here while it is happening, and survives
        as scrollback afterwards.
        """
        kind = getattr(event, "kind", "")
        if kind == EVENT_ROUND:
            self._begin_step(f"Waiting for {self.selected_model()}")
            self._append(
                f'<p style="margin-top:10px;color:{TEXT_MUTED};font-size:11px;">'
                f"— round {event.round} —</p>"
            )
        elif kind == EVENT_TEXT:
            # The model's own account of what it is about to do.
            self._append(
                f'<p style="margin-top:6px;color:{TEXT_MUTED};"><i>'
                f'{html.escape(event.message).replace(chr(10), "<br>")}</i></p>'
            )
        elif kind == EVENT_TOOL_STARTED:
            self._begin_step(f"Running {event.tool}")
            self._append(
                f'<p style="margin-top:6px;color:{ACCENT};">▸ '
                f"{html.escape(_render_call(event.tool, event.arguments))}</p>"
            )
        elif kind == EVENT_TOOL_FINISHED:
            colour = DANGER if event.failed else TEXT_MUTED
            detail = (
                f"failed: {event.message}"
                if event.failed
                else f"ok — {event.duration_s:.1f} s"
            )
            self._append(
                f'<p style="margin:0 0 0 14px;color:{colour};">'
                f"{html.escape(detail)}</p>"
            )
            self._begin_step(f"Waiting for {self.selected_model()}")
            if not event.failed:
                for image in _image_paths(getattr(event, "result", None)):
                    self._append_image(image)
        elif kind == EVENT_RETRY:
            self._append_note(event.message)
        elif kind == EVENT_LIMIT:
            self._append_note(event.message)

    def _on_finished(self, reply: Any) -> None:
        """Render the completed reply."""
        self._set_busy(False)
        self._reload_iteration_limit()
        self.activity.clear()
        if getattr(reply, "metrics", None) is not None:
            self._metrics = reply.metrics
        self._stop_meter()

        # The tool calls were written as they happened; listing them again
        # here would show the whole run twice.
        self._append_assistant(reply.text or "(no answer)")
        self._step_text = ""

        if reply.stopped_early:
            self._append_note(
                "Stopped early — either a step was declined or the tool-round "
                "limit was reached."
            )
        tokens = reply.usage.get("total_tokens")
        if tokens:
            self.statusMessage.emit(f"Done — {int(tokens)} tokens used")
        self.save_conversation()

    def _on_failed(self, message: str) -> None:
        """Report a failed request."""
        self._set_busy(False)
        self.activity.clear()
        self._step_text = ""
        self._stop_meter()
        self._append_error(message)
        self.save_conversation()

    # -- saved conversations -------------------------------------------------

    def refresh_history(self) -> None:
        """Fill the conversation list: the current one, then saved ones."""
        blocked = self.history_combo.blockSignals(True)
        try:
            self.history_combo.clear()
            self.history_combo.addItem("Current conversation", None)
            for saved in self.chat_history.list():
                if saved.chat_id == self.conversation.chat_id:
                    continue
                stamp = saved.updated_at.replace("T", " ")[:16]
                self.history_combo.addItem(f"{stamp}  {saved.title}", saved.chat_id)
            self.history_combo.setCurrentIndex(0)
        finally:
            self.history_combo.blockSignals(blocked)

    def save_conversation(self) -> None:
        """Store the conversation so far; nothing is saved before a first reply."""
        messages = (
            self.assistant.history()
            if self.assistant is not None
            else list(self._pending_messages or [])
        )
        if not messages:
            return
        self.conversation.messages = [dict(message) for message in messages]
        self.conversation.transcript_html = self.transcript.toHtml()
        self.conversation.model = self.selected_model()
        try:
            self.chat_history.save(self.conversation)
        except OSError as error:
            self._append_note(f"Could not save this conversation: {error}")
            return
        self.refresh_history()

    def _on_history_chosen(self, index: int) -> None:
        chat_id = self.history_combo.itemData(index)
        if chat_id:
            self.open_conversation(chat_id)

    def open_conversation(self, chat_id: str) -> None:
        """Bring a saved conversation back, ready to continue."""
        if self._busy:
            self._warn("Wait for the current request to finish, or stop it.")
            self.refresh_history()
            return
        self.save_conversation()
        try:
            conversation = self.chat_history.load(chat_id)
        except (OSError, ValueError) as error:
            self._warn(f"That conversation could not be read: {error}")
            return
        self.conversation = conversation
        if self.assistant is not None:
            self.assistant.messages = self.assistant.messages[:1] + list(
                conversation.messages
            )
            self._pending_messages = None
        else:
            self._pending_messages = list(conversation.messages)
        self._metrics = None
        self.meter.clear()
        self.solve_chart.setVisible(False)
        self._chart_active = False
        self.transcript.setHtml(conversation.transcript_html)
        self._append_related_work(conversation)
        self.refresh_history()
        self.statusMessage.emit(f"Continuing: {conversation.title}")

    def _append_related_work(self, conversation: Conversation) -> None:
        """List what the conversation worked on, each one openable."""
        runs = conversation.run_ids()
        projects = conversation.project_paths()
        if not runs and not projects:
            self._append_note("Conversation restored. Carry on where you left off.")
            return
        items = []
        for run in runs:
            target = html.escape(QtCore.QUrl(f"{RUN_SCHEME}:{run}").toString())
            items.append(f'<li><a href="{target}" style="color:{ACCENT};">{html.escape(run)}</a></li>')
        for path in projects:
            target = html.escape(QtCore.QUrl(f"{PROJECT_SCHEME}:{path}").toString())
            items.append(
                f'<li><a href="{target}" style="color:{ACCENT};">'
                f"{html.escape(Path(path).name)}</a> (project)</li>"
            )
        self._append(
            f'<p style="margin-top:12px;color:{WARNING};"><i>Conversation '
            "restored. The assistant remembers it and these results -- click "
            "one to open it in the program, or just carry on asking.</i></p>"
            f"<ul>{''.join(items)}</ul>"
        )

    def delete_conversation(self) -> None:
        """Delete the saved conversation picked in the list."""
        chat_id = self.history_combo.currentData()
        if not chat_id:
            self._warn("Pick a saved conversation in the list first.")
            return
        self.chat_history.delete(chat_id)
        self.refresh_history()
        self.statusMessage.emit("Conversation deleted")

    # -- live solver -------------------------------------------------------

    def _on_solver_event(self, kind: str, payload: Any) -> None:
        """Plot a solve the assistant is running, as it runs."""
        if kind == "iteration":
            if not self._chart_active:
                self.solve_chart.clear()
                self.solve_chart.setVisible(True)
                self._chart_active = True
            self.solve_chart.add_record(payload)
            if self._busy:
                self._step_text = (
                    f"Solving — {self.solve_chart.status.text()}"
                )
        elif kind == "line":
            from backend.aero_solver import STAGE_MARKER_PREFIX

            if str(payload).startswith(STAGE_MARKER_PREFIX):
                self.solve_chart.begin_stage()
        elif kind == "finished":
            # The iterations are over; the chart has served its purpose and
            # the answer below it is what matters now.
            self.solve_chart.setVisible(False)
            self._chart_active = False

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """Stop listening to the solver and keep the conversation."""
        try:
            import mcp_server

            mcp_server.remove_solver_listener(self._solverEvent.emit)
        except Exception:  # pragma: no cover
            pass
        self.save_conversation()
        super().closeEvent(event)

    # -- transcript --------------------------------------------------------

    def _append(self, markup: str) -> None:
        """Append markup and scroll to the bottom."""
        self.transcript.append(markup)
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_user(self, text: str) -> None:
        """Add the operator's message."""
        self._append(
            f'<p style="margin-top:12px;"><b style="color:{ACCENT};">You</b><br>'
            f'{html.escape(text).replace(chr(10), "<br>")}</p>'
        )

    def _append_assistant(self, text: str) -> None:
        """Add the assistant's reply."""
        # Rendered from Markdown, so a table of results is a table.
        self._append(
            f'<p style="margin-top:8px;"><b style="color:{SUCCESS};">Assistant</b></p>'
            f"{markdown_to_html(text)}"
        )

    def _append_tool_calls(self, calls: list[Any]) -> None:
        """List the tools that ran, so nothing happens invisibly."""
        rows = "".join(
            f"<li style='color:{DANGER if (call.error or call.declined) else TEXT_MUTED};'>"
            f"{html.escape(call.summary())}</li>"
            for call in calls
        )
        self._append(
            f'<p style="margin-top:8px;"><span style="color:{TEXT_MUTED};">'
            f"Tools used</span><ul>{rows}</ul></p>"
        )

    def _append_image(self, path: Path) -> None:
        """Show a rendered image in the transcript, and send it to Graphics.

        The picture itself, not only its path: a path at the end of a long
        reply is how an operator came to have "no idea where the graphics
        are".
        """
        url = QtCore.QUrl.fromLocalFile(str(path)).toString()
        target = QtCore.QUrl(GRAPHICS_SCHEME + ":" + str(path)).toString()
        viewer = QtCore.QUrl(IMAGE_SCHEME + ":" + str(path)).toString()
        self._append(
            f'<p style="margin:4px 0 0 14px;">'
            f'<a href="{html.escape(viewer)}"><img src="{html.escape(url)}" '
            f'width="{TRANSCRIPT_IMAGE_WIDTH}"></a><br>'
            f'<a href="{html.escape(viewer)}" style="color:{ACCENT};">'
            f"Enlarge</a>"
            f'<span style="color:{TEXT_MUTED};"> (or click the picture) · </span>'
            f'<a href="{html.escape(target)}" style="color:{ACCENT};">'
            f"Open in the Graphics tab</a>"
            f"</p>"
        )
        self.imageProduced.emit(str(path))

    def _on_link(self, url: QtCore.QUrl) -> None:
        """Follow a link clicked in the transcript."""
        if url.scheme() == IMAGE_SCHEME:
            self.open_image(url.path())
            return
        if url.scheme() == GRAPHICS_SCHEME:
            self.showGraphics.emit(url.path())
            return
        if url.scheme() == RUN_SCHEME:
            self.openRun.emit(url.path())
            return
        if url.scheme() == PROJECT_SCHEME:
            self.openProject.emit(url.path())
            return
        QtGui.QDesktopServices.openUrl(url)

    def open_image(self, path: str | Path) -> "ImageViewer | None":
        """Show a transcript picture full size in its own window."""
        path = Path(path)
        if not path.is_file():
            self._warn(f"The picture is no longer there:\n{path}")
            return None
        viewer = ImageViewer(path, self)
        self._viewers = [v for v in getattr(self, "_viewers", []) if v.isVisible()]
        self._viewers.append(viewer)
        viewer.show()
        return viewer

    def _append_note(self, text: str) -> None:
        """Add an italic note."""
        self._append(
            f'<p style="color:{WARNING};"><i>{html.escape(text)}</i></p>'
        )

    def _append_error(self, text: str) -> None:
        """Add an error block."""
        self._append(
            f'<p style="margin-top:8px;"><b style="color:{DANGER};">Error</b><br>'
            f'<span style="color:{DANGER};">'
            f'{html.escape(text).replace(chr(10), "<br>")}</span></p>'
        )

    def _warn(self, message: str) -> None:
        """Show a modal warning."""
        QtWidgets.QMessageBox.warning(self, "AI assistant", message)


IMAGE_KEYS = ("image_path", "geometry_image_path")


def _image_paths(result: Any) -> list[Path]:
    """The images a tool reply points at (a chart, a geometry picture) that exist."""
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    found = []
    for key in IMAGE_KEYS:
        path = result.get(key)
        if path and Path(str(path)).is_file():
            found.append(Path(str(path)))
    return found



def _ask_on_gui_thread(parent: QtWidgets.QWidget, title: str, text: str) -> bool:
    """Show a yes/no dialog, whichever thread the caller is on.

    Qt dialogs may only be created on the GUI thread, so a call from the
    worker is marshalled across and waited for.
    """
    application = QtWidgets.QApplication.instance()
    if application is None:  # pragma: no cover - no Qt application in tests
        return True

    if QtCore.QThread.currentThread() == application.thread():
        return _show_question(parent, title, text)

    holder: dict[str, bool] = {}
    loop_done = QtCore.QSemaphore(0)

    def prompt() -> None:
        holder["answer"] = _show_question(parent, title, text)
        loop_done.release()

    # Queue the dialog onto the GUI thread and block this worker thread until
    # the operator answers. The context-object overload is required: a plain
    # singleShot would start the timer in *this* thread, which has no event
    # loop, and the dialog would never appear.
    QtCore.QTimer.singleShot(0, parent, prompt)
    # Waiting without a bound would strand this worker thread for the life of
    # the process if the dialog never arrived. Declining after the wait is the
    # safe default: nothing expensive starts unasked.
    if not loop_done.tryAcquire(1, APPROVAL_TIMEOUT_MS):
        return False
    return holder.get("answer", False)


def _show_question(parent: QtWidgets.QWidget, title: str, text: str) -> bool:
    """Display the confirmation dialog and return the answer."""
    answer = QtWidgets.QMessageBox.question(
        parent,
        title,
        text,
        QtWidgets.QMessageBox.StandardButton.Yes
        | QtWidgets.QMessageBox.StandardButton.No,
        QtWidgets.QMessageBox.StandardButton.Yes,
    )
    return answer == QtWidgets.QMessageBox.StandardButton.Yes


def _wrap(layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
    """Wrap a layout in a widget so it can go into a form row."""
    container = QtWidgets.QWidget()
    container.setLayout(layout)
    return container


class ImageViewer(QtWidgets.QDialog):
    """A picture from the transcript, full size: zoom, fit, save, open.

    Mouse wheel (or + / -) zooms, 0 fits the window, 1 is actual size; drag
    to pan when zoomed in.
    """

    def __init__(self, path: Path, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(self.path.name)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.pixmap = QtGui.QPixmap(str(self.path))
        self.zoom = 1.0
        self._fit = True

        self.label = QtWidgets.QLabel()
        self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidget(self.label)
        self.scroll.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.scroll.viewport().installEventFilter(self)
        self._drag: QtCore.QPoint | None = None

        bar = QtWidgets.QHBoxLayout()
        for text, slot in (
            ("Fit", self.fit), ("100 %", self.actual_size),
            ("−", lambda: self.set_zoom(self.zoom / 1.25)),
            ("+", lambda: self.set_zoom(self.zoom * 1.25)),
            ("Save as…", self.save_as), ("Open externally", self.open_externally),
        ):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addStretch(1)
        self.zoom_label = QtWidgets.QLabel()
        bar.addWidget(self.zoom_label)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.scroll, 1)
        screen = QtGui.QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1600, 1000)
        self.resize(
            min(self.pixmap.width() + 40, int(available.width() * 0.9)),
            min(self.pixmap.height() + 80, int(available.height() * 0.9)),
        )
        for key, slot in (("+", lambda: self.set_zoom(self.zoom * 1.25)),
                          ("=", lambda: self.set_zoom(self.zoom * 1.25)),
                          ("-", lambda: self.set_zoom(self.zoom / 1.25)),
                          ("0", self.fit), ("1", self.actual_size)):
            QtGui.QShortcut(QtGui.QKeySequence(key), self, activated=slot)
        QtCore.QTimer.singleShot(0, self.fit)

    def fit(self) -> None:
        if self.pixmap.isNull():
            return
        area = self.scroll.viewport().size()
        scale = min(area.width() / self.pixmap.width(), area.height() / self.pixmap.height())
        self._fit = True
        self._apply(max(0.05, min(scale, 1.0)))

    def actual_size(self) -> None:
        self._fit = False
        self._apply(1.0)

    def set_zoom(self, zoom: float) -> None:
        self._fit = False
        self._apply(max(0.05, min(zoom, 8.0)))

    def _apply(self, zoom: float) -> None:
        self.zoom = zoom
        size = self.pixmap.size() * zoom
        self.label.setPixmap(self.pixmap.scaled(
            size, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        ))
        self.label.resize(self.label.pixmap().size())
        self.zoom_label.setText(f"{zoom * 100:.0f} %")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._fit:
            self.fit()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        kind = event.type()
        if kind == QtCore.QEvent.Type.Wheel:
            step = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.set_zoom(self.zoom * step)
            return True
        if kind == QtCore.QEvent.Type.MouseButtonPress:
            self._drag = event.position().toPoint()
        elif kind == QtCore.QEvent.Type.MouseMove and self._drag is not None:
            delta = event.position().toPoint() - self._drag
            self._drag = event.position().toPoint()
            for bar, d in ((self.scroll.horizontalScrollBar(), delta.x()),
                           (self.scroll.verticalScrollBar(), delta.y())):
                bar.setValue(bar.value() - d)
        elif kind == QtCore.QEvent.Type.MouseButtonRelease:
            self._drag = None
        elif kind == QtCore.QEvent.Type.MouseButtonDblClick:
            self.actual_size() if self._fit else self.fit()
            return True
        return super().eventFilter(watched, event)

    def save_as(self) -> None:
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save picture", str(Path.home() / self.path.name), "PNG image (*.png)"
        )
        if target:
            import shutil

            shutil.copyfile(self.path, target)

    def open_externally(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.path)))
