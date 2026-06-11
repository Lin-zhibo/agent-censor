from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .rule_tree import build_rule_forest
from .schema import LabelTreeNode


@dataclass(frozen=True)
class GraphRagNode:
    node_id: str
    node_type: str
    label: str
    title: str
    text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphRagEdge:
    source: str
    target: str
    relation: str


@dataclass(frozen=True)
class _DocumentVector:
    node_id: str
    label: str
    vector: Counter[str]
    norm: float


class LeafGraphRagIndex:
    """Leaf-scoped Graph RAG index.

    This is the local implementation behind the stable Graph RAG contract. It
    treats keywords as knowledge nodes only; it never turns a keyword recall
    into a moderation hit.
    """

    def __init__(
        self,
        nodes: Mapping[str, GraphRagNode],
        edges: Iterable[GraphRagEdge],
        leaf_labels: Iterable[str],
        leaf_documents: Mapping[str, Iterable[str]],
    ):
        self.nodes = dict(nodes)
        self.edges = tuple(edges)
        self.leaf_labels = set(leaf_labels)
        self._leaf_documents = {
            label: tuple(node_ids) for label, node_ids in leaf_documents.items()
        }
        self._vectors = {
            node_id: _document_vector(node_id, node.label, node.text)
            for node_id, node in self.nodes.items()
            if node.node_type in {"Rule", "Sample", "Keyword"} and node.text.strip()
        }

    @classmethod
    def from_files(
        cls,
        settings_path: str | Path = "config/settings.json",
        knowledge_path: str | Path = "data/knowledge.json",
    ) -> "LeafGraphRagIndex":
        with Path(settings_path).open("r", encoding="utf-8") as fp:
            settings = json.load(fp)
        with Path(knowledge_path).open("r", encoding="utf-8") as fp:
            knowledge = json.load(fp)
        return cls.from_objects(settings=settings, knowledge=knowledge)

    @classmethod
    def from_objects(
        cls,
        settings: Mapping[str, Any],
        knowledge: Mapping[str, Any],
    ) -> "LeafGraphRagIndex":
        forest = build_rule_forest(settings)
        knowledge_rules = knowledge.get("rules", {})
        if not isinstance(knowledge_rules, Mapping):
            raise TypeError("knowledge.rules must be a mapping")

        nodes: dict[str, GraphRagNode] = {}
        edges: list[GraphRagEdge] = []
        leaf_labels: set[str] = set()
        leaf_documents: dict[str, list[str]] = defaultdict(list)

        settings_leaf_labels: set[str] = set()
        for domain_root in forest.values():
            for child in domain_root.children:
                _collect_leaf_labels(child, settings_leaf_labels)

        extra_rules = sorted(set(knowledge_rules) - settings_leaf_labels)
        if extra_rules:
            raise ValueError(
                "knowledge contains labels outside settings leaf nodes: "
                + ", ".join(extra_rules[:10])
            )

        knowledge_labels = set(knowledge_rules)

        for domain_root in forest.values():
            domain_id = f"Domain:{domain_root.label}"
            nodes[domain_id] = GraphRagNode(
                node_id=domain_id,
                node_type="Domain",
                label=domain_root.label,
                title=domain_root.display_name or domain_root.label,
                metadata={"domain": domain_root.domain},
            )
            for child in domain_root.children:
                _add_tree_node(
                    node=child,
                    parent_id=domain_id,
                    nodes=nodes,
                    edges=edges,
                    leaf_labels=leaf_labels,
                    knowledge_labels=knowledge_labels,
                )

        for label in sorted(leaf_labels):
            raw_rule = knowledge_rules.get(label)
            if not isinstance(raw_rule, Mapping):
                continue
            label_id = f"Label:{label}"
            introduction = str(raw_rule.get("introduction", "")).strip()
            if introduction:
                rule_id = f"Rule:{label}"
                nodes[rule_id] = GraphRagNode(
                    node_id=rule_id,
                    node_type="Rule",
                    label=label,
                    title=f"{label} rule",
                    text=introduction,
                    metadata={"source_field": "introduction"},
                )
                edges.append(GraphRagEdge(label_id, rule_id, "HAS_RULE"))
                leaf_documents[label].append(rule_id)

            for index, sample in enumerate(_as_text_items(raw_rule.get("samples")), 1):
                sample_id = f"Sample:{label}:{index:03d}"
                nodes[sample_id] = GraphRagNode(
                    node_id=sample_id,
                    node_type="Sample",
                    label=label,
                    title=f"{label} sample {index}",
                    text=sample,
                    metadata={"source_field": "samples", "index": index},
                )
                edges.append(GraphRagEdge(label_id, sample_id, "HAS_SAMPLE"))
                leaf_documents[label].append(sample_id)

            for index, keyword in enumerate(_as_text_items(raw_rule.get("keywords")), 1):
                keyword_id = f"Keyword:{label}:{index:03d}"
                nodes[keyword_id] = GraphRagNode(
                    node_id=keyword_id,
                    node_type="Keyword",
                    label=label,
                    title=keyword,
                    text=keyword,
                    metadata={"source_field": "keywords", "index": index},
                )
                edges.append(GraphRagEdge(label_id, keyword_id, "HAS_KEYWORD"))
                leaf_documents[label].append(keyword_id)

        return cls(
            nodes=nodes,
            edges=edges,
            leaf_labels=leaf_labels,
            leaf_documents=leaf_documents,
        )

    def search(
        self,
        query: str,
        labels: list[str],
        top_k: int = 5,
        max_depth: int = 2,
    ) -> dict[str, Any]:
        label = labels[0] if labels else ""
        if len(labels) != 1 or label not in self.leaf_labels:
            return {
                "hits": [],
                "paths": [],
                "evidence_summary": "Graph RAG 只接受一个叶子标签作为检索范围。",
                "scope": {"labels": list(labels), "leaf_only": True},
            }

        query_vector = _text_vector(query)
        query_norm = _norm(query_vector)
        if query_norm == 0:
            return {
                "hits": [],
                "paths": [],
                "evidence_summary": "待审核文本为空，Graph RAG 未召回证据。",
                "scope": {"labels": [label], "max_depth": max_depth},
            }

        scored: list[tuple[float, GraphRagNode]] = []
        for node_id in self._leaf_documents.get(label, ()):
            vector = self._vectors.get(node_id)
            node = self.nodes.get(node_id)
            if vector is None or node is None:
                continue
            score = _cosine(query_vector, query_norm, vector)
            if score > 0:
                scored.append((score, node))

        scored.sort(
            key=lambda item: (
                item[0],
                _node_type_rank(item[1].node_type),
                item[1].node_id,
            ),
            reverse=True,
        )
        selected = scored[: max(top_k, 0)]

        hits = [
            {
                "node_id": node.node_id,
                "node_type": node.node_type,
                "label": node.label,
                "title": node.title,
                "similarity": round(score, 4),
                "confidence": round(score, 4),
                "summary": _summary_for(node, score),
            }
            for score, node in selected
        ]
        paths = [
            {
                "path": [f"Label:{label}", node.node_id],
                "score": round(score, 4),
            }
            for score, node in selected
        ]
        return {
            "hits": hits,
            "paths": paths,
            "evidence_summary": _evidence_summary(label, selected),
            "scope": {"labels": [label], "max_depth": max_depth, "leaf_only": True},
        }

    def stats(self) -> dict[str, int]:
        by_type = Counter(node.node_type for node in self.nodes.values())
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "leaf_labels": len(self.leaf_labels),
            "documents": len(self._vectors),
            "domains": by_type.get("Domain", 0),
            "categories": by_type.get("Category", 0),
            "labels": by_type.get("Label", 0),
            "rules": by_type.get("Rule", 0),
            "samples": by_type.get("Sample", 0),
            "keywords": by_type.get("Keyword", 0),
        }


def _add_tree_node(
    node: LabelTreeNode,
    parent_id: str,
    nodes: dict[str, GraphRagNode],
    edges: list[GraphRagEdge],
    leaf_labels: set[str],
    knowledge_labels: set[str],
) -> None:
    if node.is_leaf and node.label not in knowledge_labels:
        return
    node_type = "Label" if node.is_leaf else "Category"
    graph_id = f"{node_type}:{node.label}"
    nodes[graph_id] = GraphRagNode(
        node_id=graph_id,
        node_type=node_type,
        label=node.label,
        title=node.display_name or node.label,
        text=node.description,
        metadata={
            "domain": node.domain,
            "path": list(node.path),
            "node_id": node.node_id,
        },
    )
    edges.append(GraphRagEdge(parent_id, graph_id, "HAS_CHILD"))
    if node.is_leaf:
        leaf_labels.add(node.label)
        return
    for child in node.children:
        _add_tree_node(
            node=child,
            parent_id=graph_id,
            nodes=nodes,
            edges=edges,
            leaf_labels=leaf_labels,
            knowledge_labels=knowledge_labels,
        )


def _collect_leaf_labels(node: LabelTreeNode, leaf_labels: set[str]) -> None:
    if node.is_leaf:
        leaf_labels.add(node.label)
        return
    for child in node.children:
        _collect_leaf_labels(child, leaf_labels)


def _as_text_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _document_vector(node_id: str, label: str, text: str) -> _DocumentVector:
    vector = _text_vector(text)
    return _DocumentVector(
        node_id=node_id,
        label=label,
        vector=vector,
        norm=_norm(vector),
    )


def _text_vector(text: str) -> Counter[str]:
    compact = re.sub(r"\s+", "", text.lower())
    features: Counter[str] = Counter()
    for token in re.findall(r"[a-z0-9_]+", compact):
        features[f"w:{token}"] += 2
    for ngram_size in (2, 3):
        if len(compact) < ngram_size:
            continue
        for index in range(len(compact) - ngram_size + 1):
            features[f"c{ngram_size}:{compact[index:index + ngram_size]}"] += 1
    return features


def _norm(vector: Counter[str]) -> float:
    return math.sqrt(sum(value * value for value in vector.values()))


def _cosine(
    query_vector: Counter[str],
    query_norm: float,
    document: _DocumentVector,
) -> float:
    if query_norm == 0 or document.norm == 0:
        return 0.0
    if len(query_vector) > len(document.vector):
        query_vector, doc_vector = document.vector, query_vector
    else:
        doc_vector = document.vector
    dot = sum(value * doc_vector.get(term, 0) for term, value in query_vector.items())
    return dot / (query_norm * document.norm)


def _node_type_rank(node_type: str) -> int:
    return {"Rule": 3, "Sample": 2, "Keyword": 1}.get(node_type, 0)


def _summary_for(node: GraphRagNode, score: float) -> str:
    text = node.text.strip().replace("\n", " ")
    if len(text) > 80:
        text = text[:77] + "..."
    return f"{node.node_type} evidence for {node.label}; similarity={score:.2f}; {text}"


def _evidence_summary(label: str, selected: list[tuple[float, GraphRagNode]]) -> str:
    if not selected:
        return f"未在 {label} 叶子邻域召回 Graph RAG 证据。"
    counts = Counter(node.node_type for _, node in selected)
    parts = [f"{node_type} {count}" for node_type, count in sorted(counts.items())]
    return f"召回 {label} 叶子邻域证据：" + "、".join(parts) + "。"
