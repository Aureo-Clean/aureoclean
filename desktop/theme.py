"""Colours and the Qt stylesheet, taken from the AureoClean logo
(navy/blue "A" and water drop, green leaf). Sized for the 7-inch 800x480 display."""

NAVY = "#14336E"
BLUE = "#1F63C6"
BLUE_DARK = "#174A9C"
SKY = "#1EA7E8"
BLUE_SOFT = "#E6F0FB"
GREEN = "#2E9E48"
GREEN_DARK = "#24803A"
GREEN_SOFT = "#E5F4E8"
INK = "#162338"
MUTED = "#5B6B80"
LINE = "#D5DFEA"
BG = "#F2F5F9"
SURFACE = "#FFFFFF"
DANGER = "#B3261E"
DANGER_SOFT = "#FBE9E7"
AMBER = "#B26A00"
AMBER_SOFT = "#FFF3DC"

STATUS_COLORS = {
    "pending": (AMBER, AMBER_SOFT),
    "approved": (BLUE, BLUE_SOFT),
    "activating": (BLUE, BLUE_SOFT),
    "activated": (GREEN, GREEN_SOFT),
    "active": (GREEN, GREEN_SOFT),
    "used": (GREEN, GREEN_SOFT),
    "rejected": (DANGER, DANGER_SOFT),
    "disabled": (DANGER, DANGER_SOFT),
    "revoked": (DANGER, DANGER_SOFT),
    "expired": (MUTED, BG),
    "claimed": (MUTED, BG),
}

STYLESHEET = f"""
* {{ font-size: 14px; color: {INK}; }}
QMainWindow, QWidget#Root, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}

QFrame#Card {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 12px; }}
QFrame#TopBar {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {NAVY}, stop:1 {BLUE}); }}
QFrame#TopBar QLabel {{ color: white; }}
QLabel#Brand {{ font-size: 24px; font-weight: 800; color: {GREEN}; }}
QLabel#BrandGold {{ font-size: 24px; font-weight: 800; color: {NAVY}; }}
QLabel#Tagline {{ color: {MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#TopTitle {{ font-size: 16px; font-weight: 700; color: white; }}
QLabel#PageTitle {{ font-size: 19px; font-weight: 700; color: {NAVY}; }}
QLabel#Section {{ font-size: 15px; font-weight: 700; color: {NAVY}; }}
QLabel#Muted {{ color: {MUTED}; font-size: 12px; }}
QLabel#FieldLabel {{ color: {MUTED}; font-size: 12px; font-weight: 600; }}
QLabel#StatValue {{ font-size: 22px; font-weight: 700; color: {BLUE}; }}
QLabel#Code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 28px; font-weight: 700;
               letter-spacing: 3px; color: {NAVY}; background: {GREEN_SOFT};
               border: 1px dashed {GREEN}; border-radius: 10px; padding: 12px; }}
QLabel#Error {{ color: {DANGER}; background: {DANGER_SOFT}; border-radius: 8px; padding: 7px 9px; }}
QLabel#Success {{ color: {GREEN_DARK}; background: {GREEN_SOFT}; border-radius: 8px; padding: 9px 10px;
                  font-weight: 600; }}
QLabel#Info {{ color: {BLUE_DARK}; background: {BLUE_SOFT}; border-radius: 8px; padding: 7px 9px; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 6px 9px; min-height: 22px; selection-background-color: {BLUE};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {BLUE}; padding: 5px 8px; }}

QPushButton {{
    background: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 7px 14px; min-height: 22px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {BLUE}; }}
QPushButton:disabled {{ color: #9AA6B5; background: #EEF2F6; }}
QPushButton#Primary {{ background: {BLUE}; color: white; border: none; }}
QPushButton#Primary:hover {{ background: {BLUE_DARK}; }}
QPushButton#Primary:disabled {{ background: #9DB7DF; color: #F1F5FB; }}
QPushButton#Gold {{ background: {GREEN}; color: white; border: none; }}
QPushButton#Gold:hover {{ background: {GREEN_DARK}; }}
QPushButton#Danger {{ color: {DANGER}; }}
QPushButton#Danger:hover {{ border-color: {DANGER}; }}
QPushButton#Small {{ padding: 3px 10px; min-height: 20px; font-size: 12px; }}
QPushButton#Link {{ background: transparent; border: none; color: {BLUE}; padding: 4px 2px; text-align: left; }}
QPushButton#Link:hover {{ text-decoration: underline; }}
QPushButton#TopButton {{ background: transparent; color: white; border: 1px solid rgba(255,255,255,0.55);
                          padding: 4px 12px; }}
QPushButton#TopButton:hover {{ background: rgba(255,255,255,0.15); }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; padding: 7px 12px; color: {MUTED}; font-weight: 600;
                border-bottom: 3px solid transparent; }}
QTabBar::tab:selected {{ color: {NAVY}; border-bottom: 3px solid {GREEN}; }}

QTableWidget {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 10px;
                gridline-color: transparent; selection-background-color: {BLUE_SOFT};
                selection-color: {INK}; alternate-background-color: #F7F9FC; }}
QHeaderView::section {{ background: {SURFACE}; border: none; border-bottom: 1px solid {LINE};
                        padding: 6px; font-weight: 700; color: {MUTED}; font-size: 12px; }}
QCheckBox {{ color: {INK}; font-size: 13px; }}
"""
