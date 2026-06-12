"""
搜狗图片搜索爬虫
搜狗图片与微信生态关联，表情包资源丰富
"""

import random
import re
import time
from pathlib import Path
from typing import List

import requests


class SogouImageCrawler:
    """搜狗图片搜索爬虫"""

    SEARCH_URL = "https://pic.sogou.com/pics"

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]

    def __init__(self, delay: float = 0.5):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://pic.sogou.com/",
        })

    def search_images(self, keyword: str, num: int = 20) -> List[str]:
        """
        搜索搜狗图片，返回图片 URL 列表
        """
        urls = set()
        page = 1
        per_page = 20
        max_pages = (num // per_page) + 2

        while len(urls) < num and page <= max_pages:
            params = {
                "query": keyword,
                "mode": 1,
                "start": (page - 1) * per_page,
                "reqType": "ajax",
                "reqFrom": "result",
                "tn": 0,
            }

            try:
                resp = self.session.get(self.SEARCH_URL, params=params, timeout=10)
                resp.raise_for_status()

                # 搜狗图片返回的是HTML片段或JSON
                # 尝试多种方式提取URL
                # 方式1: 直接从HTML中提取 thumbUrl
                thumb_urls = re.findall(r'"thumbUrl":"(https?://[^"]+)"', resp.text)
                # 方式2: 提取 pic_url
                pic_urls = re.findall(r'"pic_url":"(https?://[^"]+)"', resp.text)
                # 方式3: 提取 ori_pic_url
                ori_urls = re.findall(r'"ori_pic_url":"(https?://[^"]+)"', resp.text)

                for url in thumb_urls + pic_urls + ori_urls:
                    if url and url.startswith("http"):
                        urls.add(url)

                # 如果正则没匹配到，尝试解析JSON
                if len(urls) == 0:
                    try:
                        data = resp.json()
                        items = data.get("items", [])
                        for item in items:
                            url = item.get("thumbUrl") or item.get("pic_url") or item.get("ori_pic_url")
                            if url and url.startswith("http"):
                                urls.add(url)
                    except:
                        pass

                if len(thumb_urls) == 0 and len(pic_urls) == 0:
                    break

            except Exception as e:
                print(f"[WARN] Sogou search failed for '{keyword}' page {page}: {e}")
                break

            page += 1
            time.sleep(self.delay)

        return list(urls)[:num]


if __name__ == "__main__":
    crawler = SogouImageCrawler(delay=0.5)
    urls = crawler.search_images("科比表情包", num=10)
    print(f"Found {len(urls)} URLs")
    for u in urls[:5]:
        print(f"  {u[:80]}")
