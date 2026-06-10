from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_VERSION = "4chan-json-validation-crawler/1.0.0"
API_BASE_URL = "https://a.4cdn.org"
DEFAULT_USER_AGENT = (
    "AgentCensor4chanValidationCollector/1.0 "
    "(official read-only JSON API; text-only validation data collection)"
)
DEFAULT_BOARDS = "aco,d,e,gif,h,hc,hm,hr,k,pol,biz,t,g,sci,his,b"
DEFAULT_QUOTAS = {
    "adult": 50,
    "violence": 50,
    "illegal": 50,
    "privacy": 50,
}
BOARD_PATTERN = re.compile(r"^[a-z0-9]+$")


RISK_ORDER = ("adult", "violence", "illegal", "privacy")
RISK_LABELS = {
    "adult": [
        "PORN",
        "SEX_SERVICE",
        "SEX_RESOURCE",
        "SEX_DATING",
        "SEX_BEHAVIOR",
        "SEX_SEDUCTION",
    ],
    "violence": [
        "VIOLENCE_DRUG",
        "VIOLENCE_REVENGE",
        "VIOLENCE_PROMOTE",
        "CYBER_VIOLENCE",
    ],
    "illegal": [
        "ILLEGAL_BEHAVIOR",
        "ILLEGAL_GENERAL",
        "ILLEGAL_MARKETING",
        "ILLEGAL_TOOL",
        "CONTROLLED_DRUGS",
    ],
    "privacy": [
        "PRIVACY_LAW",
        "PRIVACY_SALE",
        "PRIVACY_SERVICE",
    ],
}
BOARD_RISK_CATEGORIES = {
    "aco": ["adult"],
    "d": ["adult"],
    "e": ["adult"],
    "gif": ["adult"],
    "h": ["adult"],
    "hc": ["adult"],
    "hm": ["adult"],
    "hr": ["adult"],
    "k": ["violence"],
    "pol": ["violence", "illegal", "privacy"],
    "biz": ["illegal"],
    "t": ["illegal"],
    "b": ["adult", "violence", "illegal", "privacy"],
}
RISK_KEYWORDS = {
    "adult": [
        "adult",
        "explicit",
        "nsfw",
        "escort",
        "hookup",
        "dating",
        "onlyfans",
    ],
    "violence": [
        "attack",
        "blood",
        "fight",
        "kill",
        "revenge",
        "shoot",
        "weapon",
    ],
    "illegal": [
        "counterfeit",
        "crack",
        "drug",
        "exploit",
        "fraud",
        "leak",
        "piracy",
        "scam",
        "stolen",
    ],
    "privacy": [
        "address",
        "dox",
        "doxx",
        "email",
        "ip address",
        "personal info",
        "phone",
    ],
}


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)
URL_RE = re.compile(r"\bhttps?://[^\s<>\"]+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
SOCIAL_HANDLE_RE = re.compile(r"(?<![\w/])@[A-Za-z0-9_]{3,30}\b")
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .'-]{2,50}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct)\b",
    re.IGNORECASE,
)
OPERATIONAL_RE = re.compile(
    r"(?i)\b("
    r"step\s*\d+|step-by-step|tutorial|instructions?|recipe|materials?|"
    r"ingredients?|how\s+to|source\s+code|exploit\s+chain"
    r")\b"
)


@dataclass
class FourChanConfig:
    boards: list[str]
    output_path: Path = Path("data/validation/4chan_negative.jsonl")
    raw_dir: Path = Path("data/validation/4chan_negative_raw")
    state_path: Path = Path("data/validation/4chan_api_state.json")
    max_samples: int = 200
    max_threads_per_board: int = 10
    min_delay_sec: float = 1.1
    thread_refresh_min_sec: float = 10.0
    timeout_sec: float = 10.0
    retries: int = 2
    backoff_base_sec: float = 2.0
    min_text_chars: int = 20
    quotas: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_QUOTAS))
    include_synthetic_privacy: bool = True
    source_type: str = "公开社交媒体负类候选"
    modality: str = "text"
    usage_split: str = "validation"
    label_status: str = "待人工复核"
    risk_note: str = (
        "4chan 官方 read-only JSON API 采集的文本-only 负类候选；"
        "媒体已省略，个人信息已脱敏，需人工复核后使用。"
    )
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True)
class APIResponse:
    url: str
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    @property
    def not_modified(self) -> bool:
        return self.status == 304


@dataclass(frozen=True)
class PostCandidate:
    board: str
    thread_id: int
    post_id: int
    api_url: str
    source_url: str
    text: str
    title: str
    candidate_labels: list[str]
    risk_categories: list[str]
    pii_redacted: bool
    operational_redacted: bool


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._ignored_tags.append(tag)
            return
        if tag in {"br", "p", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_tags and self._ignored_tags[-1] == tag:
            self._ignored_tags.pop()
        if tag in {"p", "div", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_tags:
            return
        self.parts.append(data)


class GlobalRateLimiter:
    def __init__(
        self,
        min_delay_sec: float,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_delay_sec = min_delay_sec
        self.sleeper = sleeper
        self.monotonic = monotonic
        self._last_request_at: float | None = None

    def wait(self) -> None:
        if self._last_request_at is not None:
            elapsed = self.monotonic() - self._last_request_at
            if elapsed < self.min_delay_sec:
                self.sleeper(self.min_delay_sec - elapsed)
        self._last_request_at = self.monotonic()


class FourChanAPICollector:
    def __init__(
        self,
        config: FourChanConfig,
        fetcher: Callable[[str, Mapping[str, str]], APIResponse] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        now_epoch: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._uses_default_fetcher = fetcher is None
        self.fetcher = fetcher or self._fetch
        self.rate_limiter = GlobalRateLimiter(config.min_delay_sec, sleeper=sleeper)
        self.sleeper = sleeper
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.now_epoch = now_epoch or time.time
        self.state: dict[str, object] = {}
        self.seen_content_hashes: set[str] = set()
        self.category_counts: dict[str, int] = {category: 0 for category in RISK_ORDER}

    def collect(self) -> dict[str, int]:
        self._load_state()
        self._load_seen_hashes_and_counts_from_output()
        stats = {
            "catalogs": 0,
            "threads": 0,
            "posts_seen": 0,
            "written": 0,
            "synthetic_written": 0,
            "skipped": 0,
            "failed": 0,
            "not_modified": 0,
        }
        try:
            for board in self.config.boards:
                if stats["written"] >= self.config.max_samples:
                    break
                catalog_url = catalog_api_url(board)
                catalog = self._fetch_json(catalog_url, stats)
                if catalog is None:
                    continue
                stats["catalogs"] += 1
                for thread_id in extract_thread_ids(
                    catalog,
                    self.config.max_threads_per_board,
                ):
                    if stats["written"] >= self.config.max_samples:
                        break
                    thread_url = thread_api_url(board, thread_id)
                    if self._thread_recently_fetched(thread_url):
                        stats["skipped"] += 1
                        continue
                    thread = self._fetch_json(thread_url, stats, is_thread=True)
                    if thread is None:
                        continue
                    stats["threads"] += 1
                    for candidate in extract_candidates(board, thread_id, thread_url, thread):
                        stats["posts_seen"] += 1
                        if stats["written"] >= self.config.max_samples:
                            break
                        candidate = prepare_candidate(
                            candidate,
                            min_text_chars=self.config.min_text_chars,
                        )
                        if candidate is None:
                            stats["skipped"] += 1
                            continue
                        if not self._has_remaining_quota(candidate.risk_categories):
                            stats["skipped"] += 1
                            continue
                        if self._write_candidate(candidate):
                            stats["written"] += 1
                            self._record_category_counts(candidate.risk_categories)
                        else:
                            stats["skipped"] += 1

            stats["synthetic_written"] = self._write_synthetic_privacy(stats)
            stats["written"] += stats["synthetic_written"]
        finally:
            self._save_state()
        return stats

    def _fetch_json(
        self,
        url: str,
        stats: dict[str, int],
        is_thread: bool = False,
    ) -> object | None:
        if not is_allowed_api_url(url):
            raise ValueError(f"unsupported 4chan API URL: {url}")

        last_modified = self._last_modified().get(url)
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
        }
        if isinstance(last_modified, str) and last_modified:
            headers["If-Modified-Since"] = last_modified

        if not self._uses_default_fetcher:
            self.rate_limiter.wait()
        response = self.fetcher(url, headers)
        if response.not_modified:
            stats["not_modified"] += 1
            if is_thread:
                self._thread_last_fetch_at()[url] = self.now_epoch()
            return None
        if not response.ok:
            stats["failed"] += 1
            logging.warning("4chan API fetch failed: %s status=%s error=%s", url, response.status, response.error)
            return None

        last_modified_header = get_header(response.headers, "Last-Modified")
        if last_modified_header:
            self._last_modified()[url] = last_modified_header
        if is_thread:
            self._thread_last_fetch_at()[url] = self.now_epoch()
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            stats["failed"] += 1
            logging.warning("invalid 4chan API JSON: %s error=%s", url, exc)
            return None

    def _fetch(self, url: str, headers: Mapping[str, str]) -> APIResponse:
        for attempt in range(self.config.retries + 1):
            self.rate_limiter.wait()
            request = Request(url, headers=dict(headers))
            try:
                with urlopen(request, timeout=self.config.timeout_sec) as response:
                    return APIResponse(
                        url=response.url,
                        status=response.status,
                        body=response.read(),
                        headers=dict(response.headers.items()),
                    )
            except HTTPError as exc:
                if exc.code == 304:
                    return APIResponse(url=url, status=304, headers=dict(exc.headers.items()))
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < self.config.retries:
                        self._backoff(attempt)
                        continue
                return APIResponse(url=url, status=exc.code, error=str(exc))
            except (TimeoutError, URLError) as exc:
                if attempt < self.config.retries:
                    self._backoff(attempt)
                    continue
                return APIResponse(url=url, status=0, error=str(exc))
        return APIResponse(url=url, status=0, error="retry exhausted")

    def _backoff(self, attempt: int) -> None:
        delay = self.config.backoff_base_sec * (2**attempt)
        delay += random.uniform(0, self.config.backoff_base_sec)
        self.sleeper(delay)

    def _thread_recently_fetched(self, url: str) -> bool:
        last_fetch = self._thread_last_fetch_at().get(url)
        if not isinstance(last_fetch, (int, float)):
            return False
        return self.now_epoch() - float(last_fetch) < self.config.thread_refresh_min_sec

    def _write_candidate(self, candidate: PostCandidate) -> bool:
        content_hash = sha256_text(candidate.text)
        if content_hash in self.seen_content_hashes:
            return False
        self.seen_content_hashes.add(content_hash)
        sample_id = f"4chan_{sha256_text(candidate.source_url + content_hash)[:16]}"
        raw_path = self.config.raw_dir / f"{sample_id}.txt"
        clean_status = "已脱敏" if candidate.pii_redacted else "未清洗"
        sample = base_sample(
            sample_id=sample_id,
            source_type=self.config.source_type,
            source_url=candidate.source_url,
            raw_path=raw_path,
            text=candidate.text,
            title=candidate.title,
            labels=candidate.candidate_labels,
            usage_split=self.config.usage_split,
            clean_status=clean_status,
            label_status=self.config.label_status,
            risk_note=self.config.risk_note,
            now=self.now(),
        )
        sample.update(
            {
                "source_platform": "4chan",
                "board": candidate.board,
                "thread_id": candidate.thread_id,
                "post_id": candidate.post_id,
                "candidate_labels": candidate.candidate_labels,
                "risk_categories": candidate.risk_categories,
                "media_omitted": True,
                "pii_redacted": candidate.pii_redacted,
                "operational_detail_redacted": candidate.operational_redacted,
                "api_url": candidate.api_url,
            }
        )
        write_sample(self.config.output_path, raw_path, candidate.text, sample)
        return True

    def _write_synthetic_privacy(self, stats: Mapping[str, int]) -> int:
        if not self.config.include_synthetic_privacy:
            return 0
        written = 0
        remaining_total = self.config.max_samples - int(stats.get("written", 0))
        remaining_privacy = self.config.quotas.get("privacy", 0) - self.category_counts.get("privacy", 0)
        target = max(0, min(remaining_total, remaining_privacy))
        for index in range(target):
            text, pii_redacted = redact_pii(SYNTHETIC_PRIVACY_INPUTS[index % len(SYNTHETIC_PRIVACY_INPUTS)])
            text = normalize_text(text)
            content_hash = sha256_text(text)
            if content_hash in self.seen_content_hashes:
                continue
            self.seen_content_hashes.add(content_hash)
            sample_id = f"synthetic_privacy_{sha256_text(str(index) + content_hash)[:16]}"
            raw_path = self.config.raw_dir / f"{sample_id}.txt"
            labels = list(RISK_LABELS["privacy"])
            sample = base_sample(
                sample_id=sample_id,
                source_type="合成隐私负类候选",
                source_url=f"synthetic://privacy/{index + 1}",
                raw_path=raw_path,
                text=text,
                title="合成隐私侵害负类候选",
                labels=labels,
                usage_split=self.config.usage_split,
                clean_status="已脱敏",
                label_status=self.config.label_status,
                risk_note="合成隐私风险样本；不含真实个人信息，需人工复核后使用。",
                now=self.now(),
            )
            sample.update(
                {
                    "source_platform": "synthetic_privacy",
                    "board": "synthetic",
                    "thread_id": "",
                    "post_id": index + 1,
                    "candidate_labels": labels,
                    "risk_categories": ["privacy"],
                    "media_omitted": True,
                    "pii_redacted": pii_redacted,
                    "operational_detail_redacted": False,
                    "api_url": "",
                }
            )
            write_sample(self.config.output_path, raw_path, text, sample)
            self._record_category_counts(["privacy"])
            written += 1
        return written

    def _has_remaining_quota(self, categories: Iterable[str]) -> bool:
        return any(
            self.category_counts.get(category, 0) < self.config.quotas.get(category, 0)
            for category in categories
        )

    def _record_category_counts(self, categories: Iterable[str]) -> None:
        for category in categories:
            if self.category_counts.get(category, 0) < self.config.quotas.get(category, 0):
                self.category_counts[category] = self.category_counts.get(category, 0) + 1

    def _load_state(self) -> None:
        if not self.config.state_path.exists():
            self.state = {}
            return
        try:
            loaded = json.loads(self.config.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logging.warning("ignore broken state file %s: %s", self.config.state_path, exc)
            loaded = {}
        self.state = loaded if isinstance(loaded, dict) else {}
        seen = self.state.get("seen_content_hashes", [])
        if isinstance(seen, list):
            self.seen_content_hashes.update(item for item in seen if isinstance(item, str))

    def _save_state(self) -> None:
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state["script_version"] = SCRIPT_VERSION
        self.state["updated_at"] = self.now().isoformat()
        self.state["seen_content_hashes"] = sorted(self.seen_content_hashes)
        self.config.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_seen_hashes_and_counts_from_output(self) -> None:
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
                risk_categories = record.get("risk_categories", [])
                if isinstance(risk_categories, list):
                    self._record_category_counts(
                        category for category in risk_categories if isinstance(category, str)
                    )

    def _last_modified(self) -> dict[str, str]:
        value = self.state.setdefault("last_modified", {})
        if not isinstance(value, dict):
            value = {}
            self.state["last_modified"] = value
        return value

    def _thread_last_fetch_at(self) -> dict[str, float]:
        value = self.state.setdefault("thread_last_fetch_at", {})
        if not isinstance(value, dict):
            value = {}
            self.state["thread_last_fetch_at"] = value
        return value


SYNTHETIC_PRIVACY_INPUTS = [
    (
        "Synthetic privacy-risk sample: a post exposes Jane Example's email "
        "jane.example@example.com, phone +1 555-010-1234, IP 192.0.2.44, "
        "and address 123 Example Street."
    ),
    (
        "Synthetic privacy-risk sample: a user asks others to repost @samplevictim, "
        "contact sample.victim@example.org, call 555 010 2233, and visit 45 Test Road."
    ),
    (
        "Synthetic privacy-risk sample: a thread shares a private URL "
        "https://example.com/private/profile and IP 198.51.100.8 with a phone "
        "(555) 010-3344."
    ),
]


def extract_candidates(
    board: str,
    thread_id: int,
    api_url: str,
    thread: object,
) -> list[PostCandidate]:
    if not isinstance(thread, Mapping):
        return []
    posts = thread.get("posts", [])
    if not isinstance(posts, list):
        return []
    candidates = []
    for post in posts:
        if not isinstance(post, Mapping):
            continue
        post_id = as_int(post.get("no"))
        if post_id is None:
            continue
        raw_title = strip_4chan_html(str(post.get("sub", "")))
        raw_body = strip_4chan_html(str(post.get("com", "")))
        combined_text = normalize_text("\n".join(part for part in (raw_title, raw_body) if part))
        if not combined_text:
            continue
        pii_text, pii_redacted = redact_pii(combined_text)
        categories, labels = classify_candidate(board, pii_text, pii_redacted)
        if not categories:
            continue
        sanitized_text, operational_redacted = redact_operational_details(pii_text, categories)
        candidates.append(
            PostCandidate(
                board=board,
                thread_id=thread_id,
                post_id=post_id,
                api_url=api_url,
                source_url=post_web_url(board, thread_id, post_id),
                text=normalize_text(sanitized_text),
                title=raw_title,
                candidate_labels=labels,
                risk_categories=categories,
                pii_redacted=pii_redacted,
                operational_redacted=operational_redacted,
            )
        )
    return candidates


def prepare_candidate(candidate: PostCandidate, min_text_chars: int) -> PostCandidate | None:
    if len(candidate.text) < min_text_chars:
        return None
    return candidate


def classify_candidate(
    board: str,
    text: str,
    pii_redacted: bool,
) -> tuple[list[str], list[str]]:
    lower_text = text.lower()
    categories = set(BOARD_RISK_CATEGORIES.get(board, []))
    for category, keywords in RISK_KEYWORDS.items():
        if any(keyword in lower_text for keyword in keywords):
            categories.add(category)
    if pii_redacted:
        categories.add("privacy")
    ordered_categories = [category for category in RISK_ORDER if category in categories]
    labels: list[str] = []
    for category in ordered_categories:
        labels.extend(RISK_LABELS[category])
    return ordered_categories, list(dict.fromkeys(labels))


def redact_pii(text: str) -> tuple[str, bool]:
    redacted = False

    def replace(pattern: re.Pattern[str], replacement: str, value: str) -> str:
        nonlocal redacted
        new_value, count = pattern.subn(replacement, value)
        if count:
            redacted = True
        return new_value

    text = replace(EMAIL_RE, "[REDACTED_EMAIL]", text)
    text = replace(IPV4_RE, "[REDACTED_IP]", text)
    text = replace(URL_RE, "[REDACTED_URL]", text)
    text = replace(ADDRESS_RE, "[REDACTED_ADDRESS]", text)
    text = replace(SOCIAL_HANDLE_RE, "[REDACTED_HANDLE]", text)

    def redact_phone(match: re.Match[str]) -> str:
        nonlocal redacted
        digits = re.sub(r"\D", "", match.group(0))
        if 8 <= len(digits) <= 16:
            redacted = True
            return "[REDACTED_PHONE]"
        return match.group(0)

    text = PHONE_RE.sub(redact_phone, text)
    return text, redacted


def redact_operational_details(text: str, categories: Iterable[str]) -> tuple[str, bool]:
    if not {"illegal", "violence"}.intersection(categories):
        return text, False
    parts = re.split(r"(?<=[.!?。！？\n])\s+", text)
    redacted = False
    sanitized_parts = []
    for part in parts:
        if OPERATIONAL_RE.search(part):
            sanitized_parts.append("[REDACTED_OPERATIONAL_DETAIL]")
            redacted = True
        else:
            sanitized_parts.append(part)
    return " ".join(sanitized_parts), redacted


def strip_4chan_html(value: str) -> str:
    if not value:
        return ""
    value = value.replace("<wbr>", "")
    parser = TextExtractor()
    parser.feed(value)
    parser.close()
    return normalize_text(html.unescape(" ".join(parser.parts)))


def extract_thread_ids(catalog: object, max_threads: int) -> list[int]:
    if not isinstance(catalog, list):
        return []
    threads = []
    for page in catalog:
        if not isinstance(page, Mapping):
            continue
        page_threads = page.get("threads", [])
        if not isinstance(page_threads, list):
            continue
        for thread in page_threads:
            if not isinstance(thread, Mapping):
                continue
            thread_id = as_int(thread.get("no"))
            if thread_id is None:
                continue
            last_modified = as_int(thread.get("last_modified")) or 0
            bump = as_int(thread.get("bumplimit")) or 0
            threads.append((last_modified, bump, thread_id))
    threads.sort(reverse=True)
    seen: set[int] = set()
    ordered: list[int] = []
    for _last_modified, _bump, thread_id in threads:
        if thread_id in seen:
            continue
        seen.add(thread_id)
        ordered.append(thread_id)
        if len(ordered) >= max_threads:
            break
    return ordered


def catalog_api_url(board: str) -> str:
    board = normalize_board(board)
    return f"{API_BASE_URL}/{board}/catalog.json"


def thread_api_url(board: str, thread_id: int) -> str:
    board = normalize_board(board)
    if thread_id <= 0:
        raise ValueError("thread_id must be positive")
    return f"{API_BASE_URL}/{board}/thread/{thread_id}.json"


def post_web_url(board: str, thread_id: int, post_id: int) -> str:
    board = normalize_board(board)
    return f"https://boards.4chan.org/{board}/thread/{thread_id}#p{post_id}"


def normalize_board(board: str) -> str:
    board = board.strip().lower().strip("/")
    if not BOARD_PATTERN.match(board):
        raise ValueError(f"invalid 4chan board: {board!r}")
    return board


def is_allowed_api_url(url: str) -> bool:
    return bool(re.match(r"^https://a\.4cdn\.org/[a-z0-9]+/(?:catalog|thread/\d+)\.json$", url))


def parse_boards(value: str) -> list[str]:
    boards = [normalize_board(item) for item in value.split(",") if item.strip()]
    if not boards:
        raise ValueError("at least one 4chan board is required")
    return list(dict.fromkeys(boards))


def parse_quotas(value: str) -> dict[str, int]:
    quotas = dict(DEFAULT_QUOTAS)
    if not value:
        return quotas
    for item in value.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise ValueError(f"invalid quota item: {item!r}")
        key, raw_count = item.split("=", 1)
        key = key.strip().lower()
        if key not in DEFAULT_QUOTAS:
            raise ValueError(f"unknown quota category: {key!r}")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"invalid quota count for {key!r}: {raw_count!r}") from exc
        quotas[key] = max(0, count)
    return quotas


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect text-only 4chan negative validation candidates via the official JSON API."
    )
    parser.add_argument("--boards", default=DEFAULT_BOARDS)
    parser.add_argument("--out", type=Path, default=Path("data/validation/4chan_negative.jsonl"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/validation/4chan_negative_raw"))
    parser.add_argument("--state", type=Path, default=Path("data/validation/4chan_api_state.json"))
    parser.add_argument("--max-samples", type=int, default=200)
    parser.add_argument("--max-threads-per-board", type=int, default=10)
    parser.add_argument("--min-delay", type=float, default=1.1)
    parser.add_argument("--thread-refresh-min", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--min-text-chars", type=int, default=20)
    parser.add_argument("--quotas", default="adult=50,violence=50,illegal=50,privacy=50")
    synthetic_group = parser.add_mutually_exclusive_group()
    synthetic_group.add_argument(
        "--include-synthetic-privacy",
        dest="include_synthetic_privacy",
        action="store_true",
        default=True,
    )
    synthetic_group.add_argument(
        "--no-synthetic-privacy",
        dest="include_synthetic_privacy",
        action="store_false",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--verbose", action="store_true")
    return parser


def build_config(args: argparse.Namespace) -> FourChanConfig:
    boards = parse_boards(args.boards)
    quotas = parse_quotas(args.quotas)
    if args.min_delay < 1.0:
        raise ValueError("--min-delay must be at least 1.0 for the 4chan JSON API")
    return FourChanConfig(
        boards=boards,
        output_path=args.out,
        raw_dir=args.raw_dir,
        state_path=args.state,
        max_samples=max(0, args.max_samples),
        max_threads_per_board=max(0, args.max_threads_per_board),
        min_delay_sec=args.min_delay,
        thread_refresh_min_sec=max(10.0, args.thread_refresh_min),
        timeout_sec=args.timeout,
        retries=max(0, args.retries),
        min_text_chars=max(0, args.min_text_chars),
        quotas=quotas,
        include_synthetic_privacy=args.include_synthetic_privacy,
        user_agent=args.user_agent,
    )


def base_sample(
    sample_id: str,
    source_type: str,
    source_url: str,
    raw_path: Path,
    text: str,
    title: str,
    labels: list[str],
    usage_split: str,
    clean_status: str,
    label_status: str,
    risk_note: str,
    now: datetime,
) -> dict[str, object]:
    content_hash = sha256_text(text)
    return {
        "sample_id": sample_id,
        "source_type": source_type,
        "source_url": source_url,
        "source_ref": source_url,
        "collected_at": now.isoformat(),
        "modality": "text",
        "raw_content_ref": as_posix(raw_path),
        "clean_status": clean_status,
        "label_status": label_status,
        "labels": list(labels),
        "usage_split": usage_split,
        "risk_note": risk_note,
        "script_version": SCRIPT_VERSION,
        "content_hash": content_hash,
        "title": title,
        "text_excerpt": text[:300],
    }


def write_sample(output_path: Path, raw_path: Path, text: str, sample: Mapping[str, object]) -> None:
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(text, encoding="utf-8")
    with output_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(sample, ensure_ascii=False) + "\n")


def get_header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def as_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def as_posix(path: Path) -> str:
    return str(path).replace("\\", "/")


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
    collector = FourChanAPICollector(config)
    stats = collector.collect()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
