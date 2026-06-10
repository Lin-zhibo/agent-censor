import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


MODULE_PATH = Path(__file__).resolve().parents[1] / "script" / "collect_4chan_api.py"
SPEC = importlib.util.spec_from_file_location("collect_4chan_api", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class FourChanAPICollectorTest(unittest.TestCase):
    def test_collects_text_only_samples_and_redacts_pii(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requests = []
            catalog_url = "https://a.4cdn.org/aco/catalog.json"
            thread_url = "https://a.4cdn.org/aco/thread/123.json"

            def fetcher(url, headers):
                requests.append((url, dict(headers)))
                self.assertEqual(urlparse(url).netloc, "a.4cdn.org")
                if url == catalog_url:
                    return module.APIResponse(
                        url=url,
                        status=200,
                        body=json.dumps(
                            [{"page": 1, "threads": [{"no": 123, "last_modified": 10}]}]
                        ).encode("utf-8"),
                        headers={"Last-Modified": "Tue, 09 Jun 2026 00:00:00 GMT"},
                    )
                if url == thread_url:
                    return module.APIResponse(
                        url=url,
                        status=200,
                        body=json.dumps(
                            {
                                "posts": [
                                    {
                                        "no": 123,
                                        "sub": "Adult board topic",
                                        "com": (
                                            "NSFW classified text<br><b>with html</b> "
                                            "email test@example.com IP 192.0.2.9 "
                                            "phone +1 555-010-1234"
                                        ),
                                        "tim": 1,
                                        "filename": "ignored-media",
                                        "ext": ".jpg",
                                    }
                                ]
                            }
                        ).encode("utf-8"),
                        headers={"Last-Modified": "Tue, 09 Jun 2026 00:00:10 GMT"},
                    )
                raise AssertionError(f"unexpected URL {url}")

            config = module.FourChanConfig(
                boards=["aco"],
                output_path=root / "out.jsonl",
                raw_dir=root / "raw",
                state_path=root / "state.json",
                max_samples=1,
                max_threads_per_board=1,
                min_delay_sec=1.1,
                min_text_chars=5,
                quotas={"adult": 1, "violence": 0, "illegal": 0, "privacy": 1},
                include_synthetic_privacy=False,
            )
            collector = module.FourChanAPICollector(
                config,
                fetcher=fetcher,
                sleeper=lambda seconds: None,
                now=lambda: datetime(2026, 6, 9, tzinfo=timezone.utc),
                now_epoch=lambda: 100.0,
            )

            stats = collector.collect()

            self.assertEqual(stats["written"], 1)
            self.assertEqual([url for url, _headers in requests], [catalog_url, thread_url])
            record = json.loads(config.output_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["source_platform"], "4chan")
            self.assertEqual(record["board"], "aco")
            self.assertEqual(record["thread_id"], 123)
            self.assertEqual(record["post_id"], 123)
            self.assertTrue(record["media_omitted"])
            self.assertTrue(record["pii_redacted"])
            self.assertIn("PORN", record["candidate_labels"])
            self.assertIn("PRIVACY_LAW", record["candidate_labels"])
            self.assertEqual(record["label_status"], "待人工复核")
            raw_text = Path(record["raw_content_ref"]).read_text(encoding="utf-8")
            self.assertIn("[REDACTED_EMAIL]", raw_text)
            self.assertIn("[REDACTED_IP]", raw_text)
            self.assertIn("[REDACTED_PHONE]", raw_text)
            self.assertNotIn("test@example.com", raw_text)
            self.assertNotIn("192.0.2.9", raw_text)
            self.assertNotIn("555-010-1234", raw_text)
            self.assertNotIn("ignored-media", json.dumps(record))
            self.assertNotIn("i.4cdn.org", json.dumps(record))

    def test_uses_if_modified_since_and_skips_304_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread_url = "https://a.4cdn.org/g/thread/456.json"
            state = {
                "last_modified": {
                    thread_url: "Tue, 09 Jun 2026 00:00:00 GMT",
                },
                "thread_last_fetch_at": {
                    thread_url: 0,
                },
            }
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            requests = []
            sleeps = []

            def fetcher(url, headers):
                requests.append((url, dict(headers)))
                if url.endswith("/catalog.json"):
                    return module.APIResponse(
                        url=url,
                        status=200,
                        body=json.dumps(
                            [{"page": 1, "threads": [{"no": 456, "last_modified": 20}]}]
                        ).encode("utf-8"),
                    )
                self.assertEqual(url, thread_url)
                return module.APIResponse(url=url, status=304)

            config = module.FourChanConfig(
                boards=["g"],
                output_path=root / "out.jsonl",
                raw_dir=root / "raw",
                state_path=root / "state.json",
                max_samples=1,
                max_threads_per_board=1,
                min_delay_sec=1.1,
                include_synthetic_privacy=False,
            )
            collector = module.FourChanAPICollector(
                config,
                fetcher=fetcher,
                sleeper=sleeps.append,
                now_epoch=lambda: 20.0,
            )

            stats = collector.collect()

            self.assertEqual(stats["not_modified"], 1)
            self.assertFalse(config.output_path.exists())
            self.assertEqual(requests[1][1]["If-Modified-Since"], "Tue, 09 Jun 2026 00:00:00 GMT")
            self.assertTrue(any(seconds >= 1.0 for seconds in sleeps))

    def test_skips_recently_fetched_thread_without_requesting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread_url = "https://a.4cdn.org/g/thread/456.json"
            (root / "state.json").write_text(
                json.dumps({"thread_last_fetch_at": {thread_url: 15.0}}),
                encoding="utf-8",
            )
            requests = []

            def fetcher(url, headers):
                requests.append(url)
                return module.APIResponse(
                    url=url,
                    status=200,
                    body=json.dumps(
                        [{"page": 1, "threads": [{"no": 456, "last_modified": 20}]}]
                    ).encode("utf-8"),
                )

            config = module.FourChanConfig(
                boards=["g"],
                output_path=root / "out.jsonl",
                raw_dir=root / "raw",
                state_path=root / "state.json",
                max_samples=1,
                max_threads_per_board=1,
                include_synthetic_privacy=False,
            )
            collector = module.FourChanAPICollector(
                config,
                fetcher=fetcher,
                sleeper=lambda seconds: None,
                now_epoch=lambda: 20.0,
            )

            stats = collector.collect()

            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(requests, ["https://a.4cdn.org/g/catalog.json"])

    def test_label_mapping_for_risk_categories(self):
        categories, labels = module.classify_candidate(
            "pol",
            "fraud leak attack phone [REDACTED_EMAIL]",
            pii_redacted=True,
        )

        self.assertEqual(categories, ["violence", "illegal", "privacy"])
        self.assertIn("VIOLENCE_PROMOTE", labels)
        self.assertIn("ILLEGAL_BEHAVIOR", labels)
        self.assertIn("PRIVACY_LAW", labels)

    def test_synthetic_privacy_samples_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = module.FourChanConfig(
                boards=[],
                output_path=root / "out.jsonl",
                raw_dir=root / "raw",
                state_path=root / "state.json",
                max_samples=2,
                quotas={"adult": 0, "violence": 0, "illegal": 0, "privacy": 2},
                include_synthetic_privacy=True,
            )
            collector = module.FourChanAPICollector(
                config,
                fetcher=lambda url, headers: self.fail("fetcher should not be called"),
                sleeper=lambda seconds: None,
                now=lambda: datetime(2026, 6, 9, tzinfo=timezone.utc),
            )

            stats = collector.collect()

            self.assertEqual(stats["synthetic_written"], 2)
            records = [
                json.loads(line)
                for line in config.output_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 2)
            for record in records:
                self.assertEqual(record["source_platform"], "synthetic_privacy")
                self.assertEqual(record["risk_categories"], ["privacy"])
                raw_text = Path(record["raw_content_ref"]).read_text(encoding="utf-8")
                self.assertIn("[REDACTED_", raw_text)
                self.assertNotIn("jane.example@example.com", raw_text)
                self.assertNotIn("192.0.2.44", raw_text)
                self.assertNotIn("555-010", raw_text)

    def test_build_config_rejects_too_fast_delay(self):
        parser = module.build_arg_parser()
        args = parser.parse_args(["--boards", "g", "--min-delay", "0.5"])

        with self.assertRaises(ValueError):
            module.build_config(args)


if __name__ == "__main__":
    unittest.main()
