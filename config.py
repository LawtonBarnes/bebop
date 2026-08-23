"""Loads bebop's two config files: config.toml (static machine config)
and settings.ini (live user preferences bebop itself persists). See
config.toml's header comment for why they're split.
"""
import re
import tomllib
from configparser import ConfigParser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
SETTINGS_PATH = BASE_DIR / "settings.ini"

TEXT_COLORS = {
    "orange": (0xFF, 0xA5, 0x00),
    "green": (0x33, 0xFF, 0x33),
    "white": (0xFF, 0xFF, 0xFF),
    "cyan": (0x00, 0xFF, 0xFF),
}
DEFAULT_TEXT_COLOR = "cyan"

SHUFFLE_MODES = ["off", "songs", "albums"]
REPEAT_MODES = ["off", "one", "all"]


class Config:
    """Static machine config, read once at startup from config.toml."""

    def __init__(self, path=CONFIG_PATH):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        self.width = data["display"]["width"]
        self.height = data["display"]["height"]
        self.safe_area = data["display"]["safe_area"]
        self.font_path = BASE_DIR / data["font"]["path"]
        self.splash_path = BASE_DIR / data["splash"]["path"]
        self.splash_seconds = data["splash"]["seconds"]
        self.mpd_host = data["mpd"]["host"]
        self.mpd_port = data["mpd"]["port"]
        self.music_directory = Path(data["mpd"]["music_directory"])
        self.artwork_fallback_filenames = data["artwork"]["fallback_filenames"]
        self.artwork_size = data["artwork"]["size"]


def save_setting(section, key, value):
    # Surgical text edit that preserves settings.ini's comments --
    # verbatim copy of bars.py's save_setting(), see that file for the
    # full rationale (configparser's own .write() discards every
    # comment in the file on rewrite).
    text = SETTINGS_PATH.read_text() if SETTINGS_PATH.exists() else ""
    lines = text.splitlines(keepends=True)
    section_header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    in_section = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            in_section = True
            continue
        if in_section and stripped.startswith("[") and stripped != section_header:
            lines.insert(i, f"{key} = {value}\n")
            SETTINGS_PATH.write_text("".join(lines))
            return
        if in_section and key_re.match(line):
            lines[i] = f"{key} = {value}\n"
            SETTINGS_PATH.write_text("".join(lines))
            return
    if in_section:
        lines.append(f"{key} = {value}\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n{section_header}\n{key} = {value}\n")
    SETTINGS_PATH.write_text("".join(lines))


class Settings:
    """Live user preferences, read from settings.ini at startup and
    written back (via save_setting) whenever the Settings menu changes
    one, so they survive a restart."""

    def __init__(self, path=SETTINGS_PATH):
        parser = ConfigParser()
        parser.read(path)
        text_color = parser.get("bebop", "text_color", fallback=DEFAULT_TEXT_COLOR).lower()
        self.text_color = text_color if text_color in TEXT_COLORS else DEFAULT_TEXT_COLOR
        shuffle = parser.get("bebop", "shuffle", fallback="off").lower()
        self.shuffle = shuffle if shuffle in SHUFFLE_MODES else "off"
        repeat = parser.get("bebop", "repeat", fallback="off").lower()
        self.repeat = repeat if repeat in REPEAT_MODES else "off"

    @property
    def color(self):
        return TEXT_COLORS[self.text_color]

    def set_text_color(self, name):
        self.text_color = name
        save_setting("bebop", "text_color", name)

    def set_shuffle(self, mode):
        self.shuffle = mode
        save_setting("bebop", "shuffle", mode)

    def set_repeat(self, mode):
        self.repeat = mode
        save_setting("bebop", "repeat", mode)
