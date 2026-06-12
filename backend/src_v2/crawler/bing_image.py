"""
必应图片搜索爬虫
国际版必应，反爬相对宽松
"""

import random
import re
import time
from pathlib import Path
from typing import List

import requests


class BingImageCrawler:
    """必应图片搜索爬虫"""

    SEARCH_URL = "https://www.bing.com/images/async"

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]

    def __init__(self, delay: float = 0.5):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.bing.com/images/",
        })

    def search_images(self, keyword: str, num: int = 20) -> List[str]:
        """
        搜索必应图片，返回图片 URL 列表
        """
        urls = set()
        page = 0
        per_page = 35  # 必应每页默认35张
        max_pages = (num // per_page) + 2

        while len(urls) < num and page < max_pages:
            params = {
                "q": keyword,
                "first": page * per_page + 1,
                "count": per_page,
                "mmasync": 1,
            }

            try:
                resp = self.session.get(self.SEARCH_URL, params=params, timeout=10)
                resp.raise_for_status()

                # 从HTML中提取 murl（原图URL）
                murls = re.findall(r'"murl":"(https?://[^"]+)"', resp.text)
                # 提取 turl（缩略图URL）
                turls = re.findall(r'"turl":"(https?://[^"]+)"', resp.text)

                for url in murls + turls:
                    if url and url.startswith("http") and not url.startswith("data:"):
                        urls.add(url)

                if len(murls) == 0:
                    break

            except Exception as e:
                print(f"[WARN] Bing search failed for '{keyword}' page {page}: {e}")
                break

            page += 1
            time.sleep(self.delay)

        return list(urls)[:num]


if __name__ == "__main__":
    crawler = BingImageCrawler(delay=0.5)
    urls = crawler.search_images("kobe bryant meme", num=10)
    print(f"Found {len(urls)} URLs")
    for u in urls[:5]:
        print(f"  {u[:80]}")
