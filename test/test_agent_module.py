import unittest

from core.agent import (
    AuditContext,
    LeafLabelHit,
    MultiAgentModerator,
    ToolResponse,
    build_rule_forest,
)


class OrderedToolbox:
    def __init__(self):
        self.calls: list[str] = []

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
        return ToolResponse(status="success", data={"hits": [], "paths": []}, trace_id=context.trace_id)

    def audit_trace_lookup(self, trace_id):
        self.calls.append("audit_trace_lookup")
        return ToolResponse(status="success", data={"audit_trace": []}, trace_id=trace_id)

    def threshold_preview(self, context, threshold_overrides):
        self.calls.append("threshold_preview")
        return ToolResponse(status="success", data={"preview_only": True}, trace_id=context.trace_id)

    def evaluate_leaf(self, context, node):
        self.calls.append("evaluate_leaf")
        return LeafLabelHit(label=node.label, reason=f"hit {node.label}", domain=node.domain, node_id=node.node_id)


class MultiAgentModuleTest(unittest.TestCase):
    def test_builds_security_and_ecosystem_trees_from_settings(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "name": "Porn",
                        "nudity": {
                            "name": "Nudity",
                            "keywords": ["naked"],
                            "default_action": "ban",
                        },
                    }
                },
                "ecosystem": {
                    "ad": {
                        "spam": {
                            "keywords": ["promo"],
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

    def test_root_preserves_child_labels_and_security_decision_priority(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {
                            "keywords": ["naked"],
                            "default_action": "ban",
                        }
                    }
                },
                "ecosystem": {
                    "ad": {
                        "spam": {
                            "keywords": ["promo"],
                            "default_action": "limit",
                        }
                    }
                },
            }
        }
        moderator = MultiAgentModerator.from_settings(settings)

        result = moderator.moderate(
            AuditContext(
                trace_id="trace_1",
                content={"text": "naked promo"},
            )
        )

        self.assertEqual(result.security_labels, ["NUDITY"])
        self.assertEqual(result.ecosystem_labels, ["SPAM"])
        self.assertEqual(result.final_decision, "reject")
        self.assertEqual(result.suggested_action, "ban")
        self.assertEqual(result.root_result.child_labels, ["PORN", "AD"])

    def test_intermediate_agent_does_not_dedupe_or_sort_labels(self):
        settings = {
            "rules": {
                "security": {
                    "group": {
                        "label-a": {"keywords": ["first"]},
                        "label_a": {"keywords": ["second"]},
                    }
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(settings)

        result = moderator.moderate(
            AuditContext(trace_id="trace_2", content={"text": "first second"})
        )

        self.assertEqual(result.security_labels, ["LABELA", "LABELA"])
        self.assertEqual(result.final_decision, "review")
        self.assertEqual(result.suggested_action, "manual_review")
        self.assertEqual(result.root_result.child_labels, ["GROUP"])

    def test_explicit_rule_result_hits_leaf_without_keywords(self):
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
        moderator = MultiAgentModerator.from_settings(settings)

        result = moderator.moderate(
            AuditContext(
                trace_id="trace_3",
                metadata={
                    "rule_results": [
                        {
                            "label": "RISK",
                            "hit": True,
                            "reason": "rule engine hit",
                            "action": "reject",
                        }
                    ]
                },
            )
        )

        self.assertEqual(result.security_labels, ["RISK"])
        self.assertEqual(result.reason, "rule engine hit")
        self.assertEqual(result.final_decision, "reject")

    def test_labels_requested_do_not_count_as_hits(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {
                            "keywords": ["naked"],
                            "default_action": "ban",
                        }
                    }
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(settings)

        result = moderator.moderate(
            AuditContext(trace_id="trace_4", labels_requested=["NUDITY"])
        )

        self.assertEqual(result.security_labels, [])
        self.assertEqual(result.final_decision, "pass")

    def test_route_only_selects_matching_first_level_branch(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {"keywords": ["naked"]},
                    },
                    "violence": {
                        "injury": {"keywords": ["blood"]},
                    },
                },
                "ecosystem": {},
            }
        }
        moderator = MultiAgentModerator.from_settings(settings)

        result = moderator.moderate(
            AuditContext(trace_id="trace_5", content={"text": "naked only"})
        )

        self.assertEqual(result.root_result.child_labels, ["PORN"])
        self.assertEqual(result.security_labels, ["NUDITY"])

    def test_moderate_dict_input_without_trace_id_uses_default(self):
        settings = {
            "rules": {
                "security": {
                    "porn": {
                        "nudity": {"keywords": ["naked"]},
                    }
                }
            }
        }
        moderator = MultiAgentModerator.from_settings(settings)

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
                        "nudity": {"keywords": ["naked"]},
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


if __name__ == "__main__":
    unittest.main()
