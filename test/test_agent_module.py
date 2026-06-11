import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

from core.agent import (
    AuditContext,
    LeafLabelHit,
    LeafGraphRagIndex,
    MultiAgentModerator,
    ToolResponse,
    build_rule_forest,
)
from core.agent.logging import reset_agent_logger


class OrderedToolbox:
    def __init__(self):
        self.calls: list[str] = []
        self.last_graph_rag_result = None

    def route_children(self, context, node, child_nodes):
        self.calls.append("route_children")
        return ToolResponse(
            status="success",
            data={
                "child_labels": [child.label for child in child_nodes],
                "reason": "route all",
            },
            trace_id=context.trace_id,
        )

    def model_route(self, context):
        self.calls.append("model_route")
        return ToolResponse(status="success", data={"selected_model": "stub"}, trace_id=context.trace_id)

    def run_model_inference(self, context, route_decision):
        self.calls.append("run_model_inference")
        return ToolResponse(status="success", data={"labels": [], "evidence": []}, trace_id=context.trace_id)

    def query_policy_rule(self, context, node):
        self.calls.append("query_policy_rule")
        return ToolResponse(status="success", data={"rules": []}, trace_id=context.trace_id)

    def evaluate_rule(self, context, node):
        self.calls.append("evaluate_rule")
        return ToolResponse(status="success", data={"hit": False}, trace_id=context.trace_id)

    def graph_rag_search(self, context, labels):
        self.calls.append("graph_rag_search")
        return ToolResponse(
            status="success",
            data={"hits": [], "paths": [], "evidence_summary": "ordered rag"},
            trace_id=context.trace_id,
        )

    def audit_trace_lookup(self, trace_id):
        self.calls.append("audit_trace_lookup")
        return ToolResponse(status="success", data={"audit_trace": []}, trace_id=trace_id)

    def threshold_preview(self, context, threshold_overrides):
        self.calls.append("threshold_preview")
        return ToolResponse(status="success", data={"preview_only": True}, trace_id=context.trace_id)

    def evaluate_leaf(self, context, node, graph_rag_result=None):
        self.calls.append("evaluate_leaf")
        self.last_graph_rag_result = graph_rag_result
        return LeafLabelHit(label=node.label, reason=f"hit {node.label}", domain=node.domain, node_id=node.node_id)


class ScriptedToolbox(OrderedToolbox):
    def __init__(
        self,
        routes: dict[str, list[str]] | None = None,
        hit_labels: set[str] | None = None,
    ):
        super().__init__()
        self.routes = routes or {}
        self.hit_labels = hit_labels or set()

    def route_children(self, context, node, child_nodes):
        self.calls.append("route_children")
        key = node.label if node is not None else "ROOT"
        child_labels = self.routes.get(key, [child.label for child in child_nodes])
        valid = {child.label for child in child_nodes}
        return ToolResponse(
            status="success",
            data={
                "child_labels": [label for label in child_labels if label in valid],
                "reason": "scripted route",
            },
            trace_id=context.trace_id,
        )

    def evaluate_leaf(self, context, node, graph_rag_result=None):
        self.calls.append("evaluate_leaf")
        self.last_graph_rag_result = graph_rag_result
        if node.label not in self.hit_labels:
            return LeafLabelHit(label="", reason="未检出问题", domain=node.domain, node_id=node.node_id)
        return LeafLabelHit(label=node.label, reason=f"hit {node.label}", domain=node.domain, node_id=node.node_id)


class DebugRouteToolbox(ScriptedToolbox):
    def route_children(self, context, node, child_nodes):
        self.calls.append("route_children")
        key = node.label if node is not None else "ROOT"
        raw_labels = self.routes.get(key, [child.label for child in child_nodes])
        valid = [child.label for child in child_nodes]
        selected = [label for label in raw_labels if label in valid]
        rejected = [label for label in raw_labels if label not in valid]
        return ToolResponse(
            status="success",
            data={
                "child_labels": selected,
                "reason": "debug route",
                "raw_child_labels": raw_labels,
                "rejected_child_labels": rejected,
                "valid_child_labels": valid,
            },
            trace_id=context.trace_id,
        )


class MultiAgentModuleTest(unittest.TestCase):
    def test_builds_security_and_ecosystem_trees_from_settings(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "name": "Porn",
                        "nudity": {
                            "name": "Nudity",
                            "default_action": "ban",
                        },
                    }
                },
                "ecosystem": {
                    "ad": {
                        "spam": {
                            "default_action": "limit",
                        }
                    }
                },
            }
        }

        forest = build_rule_forest(settings)

        self.assertEqual(forest["SECURITY"].label, "SECURITY")
        self.assertEqual(forest["SECURITY"].domain, "security")
        self.assertEqual(forest["SECURITY"].children[0].label, "PORN")
        self.assertEqual(forest["SECURITY"].children[0].children[0].label, "NUDITY")
        self.assertEqual(forest["ECOSYSTEM"].children[0].children[0].label, "SPAM")

    def test_moderator_requires_explicit_toolbox(self):
        settings = {
            "rules": {
                "security": {"porn": {"nudity": {}}},
                "ecosystem": {},
            }
        }

        with self.assertRaises(ValueError):
            MultiAgentModerator.from_settings(settings)

    def test_root_preserves_child_labels_and_security_decision_priority(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {
                            "default_action": "ban",
                        }
                    }
                },
                "ecosystem": {
                    "ad": {
                        "spam": {
                            "default_action": "limit",
                        }
                    }
                },
            }
        }
        moderator = MultiAgentModerator.from_settings(
            settings,
            toolbox=ScriptedToolbox(hit_labels={"NUDITY", "SPAM"}),
        )

        result = moderator.moderate(
            AuditContext(
                trace_id="trace_1",
                content={"text": "naked promo"},
            )
        )

        self.assertEqual(result.security_labels, ["NUDITY"])
        self.assertEqual(result.ecosystem_labels, ["SPAM"])
        self.assertEqual(result.decision, "ban")
        self.assertEqual(result.root_result.child_labels, ["PORN", "AD"])

    def test_intermediate_agent_does_not_dedupe_or_sort_labels(self):
        settings = {
            "rules": {
                "security": {
                    "group": {
                        "label-a": {},
                        "label_a": {},
                    }
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(
            settings,
            toolbox=ScriptedToolbox(hit_labels={"LABELA"}),
        )

        result = moderator.moderate(
            AuditContext(trace_id="trace_2", content={"text": "first second"})
        )

        self.assertEqual(result.security_labels, ["LABELA", "LABELA"])
        self.assertEqual(result.decision, "ban")
        self.assertEqual(result.root_result.child_labels, ["GROUP"])

    def test_external_toolbox_can_hit_leaf_without_local_matching(self):
        settings = {
            "rules": {
                "security": {
                    "minor": {
                        "risk": {
                            "default_action": "reject",
                        }
                    }
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(
            settings,
            toolbox=ScriptedToolbox(hit_labels={"RISK"}),
        )

        result = moderator.moderate(
            AuditContext(
                trace_id="trace_3",
                metadata={"source": "external rule engine"},
            )
        )

        self.assertEqual(result.security_labels, ["RISK"])
        self.assertEqual(result.reason, "hit RISK")
        self.assertEqual(result.decision, "ban")

    def test_labels_requested_do_not_count_as_hits(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {
                            "default_action": "ban",
                        }
                    }
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(settings, toolbox=ScriptedToolbox())

        result = moderator.moderate(
            AuditContext(trace_id="trace_4", labels_requested=["NUDITY"])
        )

        self.assertEqual(result.security_labels, [])
        self.assertEqual(result.decision, "pass")

    def test_route_only_selects_matching_first_level_branch(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {},
                    },
                    "violence": {
                        "injury": {},
                    },
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(
            settings,
            toolbox=ScriptedToolbox(
                routes={"ROOT": ["PORN"], "PORN": ["NUDITY"]},
                hit_labels={"NUDITY"},
            ),
        )

        result = moderator.moderate(
            AuditContext(trace_id="trace_5", content={"text": "naked only"})
        )

        self.assertEqual(result.root_result.child_labels, ["PORN"])
        self.assertEqual(result.security_labels, ["NUDITY"])

    def test_route_stage_reasons_are_recorded_in_audit_trace(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {},
                    },
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(
            settings,
            toolbox=ScriptedToolbox(
                routes={"ROOT": ["PORN"], "PORN": ["NUDITY"]},
                hit_labels={"NUDITY"},
            ),
        )

        result = moderator.moderate(
            AuditContext(trace_id="trace_route_reason", content={"text": "naked"})
        )

        route_events = [
            event
            for event in result.audit_trace
            if "child_labels" in event and "child_count" in event
        ]
        root_route = next(
            event
            for event in route_events
            if event["agent"] == "RootAgent" and event["node_id"] == "ROOT"
        )
        porn_route = next(
            event for event in route_events if event["agent"] == "PornAgent"
        )
        self.assertEqual(root_route["child_labels"], ["PORN"])
        self.assertEqual(root_route["reason"], "scripted route")
        self.assertEqual(porn_route["child_labels"], ["NUDITY"])
        self.assertEqual(porn_route["reason"], "scripted route")

    def test_route_stage_records_rejected_child_labels_for_debugging(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {},
                    },
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(
            settings,
            toolbox=DebugRouteToolbox(
                routes={"ROOT": ["SEX_SERVICE", "PORN"], "PORN": ["NUDITY"]},
                hit_labels={"NUDITY"},
            ),
        )

        result = moderator.moderate(
            AuditContext(trace_id="trace_rejected_route", content={"text": "裸聊上门"})
        )

        root_route = next(
            event
            for event in result.audit_trace
            if event["agent"] == "RootAgent" and event["node_id"] == "ROOT"
        )
        self.assertEqual(root_route["child_labels"], ["PORN"])
        self.assertEqual(root_route["raw_child_labels"], ["SEX_SERVICE", "PORN"])
        self.assertEqual(root_route["rejected_child_labels"], ["SEX_SERVICE"])
        self.assertEqual(root_route["valid_child_labels"], ["PORN"])

    def test_moderate_dict_input_without_trace_id_uses_default(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {},
                    }
                }
            }
        }
        moderator = MultiAgentModerator.from_settings(
            settings,
            toolbox=ScriptedToolbox(hit_labels={"NUDITY"}),
        )

        result = moderator.moderate({"content": {"text": "naked"}})

        self.assertEqual(result.security_labels, ["NUDITY"])
        self.assertEqual(result.audit_trace[-1]["agent"], "RootAgent")

    def test_audit_context_text_prefers_content_text(self):
        context = AuditContext(trace_id="trace_6", content={"text": "hello"})
        self.assertEqual(context.text, "hello")

        text_context = AuditContext(trace_id="trace_7", content="plain")
        self.assertEqual(text_context.text, "plain")

    def test_leaf_agent_invokes_tools_in_documented_order(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {},
                    }
                }
            }
        }
        toolbox = OrderedToolbox()
        moderator = MultiAgentModerator.from_settings(settings, toolbox=toolbox)

        result = moderator.moderate(AuditContext(trace_id="trace_8", content={"text": "naked"}))

        self.assertEqual(result.security_labels, ["NUDITY"])
        self.assertEqual(
            toolbox.calls,
            [
                "route_children",
                "route_children",
                "model_route",
                "run_model_inference",
                "query_policy_rule",
                "evaluate_rule",
                "graph_rag_search",
                "evaluate_leaf",
            ],
        )
        self.assertIsNotNone(toolbox.last_graph_rag_result)
        self.assertEqual(
            toolbox.last_graph_rag_result.data["evidence_summary"],
            "ordered rag",
        )

    def test_leaf_graph_rag_builds_leaf_scoped_evidence(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "sex_service": {
                            "label": "SEX_SERVICE",
                            "name": "色情服务",
                        },
                        "sex_dating": {
                            "label": "SEX_DATING",
                            "name": "色情交友",
                        },
                    }
                },
                "ecosystem": {},
            }
        }
        knowledge = {
            "rules": {
                "SEX_SERVICE": {
                    "label": "SEX_SERVICE",
                    "introduction": "招嫖、色情按摩、陪侍、上门服务。",
                    "samples": ["摸摸唱技师全国可飞，先付后做。"],
                    "keywords": ["摸摸唱", "技师"],
                },
                "SEX_DATING": {
                    "label": "SEX_DATING",
                    "introduction": "约炮、裸聊等色情交友。",
                    "samples": ["裸聊约炮。"],
                    "keywords": ["裸聊"],
                },
            }
        }

        index = LeafGraphRagIndex.from_objects(settings=settings, knowledge=knowledge)
        data = index.search("摸摸唱技师上门服务", ["SEX_SERVICE"], top_k=5)

        self.assertEqual(index.stats()["labels"], 2)
        self.assertEqual(index.stats()["keywords"], 3)
        self.assertGreaterEqual(len(data["hits"]), 1)
        self.assertTrue(all(hit["label"] == "SEX_SERVICE" for hit in data["hits"]))
        self.assertTrue(all("Label:SEX_SERVICE" in path["path"][0] for path in data["paths"]))
        self.assertNotIn("decision", data)
        self.assertNotIn("matched", data)

    def test_leaf_graph_rag_rejects_non_leaf_or_multi_label_scope(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "sex_service": {"label": "SEX_SERVICE"},
                    }
                },
                "ecosystem": {},
            }
        }
        knowledge = {
            "rules": {
                "SEX_SERVICE": {
                    "label": "SEX_SERVICE",
                    "introduction": "色情服务。",
                    "samples": ["上门服务。"],
                    "keywords": ["技师"],
                },
            }
        }

        index = LeafGraphRagIndex.from_objects(settings=settings, knowledge=knowledge)

        multi = index.search("技师", ["SEX_SERVICE", "PORN"])
        parent = index.search("技师", ["PORN"])

        self.assertEqual(multi["hits"], [])
        self.assertTrue(multi["scope"]["leaf_only"])
        self.assertEqual(parent["hits"], [])
        self.assertTrue(parent["scope"]["leaf_only"])

    def test_graph_rag_search_is_only_called_by_leaf_agent(self):
        source = Path("core/agent/agents.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = []

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_ClassDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "graph_rag_search":
                    calls.append(".".join(self.stack))
                self.generic_visit(node)

        Visitor().visit(tree)

        self.assertEqual(calls, ["LeafAgent.run"])

    def test_moderate_writes_readable_agent_log_by_default(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {"default_action": "ban"},
                    }
                },
                "ecosystem": {},
            }
        }
        old_log_path = os.environ.get("AGENT_LOG_PATH")
        old_log_format = os.environ.get("AGENT_LOG_FORMAT")

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "agent.log"
            os.environ["AGENT_LOG_PATH"] = str(log_path)
            os.environ.pop("AGENT_LOG_FORMAT", None)
            reset_agent_logger()
            try:
                moderator = MultiAgentModerator.from_settings(
                    settings,
                    toolbox=ScriptedToolbox(hit_labels={"NUDITY"}),
                )
                result = moderator.moderate(
                    AuditContext(trace_id="trace_log", content={"text": "naked secret"})
                )
                reset_agent_logger()

                lines = log_path.read_text(encoding="utf-8").splitlines()
            finally:
                reset_agent_logger()
                if old_log_path is None:
                    os.environ.pop("AGENT_LOG_PATH", None)
                else:
                    os.environ["AGENT_LOG_PATH"] = old_log_path
                if old_log_format is None:
                    os.environ.pop("AGENT_LOG_FORMAT", None)
                else:
                    os.environ["AGENT_LOG_FORMAT"] = old_log_format

        self.assertTrue(any(" INFO audit.started " in line for line in lines))
        self.assertTrue(any(" INFO agent.route.completed " in line for line in lines))
        self.assertTrue(any(" INFO tool.call.completed " in line for line in lines))
        self.assertTrue(any(" INFO agent.leaf.completed " in line for line in lines))
        self.assertTrue(any(" INFO audit.completed " in line for line in lines))
        self.assertFalse(any("naked secret" in line for line in lines))
        self.assertFalse(any(line.lstrip().startswith("{") for line in lines))

        completed = [line for line in lines if " INFO audit.completed " in line][-1]
        self.assertEqual(result.decision, "ban")
        self.assertIn("trace=trace_log", completed)
        self.assertIn("decision=ban", completed)
        self.assertIn("sec=[NUDITY]", completed)
        self.assertTrue(
            any("tool_name=route_children" in line for line in lines)
        )
        self.assertTrue(any("tool_name=evaluate_leaf" in line for line in lines))

    def test_agent_log_supports_json_lines_format(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {"default_action": "ban"},
                    }
                },
                "ecosystem": {},
            }
        }
        old_log_path = os.environ.get("AGENT_LOG_PATH")
        old_log_format = os.environ.get("AGENT_LOG_FORMAT")

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "agent.jsonl"
            os.environ["AGENT_LOG_PATH"] = str(log_path)
            os.environ["AGENT_LOG_FORMAT"] = "json"
            reset_agent_logger()
            try:
                moderator = MultiAgentModerator.from_settings(
                    settings,
                    toolbox=ScriptedToolbox(hit_labels={"NUDITY"}),
                )
                result = moderator.moderate(
                    AuditContext(trace_id="trace_json", content={"text": "naked secret"})
                )
                reset_agent_logger()

                lines = log_path.read_text(encoding="utf-8").splitlines()
                records = [json.loads(line) for line in lines]
            finally:
                reset_agent_logger()
                if old_log_path is None:
                    os.environ.pop("AGENT_LOG_PATH", None)
                else:
                    os.environ["AGENT_LOG_PATH"] = old_log_path
                if old_log_format is None:
                    os.environ.pop("AGENT_LOG_FORMAT", None)
                else:
                    os.environ["AGENT_LOG_FORMAT"] = old_log_format

        events = [record["event"] for record in records]
        self.assertIn("audit.completed", events)
        completed = [
            record for record in records if record["event"] == "audit.completed"
        ][-1]
        self.assertEqual(result.decision, "ban")
        self.assertEqual(completed["decision"], "ban")
        self.assertEqual(completed["security_labels"], ["NUDITY"])
        self.assertEqual(completed["trace_id"], "trace_json")


if __name__ == "__main__":
    unittest.main()
