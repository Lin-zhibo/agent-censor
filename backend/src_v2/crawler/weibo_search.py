"""
微博搜索图片爬虫
通过移动端搜索页面获取微博配图
"""

import json
import random
import re
import time
from pathlib import Path
from typing import List

import requests


class WeiboImageCrawler:
    """微博搜索图片爬虫"""

    SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"

    USER_AGENTS = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    ]

    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://m.weibo.cn/",
            "X-Requested-With": "XMLHttpRequest",
        })

    def search_images(self, keyword: str, num: int = 20) -> List[str]:
        """
        搜索微博关键词，返回配图 URL 列表
        Args:
            keyword: 搜索关键词
            num: 需要获取的图片数量
        Returns:
            List[str]: 图片 URL 列表
        """
        urls = set()
        page = 1
        max_pages = (num // 10) + 3

        while len(urls) < num and page <= max_pages:
            # 微博搜索 containerid 格式
            containerid = f"100103type=1&q={keyword}"

            params = {
                "containerid": containerid,
                "page_type": "searchall",
                "page": page,
            }

            try:
                resp = self.session.get(self.SEARCH_URL, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                if data.get("ok") != 1:
                    print(f"[WARN] Weibo API returned ok={data.get('ok')} for '{keyword}' page {page}")
                    break

                cards = data.get("data", {}).get("cards", [])

                for card in cards:
                    # 提取微博配图
                    if "mblog" in card:
                        mblog = card["mblog"]
                        pics = mblog.get("pics", [])
                        for pic in pics:
                            url = pic.get("large", {}).get("url") or pic.get("url")
                            if url and url.startswith("http"):
                                urls.add(url)

                    # 提取card中的图片
                    elif "card_group" in card:
                        for c in card.get("card_group", []):
                            if "mblog" in c:
                                mblog = c["mblog"]
                                pics = mblog.get("pics", [])
                                for pic in pics:
                                    url = pic.get("large", {}).get("url") or pic.get("url")
                                    if url and url.startswith("http"):
                                        urls.add(url)

                if len(cards) == 0:
                    break

            except Exception as e:
                print(f"[WARN] Weibo search failed for '{keyword}' page {page}: {e}")
                break

            page += 1
            time.sleep(self.delay)

        return list(urls)[:num]


if __name__ == "__main__":
    crawler = WeiboImageCrawler(delay=1.0)
    urls = crawler.search_images("科比表情包", num=10)
    print(f"Found {len(urls)} URLs")
    for u in urls[:5]:
        print(f"  {u[:80]}")
