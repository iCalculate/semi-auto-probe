from __future__ import annotations

import logging
import sys
from datetime import datetime

from . import __version__


LOGO = r"""
   _____                _        ___        __          ____            _
  / ___/___  ____ ___  (_)      /   | _____/ /_____    / __ \_________  /_  ___
  \__ \/ _ \/ __ `__ \/ /______/ /| |/ ___/ __/ __ \  / /_/ / ___/ __ \/ / / _ \
 ___/ /  __/ / / / / / /_____/ ___ / /  / /_/ /_/ / / ____/ /  / /_/ / /_/  __/
/____/\___/_/ /_/ /_/_/     /_/  |_/_/   \__/\____/ /_/   /_/   \____/\__/\___/
"""


LEVEL_COLORS = {
    "DEBUG": "\033[38;5;245m",
    "INFO": "\033[38;5;81m",
    "WARNING": "\033[38;5;214m",
    "ERROR": "\033[38;5;203m",
    "CRITICAL": "\033[1;38;5;203m",
}
FRAME_PART_COLORS = {
    "head": "\033[38;5;81m",
    "function": "\033[38;5;214m",
    "axis": "\033[38;5;147m",
    "data": "\033[38;5;250m",
    "checksum": "\033[38;5;203m",
    "tail": "\033[38;5;245m",
}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"


class ConsoleFormatter(logging.Formatter):
    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        level = f"[{record.levelname}]"
        message = record.getMessage()
        if not self.use_color:
            return f"{timestamp} {level:<10} {message}"

        color = LEVEL_COLORS.get(record.levelname, "")
        return f"{DIM}{timestamp}{RESET} {color}{level:<10}{RESET} {message}"


class DynamicConsoleHandler(logging.StreamHandler):
    def __init__(self, stream=None) -> None:
        super().__init__(stream)
        self._repeat_key: str | None = None
        self._repeat_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        repeat_key = getattr(record, "repeat_key", None)
        if repeat_key and self.stream.isatty():
            try:
                msg = self.format(record)
                if repeat_key == self._repeat_key:
                    self._repeat_count += 1
                else:
                    if self._repeat_key is not None:
                        self.stream.write(self.terminator)
                    self._repeat_key = str(repeat_key)
                    self._repeat_count = 1
                self.stream.write(f"\r{msg}  {DIM}(x{self._repeat_count}){RESET}\033[K")
                self.flush()
            except Exception:
                self.handleError(record)
            return

        if self._repeat_key is not None and self.stream.isatty():
            self.stream.write(self.terminator)
            self._repeat_key = None
            self._repeat_count = 0
        super().emit(record)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("semi_auto_probe")
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        handler = DynamicConsoleHandler(sys.stdout)
        handler.setFormatter(ConsoleFormatter(use_color=sys.stdout.isatty()))
        logger.addHandler(handler)

    return logger


def print_startup_banner() -> None:
    use_color = sys.stdout.isatty()
    logo = LOGO.strip("\n")
    if use_color:
        print(f"\033[38;5;81m{logo}{RESET}")
        print(f"{BOLD}Semi Auto Probe{RESET}  v{__version__}")
    else:
        print(logo)
        print(f"Semi Auto Probe  v{__version__}")
    print("RS-232 three-axis motion control | USB vision preview")
    print("-" * 82)


def colorize_hex_frame(hex_message: str, direction: str = "") -> str:
    parts = hex_message.split()
    if len(parts) not in (12, 33):
        return hex_message

    colored_parts = []
    for index, part in enumerate(parts):
        if index == 0:
            color = FRAME_PART_COLORS["head"]
        elif index == 1:
            color = FRAME_PART_COLORS["function"]
        elif len(parts) == 12 and index == 2:
            color = FRAME_PART_COLORS["axis"]
        elif len(parts) == 12 and 3 <= index <= 8:
            color = FRAME_PART_COLORS["data"]
        elif len(parts) == 33 and 2 <= index <= 29:
            color = FRAME_PART_COLORS["data"]
        elif index == len(parts) - 3:
            color = FRAME_PART_COLORS["checksum"]
        else:
            color = FRAME_PART_COLORS["tail"]
        colored_parts.append(f"{color}{part}{RESET}")

    if not direction:
        return " ".join(colored_parts)

    prefix_color = "\033[38;5;82m" if direction.upper() == "TX" else "\033[38;5;75m"
    return f"{prefix_color}{direction.upper():<3}{RESET} " + " ".join(colored_parts)
