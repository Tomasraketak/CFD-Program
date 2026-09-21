"""Dark aerospace theme for the desktop application.

Kept as a Python module rather than a .qss file so the palette constants can
be shared with the 3D viewport and the residual charts, which are not styled
by Qt stylesheets.
"""

from __future__ import annotations

# Core palette.
BACKGROUND = "#12151c"
SURFACE = "#1a1f29"
SURFACE_RAISED = "#222836"
BORDER = "#2e3644"
TEXT = "#e6eaf0"
TEXT_MUTED = "#98a2b3"
ACCENT = "#4da3ff"
ACCENT_DIM = "#2b6cb0"
SUCCESS = "#3fb950"
WARNING = "#d29922"
DANGER = "#f85149"

# Semi-transparent panel fill, so the 3D scene reads through the tool panels.
PANEL_TRANSLUCENT = "rgba(26, 31, 41, 0.88)"

# Residual chart series colours.
CHART_COLOURS = ("#4da3ff", "#f778ba", "#3fb950", "#d29922", "#a371f7")

STYLESHEET = f"""
QWidget {{
    background-color: {BACKGROUND};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", "DejaVu Sans", sans-serif;
    font-size: 13px;
}}

QMainWindow, QDialog {{ background-color: {BACKGROUND}; }}

QGroupBox {{
    background-color: {PANEL_TRANSLUCENT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 16px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {ACCENT};
    letter-spacing: 0.5px;
}}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    background-color: {SURFACE};
}}
QTabBar::tab {{
    background: {SURFACE};
    color: {TEXT_MUTED};
    padding: 8px 18px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {SURFACE_RAISED};
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}

QPushButton {{
    background-color: {SURFACE_RAISED};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 7px 14px;
    font-weight: 500;
}}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:pressed {{ background-color: {ACCENT_DIM}; color: {TEXT}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; border-color: {BORDER}; }}

QPushButton#primary {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT};
    color: {TEXT};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background-color: {ACCENT}; color: {BACKGROUND}; }}

QPushButton#axis {{ padding: 6px 10px; font-family: monospace; }}
QPushButton#axis:checked {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT};
    color: {TEXT};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 7px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE_RAISED};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT};
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT_DIM}; border-radius: 2px; }}

QProgressBar {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    height: 16px;
}}
QProgressBar::chunk {{ background-color: {ACCENT_DIM}; border-radius: 3px; }}

QPlainTextEdit#log {{
    font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    color: {TEXT_MUTED};
}}

QLabel#resultValue {{
    font-size: 21px;
    font-weight: 700;
    color: {ACCENT};
}}
QLabel#resultUnit {{ color: {TEXT_MUTED}; font-size: 11px; }}
QLabel#heading {{
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 1px;
    color: {TEXT};
}}
QLabel#hint {{ color: {TEXT_MUTED}; font-size: 11px; }}

QStatusBar {{ background-color: {SURFACE}; border-top: 1px solid {BORDER}; }}
QSplitter::handle {{ background-color: {BORDER}; }}
QScrollBar:vertical {{
    background: {BACKGROUND}; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
