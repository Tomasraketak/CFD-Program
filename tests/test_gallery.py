"""The Graphics tab: finding, showing, drawing and exporting images.

An operator asked for a picture of a shock wave, got one, and then had no
idea where it was: the path was the last line of a long reply, in a folder
they had never opened. These tests hold the tab to the promise that fixes
that -- every image in the registry is listed, shown and exportable, whoever
drew it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# pydantic must finish importing before Qt; see gui/__init__.py.
from core.models import FlowParams  # noqa: E402,F401
from core.settings import AppSettings  # noqa: E402
from core.store import RunStore  # noqa: E402

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from gui import gallery  # noqa: E402
from gui.gallery import (  # noqa: E402
    GraphicsTab,
    export_image,
    find_rendered_images,
)
from gui.main_window import build_application  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = build_application([])
    yield app
    app.processEvents()


@pytest.fixture
def graphics_tab(qt_app, store: RunStore, isolated_data_root):
    tab = GraphicsTab(store, AppSettings(), isolated_data_root)
    tab.resize(1200, 800)
    yield tab
    tab._timer.stop()
    tab.deleteLater()


def write_png(path: Path, colour: str = "#4da3ff", size=(64, 36)) -> Path:
    """A small real PNG, standing in for a render."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QtGui.QImage(*size, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(colour))
    assert image.save(str(path))
    return path


def make_render(
    store: RunStore, name: str, kind: str = "aero", size=(64, 36), **metadata
) -> Path:
    record = store.create(kind, metadata)
    return write_png(record.path("renders", name), size=size)


def test_every_render_in_the_registry_is_found(store):
    """Images from any run, newest first."""
    first = make_render(store, "mach_slice_side.png")
    second = make_render(store, "surface_pressure_isometric.png")
    os.utime(first, (1_000, 1_000))

    images = find_rendered_images(store)
    assert [image.path for image in images] == [second, first]
    assert images[0].sim_id == second.parent.parent.name


def test_the_tab_lists_and_shows_images(graphics_tab, store):
    path = make_render(store, "mach_slice_side.png", mach=1.3)
    graphics_tab.refresh(force=True)

    assert graphics_tab.image_list.count() == 1
    assert graphics_tab.selected_path() == str(path)
    assert graphics_tab.export_button.isEnabled()
    assert "mach_slice_side.png" in graphics_tab.details.text()
    assert graphics_tab.empty_hint.isHidden()


def test_an_empty_registry_says_how_images_arrive(graphics_tab):
    graphics_tab.refresh(force=True)
    assert graphics_tab.image_list.count() == 0
    assert not graphics_tab.export_button.isEnabled()
    assert "assistant" in graphics_tab.empty_hint.text()


def test_images_made_elsewhere_appear_without_a_click(graphics_tab, store):
    """The assistant or an external MCP client writes; the tab notices."""
    graphics_tab.refresh(force=True)
    assert graphics_tab.image_list.count() == 0
    make_render(store, "mach_slice_side.png")
    graphics_tab.refresh()  # what the poll timer calls
    assert graphics_tab.image_list.count() == 1


def test_selecting_an_image_by_path(graphics_tab, store):
    older = make_render(store, "a.png")
    make_render(store, "b.png")
    graphics_tab.select_image(older)
    assert graphics_tab.selected_path() == str(older)


def test_the_filter_narrows_to_one_run(graphics_tab, store):
    first = make_render(store, "a.png")
    make_render(store, "b.png")
    graphics_tab.refresh(force=True)
    index = graphics_tab.filter_combo.findData(first.parent.parent.name)
    graphics_tab.filter_combo.setCurrentIndex(index)
    assert graphics_tab.image_list.count() == 1
    assert graphics_tab.selected_path() == str(first)


def test_export_copies_a_png_unchanged(tmp_path):
    source = write_png(tmp_path / "in" / "mach.png")
    written = export_image(source, tmp_path / "out" / "shock.png")
    assert written.read_bytes() == source.read_bytes()


def test_export_converts_to_jpeg_by_extension(tmp_path):
    source = write_png(tmp_path / "mach.png")
    written = export_image(source, tmp_path / "shock.jpg")
    assert written.read_bytes()[:3] == b"\xff\xd8\xff"


def test_export_without_an_extension_writes_png(tmp_path):
    source = write_png(tmp_path / "mach.png")
    written = export_image(source, tmp_path / "shock")
    assert written.suffix == ".png"
    assert written.is_file()


def test_the_export_button_saves_where_the_operator_says(
    graphics_tab, store, tmp_path, monkeypatch
):
    make_render(store, "mach_slice_side.png")
    graphics_tab.refresh(force=True)
    target = tmp_path / "report" / "shock.png"
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getSaveFileName",
        lambda *a, **k: (str(target), "PNG image (*.png)"),
    )
    written = graphics_tab.export_selected()
    assert written == target and target.is_file()
    # The next export starts in the same folder.
    assert graphics_tab.settings.last_export_directory == str(target.parent)


def test_export_all_copies_every_image_of_the_run(
    graphics_tab, store, tmp_path, monkeypatch
):
    record = store.create("aero", {})
    write_png(record.path("renders", "mach_slice_side.png"))
    write_png(record.path("renders", "surface_pressure_isometric.png"))
    make_render(store, "other_run.png")
    graphics_tab.refresh(force=True)
    graphics_tab.select_image(record.path("renders", "mach_slice_side.png"))

    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path)
    )
    written = graphics_tab.export_run()
    assert sorted(path.name for path in written) == [
        f"{record.record_id}_mach_slice_side.png",
        f"{record.record_id}_surface_pressure_isometric.png",
    ]


def test_copy_puts_the_image_on_the_clipboard(graphics_tab, store, qt_app):
    make_render(store, "mach.png", size=(40, 20))
    graphics_tab.refresh(force=True)
    graphics_tab.copy_selected()
    image = QtWidgets.QApplication.clipboard().image()
    assert (image.width(), image.height()) == (40, 20)


def test_finished_runs_are_offered_for_drawing(graphics_tab, store):
    store.create("aero", {"mach": 1.3})
    store.create("aero", {"failed": "diverged"})
    store.create("mesh", {})
    graphics_tab.refresh(force=True)
    assert graphics_tab.run_combo.count() == 1
    assert "M 1.30" in graphics_tab.run_combo.itemText(0)


def test_render_standard_draws_the_shock_and_the_surface(graphics_tab, monkeypatch):
    """A solve finished in the interface gets its pictures without asking."""
    started = []
    monkeypatch.setattr(
        graphics_tab.pool, "start", lambda worker: started.append(worker)
    )
    graphics_tab.render_standard("aero-1")
    jobs = started[0].jobs
    assert [(job["visualization_type"], job["camera_view"]) for job in jobs] == [
        ("schlieren", "side"),
        ("mach_slice", "side"),
        ("surface_pressure", "isometric"),
    ]
    assert not graphics_tab.render_button.isEnabled()


def test_the_worker_renders_through_the_tool_code(store, monkeypatch, tmp_path):
    """Drawn here or by the assistant, an image comes out the same."""
    import mcp_server

    produced, failed = [], []
    calls = []

    def fake(store_arg, **job):
        calls.append((store_arg, job))
        if job["visualization_type"] == "thermal":
            return {"ok": False, "error": "no temperature field"}
        return {"ok": True, "image_path": str(tmp_path / "x.png")}

    monkeypatch.setattr(mcp_server, "render_run_image", fake)
    worker = gallery.RenderWorker(
        store,
        [
            {"sim_id": "a", "visualization_type": "mach_slice"},
            {"sim_id": "a", "visualization_type": "thermal"},
        ],
    )
    worker.signals.produced.connect(produced.append)
    worker.signals.failed.connect(failed.append)
    worker.run()
    assert calls[0][0] is store
    assert produced == [str(tmp_path / "x.png")]
    assert failed == ["no temperature field"]


def test_an_image_from_the_assistant_appears_in_the_transcript(
    qt_app, isolated_data_root, tmp_path
):
    """The picture is shown where the operator is looking, with a way on."""
    from backend.ai_agent import EVENT_TOOL_FINISHED, ProgressEvent
    from gui.ai_panel import AITab

    tab = AITab(AppSettings(), isolated_data_root)
    image = write_png(tmp_path / "mach_slice_side.png")
    received = []
    tab.imageProduced.connect(received.append)
    tab._on_event(
        ProgressEvent(
            kind=EVENT_TOOL_FINISHED,
            message="ok",
            tool="generate_cfd_visualization",
            result={"ok": True, "image_path": str(image)},
        )
    )
    assert received == [str(image)]
    assert "Graphics tab" in tab.transcript.toPlainText()

    wanted = []
    tab.showGraphics.connect(wanted.append)
    tab._on_link(QtCore.QUrl(f"ats-graphics:{image}"))
    assert wanted == [str(image)]
    tab.deleteLater()


def test_the_main_window_routes_images_to_the_graphics_tab(qt_app, store, tmp_path):
    from gui.main_window import MainWindow

    window = MainWindow(store)
    try:
        path = make_render(store, "mach_slice_side.png")
        window.ai_tab.showGraphics.emit(str(path))
        assert window.tabs.currentWidget() is window.graphics_tab
        assert window.graphics_tab.selected_path() == str(path)
    finally:
        window.graphics_tab._timer.stop()
        window.deleteLater()


def test_the_status_line_says_when_rendering_is_done(graphics_tab):
    messages = []
    graphics_tab.statusMessage.connect(messages.append)
    graphics_tab._rendering = 1
    graphics_tab._render_finished()
    assert graphics_tab.render_button.text() == "Render"
    assert "ready" in messages[-1]


def test_several_images_can_be_picked_and_deleted(graphics_tab, store):
    """Old renders pile up; picking a handful and deleting them should be easy."""
    keep = make_render(store, "keep.png")
    first = make_render(store, "old_a.png")
    second = make_render(store, "old_b.png")
    graphics_tab.refresh(force=True)

    graphics_tab.image_list.clearSelection()
    for row in range(graphics_tab.image_list.count()):
        item = graphics_tab.image_list.item(row)
        if item.data(QtCore.Qt.ItemDataRole.UserRole) in (str(first), str(second)):
            item.setSelected(True)
    assert sorted(graphics_tab.selected_paths()) == sorted([str(first), str(second)])
    assert graphics_tab.delete_button.text() == "Delete 2"

    deleted = graphics_tab.delete_selected(confirm=False)
    assert sorted(deleted) == sorted([first, second])
    assert not first.exists() and not second.exists() and keep.exists()
    assert graphics_tab.image_list.count() == 1
