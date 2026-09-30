"""Logging setup: human-readable text locally, one JSON object per line in production (FORGE_LOG_FORMAT=json)."""

import json
import logging
import time

from . import config

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update({k: v for k, v in vars(record).items() if k not in _STD})  # `extra=` fields
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup() -> None:
    handler = logging.StreamHandler()
    if config.settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s"))
    logging.basicConfig(level=config.settings.log_level.upper(), handlers=[handler], force=True)
    for noisy in ("httpx", "nats", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
