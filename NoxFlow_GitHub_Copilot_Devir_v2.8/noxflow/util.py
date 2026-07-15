from __future__ import annotations
from pathlib import Path
from typing import Callable
import json, os, re, threading, time

class StopRequested(RuntimeError):
    pass

class EventLog:
    def __init__(self, path: Path, callback: Callable[[str], None] | None = None, max_bytes: int = 4_000_000):
        self.path = path
        self.callback = callback
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                backup = self.path.with_suffix(self.path.suffix + '.1')
                if backup.exists(): backup.unlink()
                self.path.replace(backup)
        except OSError:
            pass

    def __call__(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        with self.lock:
            self._rotate()
            with self.path.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
        if self.callback:
            self.callback(line)


def load_json(path: Path, default):
    try:
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def safe_instance_name(value: str) -> str:
    result = re.sub(r'[^A-Za-z0-9_-]+', '_', (value or '').strip()).strip('_')
    if not result:
        raise ValueError('Nox kopya adı boş olamaz.')
    return result[:48]


def interruptible_sleep(seconds: float, stop_event: threading.Event, granularity: float = 0.25) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if stop_event.is_set():
            raise StopRequested('Durdurma istendi.')
        time.sleep(min(granularity, max(0.0, deadline-time.monotonic())))
