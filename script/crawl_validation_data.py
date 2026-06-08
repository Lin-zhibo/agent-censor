from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser


SCRIPT_VERSION = "validation-crawler/1.0.0"
DEFAULT_USER_AGENT = (
    "AgentCensorValidationCrawler/1.0 "
    "(public-or-authorized validation data collection)"
)
ACCESS_CHALLENGE_PATTERNS = (
    "captcha",
    "verify you are human",
    "access denied",
    "login required",
    "sign in",
    "验证码",
    "请登录",
    "访问受限",
)


@dataclass
class CrawlerConfig:
    seeds: list[str]
    output_path: Path = Path("data/validation/crawl_validation.jsonl")
    raw_dir: Path = Path("data/validation/raw")
    state_path: Path = Path("data/validation/crawl_state.json")
    max_pages: int = 50
    max_depth: int = 1
    same_domain: bool = True
    timeout_sec: float = 10.0
    min_delay_sec: float = 1.0
    max_delay_sec: float = 3.0
    retries: int = 2
    backoff_base_sec: float = 2.0
    max_bytes: int = 2_000_000
    min_text_chars: int = 40
    respect_robots: bool = True
    source_type: str = "公开数据"
    modality: str = "text"
    usage_split: str = "validation"
    clean_status: str = "未清洗"
    label_status: str = "未标注"
    labels: list[str] = field(default_factory=list)
    risk_note: str = "公开或授权来源采集，待人工清洗和标注。"
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    body: bytes
    content_type: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error


class TextAndLinkExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_tags: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._ignored_tags.append(tag)
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                normalized = normalize_url(urljoin(self.base_url, href))
                if normalized:
                    self.links.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_tags and self._ignored_tags[-1] == tag:
            self._ignored_tags.pop()
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_tags:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.text_parts.append(text)


def extract_text_and_links(html: str, base_url: str) -> tuple[str, list[str], str]:
    parser = TextAndLinkExtractor(base_url)
    parser.feed(html)
    text = normalize_text(" ".join(parser.text_parts))
    title = normalize_text(" ".join(parser.title_parts))
    links = list(dict.fromkeys(parser.links))
    return text, links, title


class RobotsCache:
    def __init__(self, timeout_sec: float = 10.0):
        self.timeout_sec = timeout_sec
        self._cache: dict[str, RobotFileParser] = {}

    def allowed(self, url: str, user_agent: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        key = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._cache.get(key)
        if parser is None:
            parser = RobotFileParser()
            robots_url = urljoin(key, "/robots.txt")
            parser.set_url(robots_url)
            try:
                request = Request(robots_url, headers={"User-Agent": user_agent})
                with urlopen(request, timeout=self.timeout_sec) as response:
                    robots_body = response.read(512_000)
                parser.parse(decode_body(robots_body).splitlines())
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    parser.disallow_all = True
                elif 400 <= exc.code < 500:
                    parser.allow_all = True
                else:
                    logging.warning("robots fetch failed for %s: %s", key, exc)
                    parser.allow_all = True
            except Exception as exc:
                logging.warning("robots fetch failed for %s: %s", key, exc)
                parser.allow_all = True
            self._cache[key] = parser
        return parser.can_fetch(user_agent, url)


class HostRateLimiter:
    def __init__(
        self,
        min_delay_sec: float,
        max_delay_sec: float,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.min_delay_sec = min_delay_sec
        self.max_delay_sec = max_delay_sec
        self.sleeper = sleeper
        self._last_request_at: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        if not host:
            return
        now = time.monotonic()
        requested_delay = random.uniform(self.min_delay_sec, self.max_delay_sec)
        elapsed = now - self._last_request_at.get(host, 0)
        if elapsed < requested_delay:
            self.sleeper(requested_delay - elapsed)
        self._last_request_at[host] = time.monotonic()


class ValidationCrawler:
    def __init__(
        self,
        config: CrawlerConfig,
        fetcher: Callable[[str], FetchResult] | None = None,
        robots: RobotsCache | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.fetcher = fetcher or self._fetch
        self.robots = robots or RobotsCache(timeout_sec=config.timeout_sec)
        self.sleeper = sleeper
        self.rate_limiter = HostRateLimiter(
            config.min_delay_sec,
            config.max_delay_sec,
            sleeper=sleeper,
        )
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.seed_domains = {urlparse(seed).netloc for seed in config.seeds}
        self.visited_urls: set[str] = set()
        self.seen_content_hashes: set[str] = set()
        self.host_failures: dict[str, int] = {}

    def crawl(self) -> dict[str, int]:
        state = self._load_state()
        self.visited_urls.update(state.get("visited_urls", []))
        self.seen_content_hashes.update(state.get("seen_content_hashes", []))
        self._load_seen_hashes_from_output()

        pending = deque(
            (item["url"], int(item["depth"]))
            for item in state.get("pending", [])
            if isinstance(item, Mapping) and "url" in item
        )
        if not pending:
            pending.extend((normalize_url(seed), 0) for seed in self.config.seeds)

        stats = {"fetched": 0, "written": 0, "skipped": 0, "failed": 0}
        try:
            while pending and stats["written"] < self.config.max_pages:
                url, depth = pending.popleft()
                if not url:
                    stats["skipped"] += 1
                    continue
                if self._skip_url(url, depth):
                    stats["skipped"] += 1
                    continue

                self.visited_urls.add(url)
                if self.config.respect_robots and not self.robots.allowed(
                    url, self.config.user_agent
                ):
                    logging.info("robots disallow: %s", url)
                    stats["skipped"] += 1
                    continue

                result = self.fetcher(url)
                if not result.ok:
                    stats["failed"] += 1
                    self._record_host_failure(url)
                    logging.warning("fetch failed: %s status=%s error=%s", url, result.status, result.error)
                    continue

                stats["fetched"] += 1
                html = decode_body(result.body)
                text, links, title = extract_text_and_links(html, result.url or url)
                if self._should_skip_content(text):
                    stats["skipped"] += 1
                else:
                    content_hash = sha256_text(text)
                    if content_hash in self.seen_content_hashes:
                        stats["skipped"] += 1
                    else:
                        self.seen_content_hashes.add(content_hash)
                        self._write_sample(result.url or url, text, title, content_hash)
                        stats["written"] += 1

                if depth < self.config.max_depth:
                    for link in links:
                        if not self._skip_url(link, depth + 1):
                            pending.append((link, depth + 1))
                self._save_state(pending)
        finally:
            self._save_state(pending)
        return stats

    def _fetch(self, url: str) -> FetchResult:
        for attempt in range(self.config.retries + 1):
            self.rate_limiter.wait(url)
            request = Request(
                url,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                },
            )
            try:
                with urlopen(request, timeout=self.config.timeout_sec) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if not is_textual_content_type(content_type):
                        return FetchResult(
                            url=response.url,
                            status=response.status,
                            body=b"",
                            content_type=content_type,
                            error=f"unsupported content type: {content_type}",
                        )
                    body = response.read(self.config.max_bytes + 1)
                    if len(body) > self.config.max_bytes:
                        return FetchResult(
                            url=response.url,
                            status=response.status,
                            body=b"",
                            content_type=content_type,
                            error=f"response exceeds max_bytes={self.config.max_bytes}",
                        )
                    return FetchResult(
                        url=response.url,
                        status=response.status,
                        body=body,
                        content_type=content_type,
                    )
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    return FetchResult(url=url, status=exc.code, body=b"", error="access denied")
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < self.config.retries:
                        self._backoff(attempt)
                        continue
                return FetchResult(url=url, status=exc.code, body=b"", error=str(exc))
            except URLError as exc:
                if attempt < self.config.retries:
                    self._backoff(attempt)
                    continue
                return FetchResult(url=url, status=0, body=b"", error=str(exc.reason))
            except TimeoutError:
                if attempt < self.config.retries:
                    self._backoff(attempt)
                    continue
                return FetchResult(url=url, status=0, body=b"", error="timeout")
        return FetchResult(url=url, status=0, body=b"", error="retry exhausted")

    def _backoff(self, attempt: int) -> None:
        delay = self.config.backoff_base_sec * (2**attempt)
        delay += random.uniform(0, self.config.backoff_base_sec)
        self.sleeper(delay)

    def _skip_url(self, url: str, depth: int) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return True
        if depth > self.config.max_depth:
            return True
        if url in self.visited_urls:
            return True
        if self.config.same_domain and parsed.netloc not in self.seed_domains:
            return True
        if self.host_failures.get(parsed.netloc, 0) >= 3:
            return True
        return False

    def _should_skip_content(self, text: str) -> bool:
        if len(text) < self.config.min_text_chars:
            return True
        lower_text = text.lower()
        return any(pattern in lower_text for pattern in ACCESS_CHALLENGE_PATTERNS)

    def _write_sample(
        self,
        source_url: str,
        text: str,
        title: str,
        content_hash: str,
    ) -> None:
        sample_id = f"sample_{sha256_text(source_url + content_hash)[:16]}"
        raw_path = self.config.raw_dir / f"{sample_id}.txt"
        self.config.raw_dir.mkdir(parents=True, exist_ok=True)
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(text, encoding="utf-8")

        sample = {
            "sample_id": sample_id,
            "source_type": self.config.source_type,
            "source_url": source_url,
            "source_ref": source_url,
            "collected_at": self.now().isoformat(),
            "modality": self.config.modality,
            "raw_content_ref": as_posix(raw_path),
            "clean_status": self.config.clean_status,
            "label_status": self.config.label_status,
            "labels": list(self.config.labels),
            "usage_split": self.config.usage_split,
            "risk_note": self.config.risk_note,
            "script_version": SCRIPT_VERSION,
            "content_hash": content_hash,
            "title": title,
            "text_excerpt": text[:300],
        }
        with self.config.output_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(sample, ensure_ascii=False) + "\n")

    def _record_host_failure(self, url: str) -> None:
        host = urlparse(url).netloc
        self.host_failures[host] = self.host_failures.get(host, 0) + 1

    def _load_state(self) -> Mapping[str, object]:
        if not self.config.state_path.exists():
            return {}
        try:
            return json.loads(self.config.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logging.warning("ignore broken state file %s: %s", self.config.state_path, exc)
            return {}

    def _save_state(self, pending: deque[tuple[str, int]]) -> None:
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "script_version": SCRIPT_VERSION,
            "updated_at": self.now().isoformat(),
            "visited_urls": sorted(self.visited_urls),
            "seen_content_hashes": sorted(self.seen_content_hashes),
            "pending": [{"url": url, "depth": depth} for url, depth in pending],
        }
        self.config.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_seen_hashes_from_output(self) -> None:
        if not self.config.output_path.exists():
            return
        with self.config.output_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content_hash = record.get("content_hash")
                if isinstance(content_hash, str):
                    self.seen_content_hashes.add(content_hash)


def normalize_url(url: str) -> str:
    url, _fragment = urldefrag(url.strip())
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return parsed.geturl()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def decode_body(body: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="ignore")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_textual_content_type(content_type: str) -> bool:
    lower = content_type.lower()
    return not lower or lower.startswith("text/") or "html" in lower or "xml" in lower


def as_posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def read_seeds_file(path: Path) -> list[str]:
    seeds: list[str] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                seeds.append(stripped)
    return seeds


def parse_labels(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl public or authorized pages into validation-set JSONL samples."
    )
    parser.add_argument("--seed", action="append", default=[], help="Seed URL. Can be repeated.")
    parser.add_argument("--seeds-file", type=Path, help="Text file with one seed URL per line.")
    parser.add_argument("--out", type=Path, default=Path("data/validation/crawl_validation.jsonl"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/validation/raw"))
    parser.add_argument("--state", type=Path, default=Path("data/validation/crawl_state.json"))
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--max-depth", type=int, default=1)
    parser.add_argument("--no-same-domain", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--min-delay", type=float, default=1.0)
    parser.add_argument("--max-delay", type=float, default=3.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    parser.add_argument("--min-text-chars", type=int, default=40)
    parser.add_argument("--source-type", default="公开数据")
    parser.add_argument("--modality", default="text")
    parser.add_argument("--usage-split", default="validation")
    parser.add_argument("--labels", default="", help="Comma separated label hints, e.g. PORN,AD.")
    parser.add_argument("--risk-note", default="公开或授权来源采集，待人工清洗和标注。")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--verbose", action="store_true")
    return parser


def build_config(args: argparse.Namespace) -> CrawlerConfig:
    seeds = list(args.seed)
    if args.seeds_file:
        seeds.extend(read_seeds_file(args.seeds_file))
    seeds = [seed for seed in (normalize_url(seed) for seed in seeds) if seed]
    if not seeds:
        raise ValueError("at least one http(s) seed URL is required")
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        raise ValueError("--max-delay must be greater than or equal to --min-delay")
    return CrawlerConfig(
        seeds=seeds,
        output_path=args.out,
        raw_dir=args.raw_dir,
        state_path=args.state,
        max_pages=max(0, args.max_pages),
        max_depth=max(0, args.max_depth),
        same_domain=not args.no_same_domain,
        timeout_sec=args.timeout,
        min_delay_sec=args.min_delay,
        max_delay_sec=args.max_delay,
        retries=max(0, args.retries),
        max_bytes=max(1, args.max_bytes),
        min_text_chars=max(0, args.min_text_chars),
        source_type=args.source_type,
        modality=args.modality,
        usage_split=args.usage_split,
        labels=parse_labels(args.labels),
        risk_note=args.risk_note,
        user_agent=args.user_agent,
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = build_config(args)
    except ValueError as exc:
        parser.error(str(exc))
    crawler = ValidationCrawler(config)
    stats = crawler.crawl()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
