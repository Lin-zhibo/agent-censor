from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .schema import LabelTreeNode


DOMAIN_LABELS = {
    "security": "SECURITY",
    "ecosystem": "ECOSYSTEM",
}

STRUCTURAL_KEYS = {
    "name",
    "introduction",
    "description",
    "label",
    "keywords",
    "keyword",
    "rule_ids",
    "rule_id",
    "default_action",
    "action",
    "action_priority",
    "enabled",
    "threshold",
}


def normalize_label(value: Any, fallback: str = "LABEL") -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return normalized or fallback


def load_settings_file(path: str | Path) -> Mapping[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fp:
        return json.load(fp)


def build_rule_forest(settings: Mapping[str, Any]) -> dict[str, LabelTreeNode]:
    rules = settings.get("rules", settings)
    if not isinstance(rules, Mapping):
        raise TypeError("settings.rules must be a mapping")

    forest: dict[str, LabelTreeNode] = {}
    for config_key, domain_label in DOMAIN_LABELS.items():
        config = rules.get(config_key)
        if isinstance(config, Mapping):
            forest[domain_label] = _build_node(
                key=config_key,
                config=config,
                domain=config_key,
                level=0,
                parent_id="",
                parent_path=(),
                fallback_label=domain_label,
                sibling_index=0,
            )
    return forest


def _build_node(
    key: str,
    config: Mapping[str, Any],
    domain: str,
    level: int,
    parent_id: str,
    parent_path: tuple[str, ...],
    fallback_label: str,
    sibling_index: int,
) -> LabelTreeNode:
    label = normalize_label(config.get("label", key), fallback=fallback_label)
    if level == 0:
        label = fallback_label

    node_id = label if not parent_id else f"{parent_id}.{label}"
    path = parent_path + (label,)
    base = LabelTreeNode(
        node_id=node_id,
        level=level,
        label=label,
        domain=domain,
        display_name=str(config.get("name", key)),
        description=str(config.get("description", config.get("introduction", ""))),
        parent_id=parent_id,
        path=path,
        keywords=_as_tuple(config.get("keywords", config.get("keyword", ()))),
        rule_ids=_as_tuple(config.get("rule_ids", config.get("rule_id", ()))),
        default_action=str(config.get("default_action", config.get("action", ""))).lower(),
        action_priority=_as_int(config.get("action_priority", 0)),
        metadata=_metadata(config),
    )

    children = tuple(
        _build_node(
            key=child_key,
            config=child_config,
            domain=domain,
            level=level + 1,
            parent_id=node_id,
            parent_path=path,
            fallback_label=f"NODE{level + 1}{child_index}",
            sibling_index=child_index,
        )
        for child_index, (child_key, child_config) in enumerate(_child_items(config))
    )
    return replace(base, children=children)


def _child_items(config: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    return [
        (str(key), value)
        for key, value in config.items()
        if key not in STRUCTURAL_KEYS and isinstance(value, Mapping)
    ]


def _metadata(config: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        str(key): value
        for key, value in config.items()
        if key in STRUCTURAL_KEYS and key not in {"name", "introduction", "description"}
    }


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
