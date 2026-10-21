"""Colours and the Qt stylesheet. "Aureo" = gold, paired with a deep teal."""

TEAL = "#0F4C5C"
TEAL_DARK = "#0A3742"
TEAL_SOFT = "#E3EEF0"
GOLD = "#C8971E"
GOLD_SOFT = "#FBF3DE"
INK = "#1B2428"
MUTED = "#5E6B70"
LINE = "#D7DEE0"
BG = "#F3F5F4"
SURFACE = "#FFFFFF"
DANGER = "#B3261E"
DANGER_SOFT = "#FBE9E7"
SUCCESS = "#1E7A4C"
SUCCESS_SOFT = "#E4F4EA"

STATUS_COLORS = {
    "pending": (GOLD, GOLD_SOFT),
    "approved": (TEAL, TEAL_SOFT),
    "activating": (TEAL, TEAL_SOFT),
    "activated": (SUCCESS, SUCCESS_SOFT),
    "active": (SUCCESS, SUCCESS_SOFT),
    "used": (SUCCESS, SUCCESS_SOFT),
    "rejected": (DANGER, DANGER_SOFT),
    "disabled": (DANGER, DANGER_SOFT),
    "revoked": (DANGER, DANGER_SOFT),
    "expired": (MUTED, BG),
    "claimed": (MUTED, BG),
}

STYLESHEET = f"""
* {{ font-size: 15px; color: {INK}; }}
QMainWindow, QWidget#Root, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}

QFrame#Card {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 14px; }}
QFrame#TopBar {{ background: {TEAL}; }}
QFrame#TopBar QLabel {{ color: white; }}
QLabel#Brand {{ font-size: 26px; font-weight: 700; color: {TEAL}; }}
QLabel#BrandGold {{ font-size: 26px; font-weight: 700; color: {GOLD}; }}
QLabel#TopTitle {{ font-size: 18px; font-weight: 700; color: white; }}
QLabel#PageTitle {{ font-size: 21px; font-weight: 700; }}
QLabel#Section {{ font-size: 16px; font-weight: 700; }}
QLabel#Muted {{ color: {MUTED}; font-size: 13px; }}
QLabel#FieldLabel {{ color: {MUTED}; font-size: 13px; font-weight: 600; }}
QLabel#StatValue {{ font-size: 26px; font-weight: 700; color: {TEAL}; }}
QLabel#Code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 30px; font-weight: 700;
               letter-spacing: 3px; color: {TEAL_DARK}; background: {GOLD_SOFT};
               border: 1px dashed {GOLD}; border-radius: 10px; padding: 14px; }}
QLabel#Error {{ color: {DANGER}; background: {DANGER_SOFT}; border-radius: 8px; padding: 8px 10px; }}
QLabel#Success {{ color: {SUCCESS}; background: {SUCCESS_SOFT}; border-radius: 8px; padding: 8px 10px; }}
QLabel#Info {{ color: {TEAL_DARK}; background: {TEAL_SOFT}; border-radius: 8px; padding: 8px 10px; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 9px 10px; min-height: 22px; selection-background-color: {TEAL};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {TEAL}; padding: 8px 9px; }}

QPushButton {{
    background: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 9px 16px; min-height: 22px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {TEAL}; }}
QPushButton:disabled {{ color: #9AA5A9; background: #EEF1F1; }}
QPushButton#Primary {{ background: {TEAL}; color: white; border: none; }}
QPushButton#Primary:hover {{ background: {TEAL_DARK}; }}
QPushButton#Primary:disabled {{ background: #8FAAB1; color: #EEF3F4; }}
QPushButton#Gold {{ background: {GOLD}; color: white; border: none; }}
QPushButton#Danger {{ color: {DANGER}; }}
QPushButton#Danger:hover {{ border-color: {DANGER}; }}
QPushButton#Link {{ background: transparent; border: none; color: {TEAL}; padding: 6px 4px; text-align: left; }}
QPushButton#Link:hover {{ text-decoration: underline; }}
QPushButton#TopButton {{ background: transparent; color: white; border: 1px solid rgba(255,255,255,0.5); }}
QPushButton#TopButton:hover {{ background: {TEAL_DARK}; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; padding: 10px 16px; color: {MUTED}; font-weight: 600;
                border-bottom: 3px solid transparent; }}
QTabBar::tab:selected {{ color: {TEAL}; border-bottom: 3px solid {GOLD}; }}

QTableWidget {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 10px;
                gridline-color: transparent; selection-background-color: {TEAL_SOFT};
                selection-color: {INK}; alternate-background-color: #F8FAFA; }}
QHeaderView::section {{ background: {SURFACE}; border: none; border-bottom: 1px solid {LINE};
                        padding: 8px; font-weight: 700; color: {MUTED}; font-size: 13px; }}
QCheckBox {{ color: {MUTED}; font-size: 13px; }}
"""
