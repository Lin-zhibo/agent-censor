from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping


LOGGER_NAME = "contentsec.agent"
LOG_SCHEMA_VERSION = "1.0"
DEFAULT_LOG_PATH = Path("log") / "agent.log"
LOG_PATH_ENV = "AGENT_LOG_PATH"
LOG_FORMAT_ENV = "AGENT_LOG_FORMAT"
MAX_BYTES_ENV = "AGENT_LOG_MAX_BYTES"
BACKUP_COUNT_ENV = "AGENT_LOG_BACKUP_COUNT"

_HANDLER_MARK = "_contentsec_agent_handler"
_LOGGER_PATH_ATTR = "_contentsec_agent_log_path"
_LOGGER_FORMAT_ATTR = "_contentsec_agent_log_format"
_LOGGER_LOCK = threading.RLock()

_READABLE_FIELD_ORDER = (
    "trace_id",
    "agent",
    "tool_name",
    "node_label",
    "node_id",
    "child_label",
    "child_agent",
    "decision",
    "status",
    "ok",
    "hit",
    "hit_label",
    "needs_review",
    "action",
    "duration_ms",
    "tool_latency_ms",
    "selected_child_labels",
    "visible_child_labels",
    "security_labels",
    "ecosystem_labels",
    "root_child_count",
    "child_count",
    "selected_count",
    "hit_count",
    "audit_event_count",
    "error_count",
    "error_codes",
    "modality",
    "text_length",
    "policy_id",
    "policy_version",
    "tenant_id",
    "business_id",
)

_READABLE_FIELD_ALIASES = {
    "trace_id": "trace",
    "node_label": "node",
    "duration_ms": "duration",
    "tool_latency_ms": "tool_latency",
    "selected_child_labels": "selected",
    "visible_child_labels": "visible",
    "security_labels": "sec",
    "ecosystem_labels": "eco",
    "text_length": "text_len",
}

_READABLE_SKIP_FIELDS = {
    "event",
    "detail_level",
    "labels_requested",
    "labels_requested_count",
    "candidate_models_count",
    "path",
    "is_leaf",
    "domain",
    "node_level",
    "child_node_id",
    "child_level",
    "child_is_leaf",
    "response_trace_id",
}


class ReadableAgentFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event_fields = _event_fields(record)
        event = str(event_fields.get("event") or record.getMessage())
        timestamp = (
            datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds")
        )
        parts = [timestamp, record.levelname, event]
        parts.extend(_readable_field_parts(event_fields))
        if record.exc_info:
            parts.append(f"exception={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "log_schema_version": LOG_SCHEMA_VERSION,
            "process_id": record.process,
            "thread_name": record.threadName,
        }
        payload.update(_event_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def get_agent_logger() -> logging.Logger:
    return configure_agent_logger()


def configure_agent_logger(path: str | os.PathLike[str] | None = None) -> logging.Logger:
    log_path = _resolve_log_path(path)
    log_format = _resolve_log_format()
    with _LOGGER_LOCK:
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        current_path = getattr(logger, _LOGGER_PATH_ATTR, None)
        current_format = getattr(logger, _LOGGER_FORMAT_ATTR, None)
        if (
            current_path == str(log_path)
            and current_format == log_format
            and _has_agent_handler(logger)
        ):
            return logger

        _remove_agent_handlers(logger)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=_env_int(MAX_BYTES_ENV, 10 * 1024 * 1024),
            backupCount=_env_int(BACKUP_COUNT_ENV, 5),
            encoding="utf-8",
        )
        setattr(handler, _HANDLER_MARK, True)
        handler.setFormatter(_make_formatter(log_format))
        logger.addHandler(handler)
        setattr(logger, _LOGGER_PATH_ATTR, str(log_path))
        setattr(logger, _LOGGER_FORMAT_ATTR, log_format)
        return logger


def log_agent_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    try:
        payload: dict[str, Any] = {"event": event}
        payload.update(
            {key: value for key, value in fields.items() if value is not None}
        )
        get_agent_logger().log(level, event, extra={"agent_event": payload})
    except Exception:
        # Agent logging must never interrupt an audit request.
        return


def reset_agent_logger() -> None:
    with _LOGGER_LOCK:
        logger = logging.getLogger(LOGGER_NAME)
        _remove_agent_handlers(logger)
        if hasattr(logger, _LOGGER_PATH_ATTR):
            delattr(logger, _LOGGER_PATH_ATTR)
        if hasattr(logger, _LOGGER_FORMAT_ATTR):
            delattr(logger, _LOGGER_FORMAT_ATTR)


def _resolve_log_path(path: str | os.PathLike[str] | None = None) -> Path:
    configured = path or os.getenv(LOG_PATH_ENV) or DEFAULT_LOG_PATH
    return Path(configured)


def _resolve_log_format() -> str:
    value = os.getenv(LOG_FORMAT_ENV, "text").strip().lower()
    return "json" if value in {"json", "jsonl"} else "text"


def _make_formatter(log_format: str) -> logging.Formatter:
    if log_format == "json":
        return JsonLineFormatter()
    return ReadableAgentFormatter()


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _has_agent_handler(logger: logging.Logger) -> bool:
    return any(getattr(handler, _HANDLER_MARK, False) for handler in logger.handlers)


def _remove_agent_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if not getattr(handler, _HANDLER_MARK, False):
            continue
        logger.removeHandler(handler)
        handler.close()


def _event_fields(record: logging.LogRecord) -> dict[str, Any]:
    event_payload = getattr(record, "agent_event", None)
    if isinstance(event_payload, Mapping):
        return dict(_json_safe(event_payload))
    if event_payload:
        return {"event": str(event_payload)}
    return {"event": record.getMessage()}


def _readable_field_parts(fields: Mapping[str, Any]) -> list[str]:
    emitted: set[str] = set()
    parts: list[str] = []
    for key in _READABLE_FIELD_ORDER:
        if key not in fields:
            continue
        if not _should_emit(fields[key]):
            continue
        parts.append(_readable_pair(key, fields[key]))
        emitted.add(key)
    for key in sorted(fields):
        if key in emitted or key in _READABLE_SKIP_FIELDS:
            continue
        if not _should_emit(fields[key]):
            continue
        parts.append(_readable_pair(key, fields[key]))
    return parts


def _readable_pair(key: str, value: Any) -> str:
    alias = _READABLE_FIELD_ALIASES.get(key, key)
    if key.endswith("_ms"):
        return f"{alias}={value}ms"
    return f"{alias}={_format_readable_value(value)}"


def _format_readable_value(value: Any) -> str:
    if isinstance(value, list):
        return _format_readable_list(value)
    if isinstance(value, tuple):
        return _format_readable_list(list(value))
    if isinstance(value, Mapping):
        return _quote(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _quote(str(value))


def _format_readable_list(values: list[Any]) -> str:
    if not values:
        return "[]"
    visible = [_format_list_item(value) for value in values[:8]]
    if len(values) > 8:
        visible.append(f"+{len(values) - 8}")
    return "[" + ",".join(visible) + "]"


def _format_list_item(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _quote(str(value))
    return _quote(json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":")))


def _quote(value: str) -> str:
    if not value:
        return '""'
    if any(char.isspace() for char in value) or any(char in value for char in "=[]{},"):
        return json.dumps(value, ensure_ascii=False)
    return value


def _should_emit(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    return True


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=str)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
