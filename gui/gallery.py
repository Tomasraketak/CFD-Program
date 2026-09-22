"""The Graphics tab: every rendered image in one place, and a way out of it.

Rendered images used to land in a ``renders`` folder inside each run's
directory under the data root, and the only trace of them was a path at the
end of the assistant's reply. An operator who asked for a picture of the
shock wave was told where it was, in a folder they had never opened.

This tab collects them. It lists every image in the run registry, newest
first, whoever made it -- the assistant, the MCP server running as a separate
process, or the tab itself -- shows the selected one large, and exports it
with one click. It can also draw new ones, so rendering is available from
the interface and not only through the assistant.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from core.settings import AppSettings, save_settings
from core.store import RunStore
from gui.theme import TEXT_MUTED

# How often the tab looks for images made elsewhere. The assistant and the
# MCP server write into the same registry; a few seconds' lag is invisible,
# and scanning a directory listing costs nothing.
POLL_INTERVAL_MS = 3000

# Thumbnail size in the list.
THUMBNAIL_SIZE = QtCore.QSize(220, 124)

# Images drawn automatically when a solve finishes in the interface: the
# shock system from the side, and the pressure on the body.
STANDARD_RENDERS = (
    ("mach_slice", "side"),
    ("surface_pressure", "isometric"),
)

# Formats offered on export. PNG is lossless and the default; JPEG is for a
# report or an e-mail where size matters more.
EXPORT_FILTER = "PNG image (*.png);;JPEG image (*.jpg *.jpeg)"


@dataclass(frozen=True)
class RenderedImage:
    """One image in the registry."""

    path: Path
    sim_id: str
    modified: float

    @property
    def label(self) -> str:
        """The image's name without the extension, made readable."""
        return self.path.stem.replace("_", " ")


def find_rendered_images(store: RunStore) -> list[RenderedImage]:
    """Every PNG in every run's ``renders`` folder, newest first."""
    images: list[RenderedImage] = []
    runs = store.runs_dir
    if not runs.is_dir():
        return images
    for path in runs.glob("*/renders/*.png"):
        try:
            modified = path.stat().st_mtime
        except OSError:  # pragma: no cover - removed while scanning
            continue
        images.append(
            RenderedImage(path=path, sim_id=path.parent.parent.name, modified=modified)
        )
    return sorted(images, key=lambda image: image.modified, reverse=True)


def export_image(source: Path | str, destination: Path | str) -> Path:
    """Copy an image, converting it when the extension asks for another format.

    A plain copy for PNG keeps the file byte-for-byte; anything else goes
    through Qt, which picks the encoder from the suffix.
    """
    source = Path(source)
    destination = Path(destination)
    if not destination.suffix:
        destination = destination.with_suffix(".png")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == source.suffix.lower():
        shutil.copy2(source, destination)
        return destination

    image = QtGui.QImage(str(source))
    if image.isNull():
        raise OSError(f"could not read {source.name}")
    if destination.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG has no alpha channel; flatten rather than let it go black.
        image = image.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    if not image.save(str(destination), quality=95):
        raise OSError(f"could not write {destination}")
    return destination


class RenderWorker(QtCore.QRunnable):
    """Draws one or more images off the GUI thread.

    A 4K render of a quarter-million-cell solution takes seconds, which is
    long enough to freeze the window if it ran on the event loop.
    """

    class Signals(QtCore.QObject):
        produced = QtCore.Signal(str)
        failed = QtCore.Signal(str)
        finished = QtCore.Signal()

    def __init__(self, store: RunStore, jobs: list[dict[str, Any]]) -> None:
        super().__init__()
        self.store = store
        self.jobs = jobs
        self.signals = self.Signals()

    @QtCore.Slot()
    def run(self) -> None:
        """Render every job, reporting each image as it appears."""
        try:
            import mcp_server
        except Exception as error:  # pragma: no cover - broken install
            self.signals.failed.emit(f"rendering unavailable: {error}")
            self.signals.finished.emit()
            return

        # Through the MCP tool's own code, so an image drawn here is drawn
        # exactly as one the assistant asks for: same frame, same caption,
        # same place in the registry.
        for job in self.jobs:
            try:
                reply = mcp_server.render_run_image(self.store, **job)
            except Exception as error:  # noqa: BLE001 - reported to the UI
                self.signals.failed.emit(f"{type(error).__name__}: {error}")
                continue
            if reply.get("ok"):
                self.signals.produced.emit(str(reply["image_path"]))
            else:
                self.signals.failed.emit(str(reply.get("error", "render failed")))
        self.signals.finished.emit()


class GraphicsTab(QtWidgets.QWidget):
    """Browse, draw and export rendered images."""

    statusMessage = QtCore.Signal(str)

    def __init__(
        self,
        store: RunStore,
        settings: AppSettings,
        data_root: Any = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.settings = settings
        self.data_root = data_root
        self.pool = QtCore.QThreadPool.globalInstance()
        self._images: list[RenderedImage] = []
        self._signature: tuple = ()
        self._pending_selection: str | None = None
        self._rendering = 0

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_browser())
        splitter.addWidget(self._build_viewer())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 800])

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._build_render_controls())
        layout.addWidget(splitter, 1)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(POLL_INTERVAL_MS)
        self.refresh()

    # -- construction ------------------------------------------------------

    def _build_render_controls(self) -> QtWidgets.QGroupBox:
        """Pick a finished run and draw a picture of it."""
        group = QtWidgets.QGroupBox("Draw a new image")
        try:
            from backend.visualizer import (
                CAMERA_VIEWS,
                COLORMAPS,
                RESOLUTIONS,
                VISUALIZATION_TYPES,
            )
        except Exception:  # pragma: no cover - PyVista missing or broken
            # Browsing and exporting still work without the renderer.
            CAMERA_VIEWS, COLORMAPS, RESOLUTIONS, VISUALIZATION_TYPES = (
                {"side": None}, ("turbo",), {"hd": None}, ("mach_slice",)
            )
            group.setEnabled(False)
            group.setToolTip("Rendering needs PyVista, which is not installed.")

        row = QtWidgets.QHBoxLayout(group)
        row.setSpacing(6)

        self.run_combo = QtWidgets.QComboBox()
        self.run_combo.setMinimumContentsLength(24)
        self.run_combo.setToolTip("A finished simulation to draw")
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(VISUALIZATION_TYPES)
        self.type_combo.setCurrentText("mach_slice")
        self.view_combo = QtWidgets.QComboBox()
        self.view_combo.addItems(list(CAMERA_VIEWS))
        self.view_combo.setCurrentText("side")
        self.view_combo.setToolTip(
            "Rockets are drawn nose-up along +Z. 'side' looks straight at "
            "the Mach slice."
        )
        self.colormap_combo = QtWidgets.QComboBox()
        self.colormap_combo.addItems(COLORMAPS)
        self.colormap_combo.setCurrentText(
            self.settings.default_colormap
            if self.settings.default_colormap in COLORMAPS
            else "turbo"
        )
        self.resolution_combo = QtWidgets.QComboBox()
        self.resolution_combo.addItems(list(RESOLUTIONS))
        self.resolution_combo.setCurrentText(
            self.settings.default_render_resolution
            if self.settings.default_render_resolution in RESOLUTIONS
            else "hd"
        )
        self.render_button = QtWidgets.QPushButton("Render")
        self.render_button.setObjectName("primary")
        self.render_button.clicked.connect(self.render_selected_run)

        for caption, widget in (
            ("Run", self.run_combo),
            ("Image", self.type_combo),
            ("View", self.view_combo),
            ("Colours", self.colormap_combo),
            ("Size", self.resolution_combo),
        ):
            label = QtWidgets.QLabel(caption)
            label.setObjectName("hint")
            row.addWidget(label)
            row.addWidget(widget)
        row.addWidget(self.render_button)
        row.addStretch(1)
        return group

    def _build_browser(self) -> QtWidgets.QWidget:
        """Thumbnails of every image, with a filter by run."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        filters = QtWidgets.QHBoxLayout()
        caption = QtWidgets.QLabel("Show")
        caption.setObjectName("hint")
        self.filter_combo = QtWidgets.QComboBox()
        self.filter_combo.addItem("All runs", None)
        self.filter_combo.currentIndexChanged.connect(self._populate_list)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh(force=True))
        filters.addWidget(caption)
        filters.addWidget(self.filter_combo, 1)
        filters.addWidget(refresh)
        layout.addLayout(filters)

        self.image_list = QtWidgets.QListWidget()
        self.image_list.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.image_list.setIconSize(THUMBNAIL_SIZE)
        self.image_list.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.image_list.setMovement(QtWidgets.QListView.Movement.Static)
        self.image_list.setWordWrap(True)
        self.image_list.setSpacing(6)
        self.image_list.setUniformItemSizes(True)
        self.image_list.currentItemChanged.connect(self._show_current)
        self.image_list.itemDoubleClicked.connect(lambda _item: self.open_externally())
        layout.addWidget(self.image_list, 1)

        self.empty_hint = QtWidgets.QLabel(
            "No images yet. Finish a simulation and draw one above, or ask "
            "the assistant for one — they all appear here."
        )
        self.empty_hint.setObjectName("hint")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)
        return panel

    def _build_viewer(self) -> QtWidgets.QWidget:
        """The selected image, large, with the export buttons."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.preview = QtWidgets.QLabel()
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 200)
        self.preview.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Ignored,
        )
        self.preview.setStyleSheet(f"color: {TEXT_MUTED};")
        self.preview.setText("Select an image")
        layout.addWidget(self.preview, 1)

        self.details = QtWidgets.QLabel()
        self.details.setObjectName("hint")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.details)

        buttons = QtWidgets.QHBoxLayout()
        self.export_button = QtWidgets.QPushButton("Export …")
        self.export_button.setObjectName("primary")
        self.export_button.setToolTip("Save a copy of this image anywhere, as PNG or JPEG")
        self.export_button.clicked.connect(self.export_selected)
        self.export_run_button = QtWidgets.QPushButton("Export all from this run …")
        self.export_run_button.clicked.connect(self.export_run)
        self.copy_button = QtWidgets.QPushButton("Copy")
        self.copy_button.setToolTip("Copy the image to the clipboard")
        self.copy_button.clicked.connect(self.copy_selected)
        self.open_button = QtWidgets.QPushButton("Open")
        self.open_button.setToolTip("Open in the system image viewer")
        self.open_button.clicked.connect(self.open_externally)
        self.folder_button = QtWidgets.QPushButton("Show folder")
        self.folder_button.clicked.connect(self.open_folder)
        for button in (
            self.export_button, self.export_run_button, self.copy_button,
            self.open_button, self.folder_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._set_actions_enabled(False)
        return panel

    # -- listing -----------------------------------------------------------

    def refresh(self, force: bool = False) -> None:
        """Rescan the registry, redrawing only when something changed."""
        images = find_rendered_images(self.store)
        signature = tuple((str(image.path), image.modified) for image in images)
        self._refresh_runs()
        if signature == self._signature and not force:
            return
        self._images = images
        self._signature = signature
        self._refresh_filter()
        self._populate_list()

    def _refresh_runs(self) -> None:
        """Offer every finished simulation for drawing, newest first."""
        try:
            records = [
                record
                for record in self.store.list_records()
                if record.kind in ("aero", "thermal")
                and "failed" not in record.metadata
            ]
        except OSError:  # pragma: no cover - registry unreadable
            return
        wanted = [record.record_id for record in records]
        current = [
            self.run_combo.itemData(index) for index in range(self.run_combo.count())
        ]
        if wanted == current:
            return
        chosen = self.run_combo.currentData()
        blocked = self.run_combo.blockSignals(True)
        try:
            self.run_combo.clear()
            for record in records:
                self.run_combo.addItem(_describe_run(record), record.record_id)
            index = self.run_combo.findData(chosen)
            self.run_combo.setCurrentIndex(max(index, 0))
        finally:
            self.run_combo.blockSignals(blocked)
        self.render_button.setEnabled(bool(records) and self._rendering == 0)

    def _refresh_filter(self) -> None:
        """One filter entry per run that has images."""
        chosen = self.filter_combo.currentData()
        runs = sorted({image.sim_id for image in self._images}, reverse=True)
        blocked = self.filter_combo.blockSignals(True)
        try:
            self.filter_combo.clear()
            self.filter_combo.addItem("All runs", None)
            for run in runs:
                self.filter_combo.addItem(run, run)
            index = self.filter_combo.findData(chosen) if chosen else 0
            self.filter_combo.setCurrentIndex(max(index, 0))
        finally:
            self.filter_combo.blockSignals(blocked)

    def _populate_list(self, *_: Any) -> None:
        """Fill the thumbnail list, keeping the selection where possible."""
        selected = self._pending_selection or self.selected_path()
        run = self.filter_combo.currentData()
        self.image_list.clear()
        for image in self._images:
            if run and image.sim_id != run:
                continue
            item = QtWidgets.QListWidgetItem(
                _thumbnail(image.path), f"{image.label}\n{image.sim_id}"
            )
            item.setData(QtCore.Qt.ItemDataRole.UserRole, str(image.path))
            item.setToolTip(str(image.path))
            self.image_list.addItem(item)

        self.empty_hint.setVisible(self.image_list.count() == 0)
        if self.image_list.count() == 0:
            self._show_current(None)
            return
        target = 0
        if selected:
            for row in range(self.image_list.count()):
                if self.image_list.item(row).data(QtCore.Qt.ItemDataRole.UserRole) == str(selected):
                    target = row
                    break
        self.image_list.setCurrentRow(target)
        if self._pending_selection and str(self._pending_selection) == self.selected_path():
            self._pending_selection = None

    def select_image(self, path: Path | str) -> None:
        """Show a particular image, rescanning first if it is new."""
        self._pending_selection = str(Path(path))
        self.refresh(force=True)

    def selected_path(self) -> str | None:
        """Path of the selected image, if any."""
        item = self.image_list.currentItem()
        if item is None:
            return None
        return item.data(QtCore.Qt.ItemDataRole.UserRole)

    # -- viewing -----------------------------------------------------------

    def _show_current(self, item: Any, _previous: Any = None) -> None:
        """Put the selected image in the large view."""
        path = item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
        self._set_actions_enabled(path is not None)
        if path is None:
            self._pixmap = None
            self.preview.setPixmap(QtGui.QPixmap())
            self.preview.setText("Select an image")
            self.details.clear()
            return
        self._pixmap = QtGui.QPixmap(path)
        if self._pixmap.isNull():
            self.preview.setText("This image could not be read.")
        else:
            self._fit_preview()
        info = Path(path)
        try:
            stamp = datetime.fromtimestamp(info.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:  # pragma: no cover - removed while viewing
            stamp = ""
        size = (
            f"{self._pixmap.width()} × {self._pixmap.height()} px"
            if not self._pixmap.isNull()
            else ""
        )
        self.details.setText(f"{info.name}  ·  {size}  ·  {stamp}\n{info}")

    def _fit_preview(self) -> None:
        """Scale the image to the space available, keeping its proportions."""
        pixmap = getattr(self, "_pixmap", None)
        if pixmap is None or pixmap.isNull():
            return
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        """Rescale the large view with the window."""
        super().resizeEvent(event)
        self._fit_preview()

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button in (
            self.export_button, self.export_run_button, self.copy_button,
            self.open_button, self.folder_button,
        ):
            button.setEnabled(enabled)

    # -- export ------------------------------------------------------------

    def _export_directory(self) -> str:
        """Where the last export went, or the user's pictures folder."""
        remembered = self.settings.last_export_directory
        if remembered and Path(remembered).is_dir():
            return remembered
        locations = QtCore.QStandardPaths.standardLocations(
            QtCore.QStandardPaths.StandardLocation.PicturesLocation
        )
        return locations[0] if locations else str(Path.home())

    def _remember_export_directory(self, directory: Path) -> None:
        self.settings.last_export_directory = str(directory)
        try:
            save_settings(self.settings, self.data_root)
        except Exception:  # noqa: BLE001 - a preference is not worth a failure
            pass

    def export_selected(self) -> Path | None:
        """Save a copy of the selected image where the operator chooses."""
        source = self.selected_path()
        if source is None:
            return None
        source_path = Path(source)
        suggested = Path(self._export_directory()) / (
            f"{source_path.parent.parent.name}_{source_path.name}"
        )
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export image", str(suggested), EXPORT_FILTER
        )
        if not destination:
            return None
        try:
            written = export_image(source_path, destination)
        except OSError as error:
            QtWidgets.QMessageBox.warning(self, "Export image", str(error))
            return None
        self._remember_export_directory(written.parent)
        self.statusMessage.emit(f"Exported {written.name}")
        return written

    def export_run(self) -> list[Path]:
        """Copy every image of the selected image's run into a folder."""
        source = self.selected_path()
        if source is None:
            return []
        run = Path(source).parent.parent.name
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, f"Export all images of {run}", self._export_directory()
        )
        if not directory:
            return []
        written: list[Path] = []
        try:
            for image in self._images:
                if image.sim_id == run:
                    written.append(
                        export_image(
                            image.path, Path(directory) / f"{run}_{image.path.name}"
                        )
                    )
        except OSError as error:
            QtWidgets.QMessageBox.warning(self, "Export images", str(error))
        if written:
            self._remember_export_directory(Path(directory))
            self.statusMessage.emit(f"Exported {len(written)} images to {directory}")
        return written

    def copy_selected(self) -> None:
        """Put the selected image on the clipboard, for pasting into a report."""
        source = self.selected_path()
        if source is None:
            return
        image = QtGui.QImage(source)
        if image.isNull():
            return
        QtWidgets.QApplication.clipboard().setImage(image)
        self.statusMessage.emit("Image copied to the clipboard")

    def open_externally(self) -> None:
        """Open the selected image in the system viewer."""
        source = self.selected_path()
        if source is not None:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(source))

    def open_folder(self) -> None:
        """Open the folder holding the selected image."""
        source = self.selected_path()
        if source is not None:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(Path(source).parent))
            )

    # -- drawing -----------------------------------------------------------

    def render_selected_run(self) -> None:
        """Draw the image chosen in the controls."""
        sim_id = self.run_combo.currentData()
        if not sim_id:
            QtWidgets.QMessageBox.information(
                self, "Draw an image", "Finish a simulation first."
            )
            return
        self._start_render(
            [
                {
                    "sim_id": sim_id,
                    "visualization_type": self.type_combo.currentText(),
                    "camera_view": self.view_combo.currentText(),
                    "colormap": self.colormap_combo.currentText(),
                    "resolution": self.resolution_combo.currentText(),
                }
            ]
        )

    def render_standard(self, sim_id: str) -> None:
        """Draw the usual pictures of a freshly finished aerodynamic run."""
        self._start_render(
            [
                {
                    "sim_id": sim_id,
                    "visualization_type": kind,
                    "camera_view": view,
                    "colormap": self.colormap_combo.currentText(),
                    "resolution": "hd",
                }
                for kind, view in STANDARD_RENDERS
            ]
        )

    def _start_render(self, jobs: list[dict[str, Any]]) -> None:
        self._rendering += 1
        self.render_button.setEnabled(False)
        self.render_button.setText("Rendering …")
        worker = RenderWorker(self.store, jobs)
        worker.signals.produced.connect(self.select_image)
        worker.signals.failed.connect(self._render_failed)
        worker.signals.finished.connect(self._render_finished)
        self.statusMessage.emit(f"Rendering {len(jobs)} image(s) …")
        self.pool.start(worker)

    def _render_failed(self, message: str) -> None:
        self.statusMessage.emit(f"Render failed: {message.splitlines()[0]}")
        self.details.setText(f"Render failed: {message}")

    def _render_finished(self) -> None:
        self._rendering = max(0, self._rendering - 1)
        if self._rendering == 0:
            self.render_button.setText("Render")
            self.render_button.setEnabled(self.run_combo.count() > 0)
            self.statusMessage.emit("Images ready in the Graphics tab")


def _describe_run(record: Any) -> str:
    """A run's id with the condition it was run at, when known."""
    mach = record.metadata.get("mach")
    if isinstance(mach, (int, float)):
        return f"{record.record_id}  (M {mach:.2f})"
    return record.record_id


def _thumbnail(path: Path) -> QtGui.QIcon:
    """A scaled-down icon, so the list does not hold 4K images in memory."""
    reader = QtGui.QImageReader(str(path))
    size = reader.size()
    if size.isValid():
        reader.setScaledSize(size.scaled(THUMBNAIL_SIZE, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return QtGui.QIcon()
    return QtGui.QIcon(QtGui.QPixmap.fromImage(image))
