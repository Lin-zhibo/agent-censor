#!/usr/bin/env python3
"""
补充 harmful 数据 - 追加到现有目录
用法: python supplement_harmful.py
"""
import asyncio
import random
import re
import time
from io import BytesIO
from pathlib import Path
from typing import List, Set

import aiohttp
import imagehash
import requests
from PIL import Image


class BaiduImageCrawler:
    """百度图片搜索爬虫 - 追加模式"""

    SEARCH_URL = "https://image.baidu.com/search/acjson"

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]

    def __init__(self, delay: float = 0.5):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "text/plain, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://image.baidu.com/",
        })

    def search_images(self, keyword: str, num: int = 120) -> List[str]:
        """搜索关键词，返回图片 URL 列表"""
        urls = set()
        page = 0
        per_page = 30
        max_pages = (num // per_page) + 2

        while len(urls) < num and page < max_pages:
            params = {
                "tn": "resultjson_com",
                "word": keyword,
                "pn": page * per_page,
                "rn": per_page,
                "gsm": "1e",
            }

            try:
                resp = self.session.get(self.SEARCH_URL, params=params, timeout=10)
                resp.raise_for_status()
                thumb_urls = re.findall(r'"thumbURL":"(.*?)"', resp.text)
                middle_urls = re.findall(r'"middleURL":"(.*?)"', resp.text)

                for url in thumb_urls + middle_urls:
                    if url and url.startswith("http"):
                        urls.add(url)

                if len(thumb_urls) == 0:
                    break

            except Exception as e:
                print(f"[WARN] Search failed for '{keyword}' page {page}: {e}")
                break

            page += 1
            time.sleep(self.delay)

        return list(urls)[:num]

    async def _download_single(
        self,
        session: aiohttp.ClientSession,
        url: str,
        output_dir: Path,
        min_size: int,
        existing_hashes: Set[str],
        counter: List[int],
    ) -> bool:
        """下载单张图片，检查尺寸和重复"""
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return False

                content = await resp.read()
                if len(content) < 1024:
                    return False

                img = Image.open(BytesIO(content))

                # 检查尺寸
                if img.width < min_size or img.height < min_size:
                    return False

                # 感知哈希去重
                img_hash = str(imagehash.phash(img))
                if img_hash in existing_hashes:
                    return False
                existing_hashes.add(img_hash)

                # 统一转为RGB+JPEG
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                counter[0] += 1
                filename = f"{counter[0]:05d}.jpg"
                save_path = output_dir / filename
                img.save(save_path, "JPEG", quality=95)

                return True

        except Exception as e:
            print(f"[WARN] Download failed: {url[:60]}... | {e}")
            return False

    def download_images(
        self,
        urls: List[str],
        output_dir: str,
        max_concurrent: int = 20,
        min_size: int = 224,
        start_idx: int = 0,
        existing_hashes: Set[str] = None,
    ) -> dict:
        """并发下载图片，支持追加模式"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if existing_hashes is None:
            existing_hashes = set()
        stats = {"saved": 0, "skipped": 0, "failed": 0}
        counter = [start_idx]

        async def _run():
            connector = aiohttp.TCPConnector(limit=max_concurrent)
            headers = {"User-Agent": random.choice(self.USER_AGENTS)}

            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                semaphore = asyncio.Semaphore(max_concurrent)

                async def _bounded_download(url):
                    async with semaphore:
                        return await self._download_single(
                            session, url, output_path, min_size, existing_hashes, counter
                        )

                results = await asyncio.gather(*[_bounded_download(u) for u in urls])

                for ok in results:
                    if ok:
                        stats["saved"] += 1
                    else:
                        stats["failed"] += 1

        asyncio.run(_run())
        stats["skipped"] = len(urls) - stats["saved"] - stats["failed"]
        return stats


def load_existing_hashes(output_dir: str) -> Set[str]:
    """加载已有图片的感知哈希"""
    hashes = set()
    path = Path(output_dir)
    if not path.exists():
        return hashes

    for img_path in sorted(path.glob("*.jpg")):
        try:
            img = Image.open(img_path)
            h = str(imagehash.phash(img))
            hashes.add(h)
        except Exception:
            pass

    return hashes


def get_start_idx(output_dir: str) -> int:
    """获取下一个可用编号"""
    path = Path(output_dir)
    if not path.exists():
        return 0

    max_idx = 0
    for p in path.glob("*.jpg"):
        try:
            idx = int(p.stem)
            max_idx = max(max_idx, idx)
        except ValueError:
            pass

    return max_idx


def main():
    keyword_file = Path(__file__).parent / "keywords_harmful_v2.txt"
    output_dir = Path("../../data/harmful")

    # 读取关键词
    keywords = [line.strip() for line in keyword_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]

    # 加载已有数据状态
    existing_hashes = load_existing_hashes(str(output_dir))
    start_idx = get_start_idx(str(output_dir))

    print(f"Keywords: {len(keywords)}")
    print(f"Existing images: {start_idx}")
    print(f"Existing hashes: {len(existing_hashes)}")
    print("-" * 50)

    crawler = BaiduImageCrawler(delay=0.5)
    all_urls = []

    # 第一阶段: 收集URL（每关键词150张）
    for i, kw in enumerate(keywords, 1):
        print(f"[{i}/{len(keywords)}] Searching: {kw}")
        urls = crawler.search_images(kw, num=150)
        print(f"  Found {len(urls)} URLs")
        all_urls.extend(urls)

    # URL去重
    seen = set()
    unique_urls = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    print(f"\nTotal unique URLs: {len(unique_urls)}")

    # 第二阶段: 下载（追加模式）
    print("\nDownloading (append mode)...")
    start = time.time()
    stats = crawler.download_images(
        unique_urls,
        output_dir=str(output_dir),
        max_concurrent=20,
        start_idx=start_idx,
        existing_hashes=existing_hashes,
    )
    elapsed = time.time() - start

    print("\n" + "=" * 50)
    print("Done!")
    print(f"  Saved:   {stats['saved']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Failed:  {stats['failed']}")
    if elapsed > 0 and stats['saved'] > 0:
        print(f"  Speed:   {stats['saved'] / elapsed * 60:.0f} img/min")
    print(f"  Time:    {elapsed:.1f}s")
    print(f"  Total harmful images: {start_idx + stats['saved']}")


if __name__ == "__main__":
    main()
