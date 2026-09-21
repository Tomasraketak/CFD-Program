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
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from backend.ai_agent import (
    DEFAULT_MODEL,
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
from core.settings import AppSettings, save_settings
from gui.theme import ACCENT, DANGER, SUCCESS, TEXT_MUTED, WARNING

OPENROUTER_KEYS_URL = "https://openrouter.ai/keys"

# How long a worker thread waits for the operator to answer a confirmation
# dialog before giving up and declining. Long enough to fetch a coffee, short
# enough that a lost dialog cannot strand the thread forever.
APPROVAL_TIMEOUT_MS = 10 * 60 * 1000

# First entry in the model list. Choosing it clears the box so an id can be
# typed; it is never itself a model id.
CUSTOM_MODEL_LABEL = "Custom — type an id below"

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


class AIWorker(QtCore.QRunnable):
    """Runs one assistant request off the GUI thread.

    Network calls and tool execution both block for a long time, so neither
    may happen on the Qt event loop.
    """

    class Signals(QtCore.QObject):
        """Signals emitted as the request proceeds."""

        progress = QtCore.Signal(str)
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

        self.model_combo = QtWidgets.QComboBox()
        # Editable so any id can be typed: the shortlist is a convenience,
        # not a restriction, and OpenRouter's catalogue outruns any list
        # compiled here.
        self.model_combo.setEditable(True)
        self.model_combo.addItem(CUSTOM_MODEL_LABEL)
        self.model_combo.insertSeparator(1)
        self.model_combo.addItems(SUGGESTED_MODELS)
        self.model_combo.setCurrentText(self.settings.ai_model or DEFAULT_MODEL)
        # 'activated' rather than 'currentIndexChanged': the Custom entry is
        # index 0 and an editable box can already be sitting on it, in which
        # case choosing it again changes no index and emits nothing.
        self.model_combo.activated.connect(self._on_model_chosen)
        self.model_combo.setToolTip(
            "Any OpenRouter model id. Pick one, or choose Custom and type "
            "your own. Use Fetch to replace the list with what your account "
            "can actually reach."
        )
        model_form.addRow("Model id", self.model_combo)

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

        self.transcript = QtWidgets.QTextBrowser()
        self.transcript.setOpenExternalLinks(True)
        layout.addWidget(self.transcript, 1)

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
        self.new_chat_button = QtWidgets.QPushButton("New conversation")
        self.new_chat_button.clicked.connect(self.new_conversation)
        controls.addWidget(self.send_button)
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
        self.model_combo.clear()
        self.model_combo.addItem(CUSTOM_MODEL_LABEL)
        self.model_combo.insertSeparator(1)
        self.model_combo.addItems(sorted(entry["id"] for entry in models))
        self.model_combo.setCurrentText(current)
        self.model_status.setText(f"{len(models)} models available.")
        self.model_status.setStyleSheet(f"color: {TEXT_MUTED};")

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
        self._show_welcome()
        self.activity.clear()
        self.statusMessage.emit("Started a new conversation")

    def _ensure_assistant(self) -> bool:
        """Build the assistant if needed, reporting any problem."""
        if self.assistant is not None:
            self._apply_settings_to_assistant()
            return True

        from backend.ai_agent import create_assistant

        try:
            self.assistant = create_assistant(
                model=self.selected_model(),
                approve=self._approve_tool,
            )
        except AuthenticationError as error:
            self._append_error(str(error))
            return False
        except AIAgentError as error:
            self._append_error(str(error))
            return False

        self._apply_settings_to_assistant()
        return True

    def selected_model(self) -> str:
        """The model id in the box, with the Custom placeholder filtered out."""
        text = self.model_combo.currentText().strip()
        if not text or text == CUSTOM_MODEL_LABEL:
            return DEFAULT_MODEL
        return text

    def _on_model_chosen(self, index: int) -> None:
        """Clear the box when Custom is picked, so an id can be typed.

        Deferred by one event-loop turn because Qt writes the chosen item's
        text into the line edit *after* this signal, which would otherwise
        put the placeholder straight back.
        """
        if self.model_combo.itemText(index) == CUSTOM_MODEL_LABEL:
            QtCore.QTimer.singleShot(0, self, self._clear_model_box)

    def _clear_model_box(self) -> None:
        """Empty the model box and invite an id."""
        self.model_combo.setCurrentText("")
        self.model_combo.lineEdit().setPlaceholderText(
            "provider/model-id, e.g. deepseek/deepseek-chat"
        )
        self.model_combo.setFocus()

    def _apply_settings_to_assistant(self) -> None:
        """Push the current interface settings onto the assistant."""
        if self.assistant is None:
            return
        self.assistant.model = self.selected_model()
        self.assistant.max_rounds = self.max_rounds.value()
        self.assistant.approve = (
            self._approve_tool if self.confirm_long.isChecked() else None
        )

    def _persist_settings(self) -> None:
        """Remember the model and safety choices between sessions."""
        self.settings.ai_model = self.selected_model()
        self.settings.ai_confirm_long_tools = self.confirm_long.isChecked()
        self.settings.ai_max_tool_rounds = self.max_rounds.value()
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
        self._set_busy(True)

        worker = AIWorker(self.assistant, prompt)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.metrics.connect(self._on_metrics)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        self._start_meter()
        self.pool.start(worker)

    def _set_busy(self, busy: bool) -> None:
        """Enable or disable input while a request is in flight."""
        self._busy = busy
        self.send_button.setEnabled(not busy)
        self.send_button.setText("Working …" if busy else "Send")
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

    def _refresh_meter(self) -> None:
        """Redraw the meter, advancing the clock between rounds.

        The token and cost figures only move when a round completes, so
        without this the display would sit still through a four-minute mesh
        and look broken.
        """
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
        """Show what the assistant is doing right now."""
        self.activity.setText(message)
        self.statusMessage.emit(message)

    def _on_finished(self, reply: Any) -> None:
        """Render the completed reply."""
        self._set_busy(False)
        self.activity.clear()
        if getattr(reply, "metrics", None) is not None:
            self._metrics = reply.metrics
        self._stop_meter()

        if reply.tool_calls:
            self._append_tool_calls(reply.tool_calls)
        self._append_assistant(reply.text or "(no answer)")

        if reply.stopped_early:
            self._append_note(
                "Stopped early — either a step was declined or the tool-round "
                "limit was reached."
            )
        tokens = reply.usage.get("total_tokens")
        if tokens:
            self.statusMessage.emit(f"Done — {int(tokens)} tokens used")

    def _on_failed(self, message: str) -> None:
        """Report a failed request."""
        self._set_busy(False)
        self.activity.clear()
        self._stop_meter()
        self._append_error(message)

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
        self._append(
            f'<p style="margin-top:8px;"><b style="color:{SUCCESS};">Assistant</b>'
            f'<br>{html.escape(text).replace(chr(10), "<br>")}</p>'
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
