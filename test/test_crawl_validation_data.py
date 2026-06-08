import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "script" / "crawl_validation_data.py"
SPEC = importlib.util.spec_from_file_location("crawl_validation_data", MODULE_PATH)
crawler_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = crawler_module
SPEC.loader.exec_module(crawler_module)


class AllowRobots:
    def allowed(self, url, user_agent):
        return True


class DenyRobots:
    def allowed(self, url, user_agent):
        return False


class ValidationCrawlerTest(unittest.TestCase):
    def test_extract_text_and_links_skips_scripts_and_resolves_links(self):
        html = """
        <html>
          <head><title>Demo Page</title><script>secret()</script></head>
          <body><p>Hello validation text.</p><a href="/next#top">Next</a></body>
        </html>
        """

        text, links, title = crawler_module.extract_text_and_links(
            html, "https://example.test/start"
        )

        self.assertEqual(title, "Demo Page")
        self.assertIn("Hello validation text.", text)
        self.assertNotIn("secret", text)
        self.assertEqual(links, ["https://example.test/next"])

    def test_crawler_writes_validation_jsonl_and_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages = {
                "https://example.test/a": (
                    "<html><body>Alpha validation content long enough."
                    '<a href="/b">B</a></body></html>'
                ),
                "https://example.test/b": (
                    "<html><body>Beta validation content long enough.</body></html>"
                ),
            }

            def fetcher(url):
                return crawler_module.FetchResult(
                    url=url,
                    status=200,
                    body=pages[url].encode("utf-8"),
                    content_type="text/html",
                )

            config = crawler_module.CrawlerConfig(
                seeds=["https://example.test/a"],
                output_path=root / "validation.jsonl",
                raw_dir=root / "raw",
                state_path=root / "state.json",
                max_pages=2,
                max_depth=1,
                min_delay_sec=0,
                max_delay_sec=0,
                min_text_chars=5,
            )
            crawler = crawler_module.ValidationCrawler(
                config,
                fetcher=fetcher,
                robots=AllowRobots(),
                sleeper=lambda seconds: None,
                now=lambda: datetime(2026, 6, 8, tzinfo=timezone.utc),
            )

            stats = crawler.crawl()

            self.assertEqual(stats["written"], 2)
            records = [
                json.loads(line)
                for line in config.output_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["usage_split"], "validation")
            self.assertEqual(records[0]["source_type"], "公开数据")
            self.assertEqual(records[0]["clean_status"], "未清洗")
            self.assertEqual(records[0]["label_status"], "未标注")
            self.assertTrue(Path(records[0]["raw_content_ref"]).exists())

    def test_crawler_respects_robots_disallow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            called = []

            def fetcher(url):
                called.append(url)
                raise AssertionError("fetcher should not be called")

            config = crawler_module.CrawlerConfig(
                seeds=["https://example.test/private"],
                output_path=root / "validation.jsonl",
                raw_dir=root / "raw",
                state_path=root / "state.json",
                min_delay_sec=0,
                max_delay_sec=0,
            )
            crawler = crawler_module.ValidationCrawler(
                config,
                fetcher=fetcher,
                robots=DenyRobots(),
                sleeper=lambda seconds: None,
            )

            stats = crawler.crawl()

            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(called, [])
            self.assertFalse(config.output_path.exists())

    def test_access_challenge_page_is_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fetcher(url):
                return crawler_module.FetchResult(
                    url=url,
                    status=200,
                    body=b"<html><body>captcha verify you are human</body></html>",
                    content_type="text/html",
                )

            config = crawler_module.CrawlerConfig(
                seeds=["https://example.test/challenge"],
                output_path=root / "validation.jsonl",
                raw_dir=root / "raw",
                state_path=root / "state.json",
                min_delay_sec=0,
                max_delay_sec=0,
                min_text_chars=5,
            )
            crawler = crawler_module.ValidationCrawler(
                config,
                fetcher=fetcher,
                robots=AllowRobots(),
                sleeper=lambda seconds: None,
            )

            stats = crawler.crawl()

            self.assertEqual(stats["written"], 0)
            self.assertFalse(config.output_path.exists())


if __name__ == "__main__":
    unittest.main()
