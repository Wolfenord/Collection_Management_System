"""Layered deployment configuration for the CMS project.

Values are resolved with the following precedence (first hit wins):

  1. Environment variable (incl. anything loaded from ``.env`` via python-dotenv)
  2. ``config.ini`` in the project root (path overridable via ``CMS_CONFIG_FILE``)
  3. The code default passed by the caller

The INI file uses sections purely for readability — all keys are flattened and
matched case-insensitively against the environment-variable name, so
``[database] db_engine = postgres`` and ``DB_ENGINE=postgres`` configure the
same thing. See ``config.example.ini`` for a documented template.

This module is imported by ``settings.py`` and must therefore not import any
Django machinery.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

_TRUTHY = {'1', 'true', 'yes', 'on'}


def _load_ini() -> dict[str, str]:
    """Flatten every ``[section] key = value`` of the INI file into one mapping."""
    path = Path(os.environ.get('CMS_CONFIG_FILE', BASE_DIR / 'config.ini'))
    if not path.is_file():
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding='utf-8')
    return {
        key.upper(): value
        for section in parser.sections()
        for key, value in parser.items(section)
    }


_INI_VALUES = _load_ini()


def get(name: str, default: str = '') -> str:
    """Resolve a string setting: environment > config.ini > ``default``."""
    value = os.environ.get(name)
    if value is not None:
        return value
    return _INI_VALUES.get(name.upper(), default)


def get_bool(name: str, default: bool = False) -> bool:
    return get(name, str(default)).strip().lower() in _TRUTHY


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def get_list(name: str, default: str = '') -> list[str]:
    """Comma-separated value → list of stripped, non-empty entries."""
    return [part.strip() for part in get(name, default).split(',') if part.strip()]
