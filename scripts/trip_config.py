#!/usr/bin/env python3
"""Shared JSON settings for the trip-planner skill.

Every script reads the same settings file, written by the DSH settings panel
(or by ``python scripts/trip_config.py set KEY VALUE`` on other hosts). No
configuration value is read from an environment variable any more; the only
environment lookup is ``DSH_HOME`` to locate the file on disk.

Default location: ``~/.dsh/trip-planner/settings.json`` (``$DSH_HOME/trip-planner``
when DSH_HOME is set). The file is plain JSON, UTF-8, mode 0600.

Usage:
  python3 scripts/trip_config.py status
  python3 scripts/trip_config.py get amapKey
  python3 scripts/trip_config.py set amapKey <key>

The CLI never echoes a value; it only reports whether a key is configured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

VERSION = 1

#: Empty defaults for every supported setting. Secrets stay empty until the
#: user fills them in the settings panel; nothing is baked into the package.
DEFAULTS = {
    "amapKey": "",
    "baiduMapAk": "",
    "dashscopeApiKey": "",
    "siliconflowApiKey": "",
    "mapProvider": "amap",
    "trainLiveCacheDir": "",
    "trainLiveMcpCommand": "",
    "flyaiApiKey": "",
    "flyaiCli": "",
    "windyBase": "https://node.windy.com",
    "windyCache": "windycache.json",
}

#: Keys whose values must never appear in logs, status output or fixtures.
SECRET_KEYS = frozenset({
    "amapKey",
    "baiduMapAk",
    "dashscopeApiKey",
    "siliconflowApiKey",
    "flyaiApiKey",
})

#: Keys that are safe to show as a short "configured / not configured" chip.
STATUS_KEYS = (
    "amapKey",
    "baiduMapAk",
    "dashscopeApiKey",
    "siliconflowApiKey",
    "trainLiveMcpCommand",
    "flyaiApiKey",
    "flyaiCli",
)

KEYS = tuple(DEFAULTS)

# Test seam: tests point this at a temporary file instead of the user's home.
_SETTINGS_PATH: Optional[Path] = None


def settings_dir() -> Path:
    """Directory that owns ``settings.json`` (``$DSH_HOME/trip-planner``)."""
    base = (os.environ.get("DSH_HOME") or "").strip()
    root = Path(base).expanduser().resolve() if base else Path.home() / ".dsh"
    return root / "trip-planner"


def settings_path() -> Path:
    """Absolute path of the settings file."""
    if _SETTINGS_PATH is not None:
        return _SETTINGS_PATH
    return settings_dir() / "settings.json"


def set_settings_path(path: Optional[os.PathLike[str]]) -> None:
    """Override the settings path (used by tests and the settings CLI)."""
    global _SETTINGS_PATH
    _SETTINGS_PATH = Path(path).expanduser() if path is not None else None


def normalize(raw: Any) -> dict[str, str]:
    """Return a complete settings dict with defaults filled in.

    Unknown keys are dropped, non-string values are stringified, every value
    is trimmed, and ``mapProvider`` is constrained to ``amap``/``baidu``.
    """
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            value = ""
        if not isinstance(value, str):
            value = str(value)
        out[key] = value.strip()
    if out["mapProvider"] not in ("amap", "baidu"):
        out["mapProvider"] = "amap"
    return out


def load(path: Optional[os.PathLike[str]] = None) -> dict[str, str]:
    """Read and normalize settings; a missing/corrupt file yields defaults."""
    file = Path(path).expanduser() if path is not None else settings_path()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return normalize(raw)


def get(key: str, default: str | None = None) -> str:
    """Return one setting, or ``default`` when it is absent or empty."""
    value = load().get(key)
    if value is None or value == "":
        return default if default is not None else ""
    return value


def all_settings() -> dict[str, str]:
    """Return every setting with defaults applied."""
    return load()


def status() -> dict[str, Any]:
    """Return a redacted status summary (never contains secret values)."""
    data = load()
    return {
        "version": VERSION,
        "settingsPath": str(settings_path()),
        "configured": {key: bool(data.get(key)) for key in STATUS_KEYS},
        "mapProvider": data["mapProvider"],
    }


def save(data: dict[str, Any], path: Optional[os.PathLike[str]] = None) -> dict[str, str]:
    """Atomically write ``data`` (merged with defaults) to the settings file."""
    file = Path(path).expanduser() if path is not None else settings_path()
    normalized = normalize(data)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = file.with_name(".{}.{}.tmp".format(file.name, os.getpid()))
    try:
        tmp.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, file)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return normalized



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read or write trip-planner settings (JSON, never env vars).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show which keys are configured (values redacted)")
    get_parser = sub.add_parser("get", help="print one value (use with care)")
    get_parser.add_argument("key", choices=KEYS)
    set_parser = sub.add_parser("set", help="set one key; the value is never echoed")
    set_parser.add_argument("key", choices=KEYS)
    set_parser.add_argument("value")
    args = parser.parse_args(argv)

    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "get":
        value = get(args.key)
        if not value:
            print("{} is not configured".format(args.key), file=sys.stderr)
            return 1
        print(value)
        return 0
    if args.command == "set":
        data = load()
        data[args.key] = args.value
        save(data)
        print("saved {} (value not echoed) -> {}".format(args.key, settings_path()))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
