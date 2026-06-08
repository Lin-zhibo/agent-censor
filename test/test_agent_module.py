import unittest

from core.agent import AuditContext, MultiAgentModerator, build_rule_forest


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


if __name__ == "__main__":
    unittest.main()
