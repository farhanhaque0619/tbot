"""Structured (JSON lines) file logging + readable console logging, with secret redaction."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class RedactFilter(logging.Filter):
    """Replaces any configured secret value with *** in log messages and args."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def _scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            record.msg = self._scrub(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: self._scrub(str(v)) for k, v in record.args.items()}
                else:
                    record.args = tuple(self._scrub(str(a)) for a in record.args)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "event", None)
        if extra:
            payload["event"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(log_dir: Path, level: str = "INFO", secrets: Iterable[str] = (),
                  filename: str = "bot.jsonl") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers when called twice (tests, CLI re-entry).
    for h in list(root.handlers):
        root.removeHandler(h)
    redact = RedactFilter(secrets)

    fh = logging.FileHandler(log_dir / filename, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh.addFilter(redact)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    ch.addFilter(redact)
    root.addHandler(ch)

    # third-party noise
    for noisy in ("urllib3", "requests", "alpaca"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_dir / filename


def log_event(logger: logging.Logger, msg: str, **event) -> None:
    """Log a message with a structured ``event`` dict attached (lands in the JSONL file)."""
    logger.info(msg, extra={"event": event})
