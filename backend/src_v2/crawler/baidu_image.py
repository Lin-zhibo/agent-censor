"""百度图片爬虫 - 异步下载 + 感知哈希去重"""

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
    """百度图片搜索爬虫"""

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

    def search_images(self, keyword: str, num: int = 60) -> List[str]:
        """
        搜索关键词，返回图片 URL 列表
        Args:
            keyword: 搜索关键词
            num: 需要获取的图片数量
        Returns:
            List[str]: 图片 URL 列表
        """
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
                # 百度返回的JSON包含无效转义，使用正则提取URL
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
        min_size: int = 224,
        existing_hashes: Set[str] = None,
    ) -> bool:
        """
        下载单张图片，检查尺寸和重复
        Returns:
            bool: True=成功保存, False=跳过或失败
        """
        if existing_hashes is None:
            existing_hashes = set()

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

                filename = f"{len(existing_hashes):05d}.jpg"
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
    ) -> dict:
        """
        并发下载图片
        Args:
            urls: 图片URL列表
            output_dir: 保存目录
            max_concurrent: 最大并发数
            min_size: 最小尺寸
        Returns:
            dict: {saved: int, skipped: int, failed: int}
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        existing_hashes: Set[str] = set()
        stats = {"saved": 0, "skipped": 0, "failed": 0}

        async def _run():
            connector = aiohttp.TCPConnector(limit=max_concurrent)
            headers = {"User-Agent": random.choice(self.USER_AGENTS)}

            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                semaphore = asyncio.Semaphore(max_concurrent)

                async def _bounded_download(url):
                    async with semaphore:
                        return await self._download_single(
                            session, url, output_path, min_size, existing_hashes
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
