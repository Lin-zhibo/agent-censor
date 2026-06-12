#!/usr/bin/env python3
"""
微博图片爬虫 - Playwright 浏览器自动化
支持长时间后台运行，自动滚动加载，断点续传

Usage:
    # 前台运行（调试用）
    python weibo_playwright.py --keywords "科比表情包,牢大梗图" --output ../../data/harmful_weibo --num 50

    # 后台运行一整晚（Windows）
    start /B python weibo_playwright.py --keywords-file keywords_harmful_v2.txt --output ../../data/harmful_weibo --num 100

    # 后台运行一整晚（Linux/Mac）
    nohup python weibo_playwright.py --keywords-file keywords_harmful_v2.txt --output ../../data/harmful_weibo --num 100 > weibo.log 2>&1 &
"""

import argparse
import asyncio
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import List, Set

from playwright.async_api import async_playwright


class WeiboPlaywrightCrawler:
    """基于 Playwright 的微博图片爬虫"""

    def __init__(self, output_dir: str, delay: float = 2.0, headless: bool = True):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.headless = headless
        self.existing_urls: Set[str] = set()
        self.stats = {"searched": 0, "found": 0, "saved": 0, "failed": 0}
        self.progress_file = self.output_dir / ".progress.json"
        self._load_progress()

    def _load_progress(self):
        """加载进度，支持断点续传"""
        if self.progress_file.exists():
            with open(self.progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.existing_urls = set(data.get("urls", []))
                self.stats = data.get("stats", self.stats)
            print(f"[INFO] Loaded progress: {len(self.existing_urls)} URLs already collected")

    def _save_progress(self):
        """保存进度"""
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump({
                "urls": list(self.existing_urls),
                "stats": self.stats,
                "last_update": datetime.now().isoformat(),
            }, f, indent=2, ensure_ascii=False)

    async def _scroll_and_collect(self, page, keyword: str, target_num: int) -> List[str]:
        """
        访问微博搜索页面，滚动加载，收集图片URL
        """
        urls: List[str] = []
        max_scrolls = 20
        no_new_count = 0

        # 构建微博搜索URL（PC端）
        encoded_kw = keyword.replace(" ", "%20")
        search_url = f"https://s.weibo.com/weibo?q={encoded_kw}&typeall=1&suball=1&timescope=custom:0-0&page=1"

        try:
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(self.delay)

            for scroll in range(max_scrolls):
                # 提取当前页面的图片URL
                # 微博图片通常在 .card-wrap .pic img 或类似的selector中
                img_urls = await page.evaluate("""
                    () => {
                        const urls = [];
                        // 尝试多种selector
                        const selectors = [
                            '.card-wrap img',
                            '.m-img-box img',
                            'article img',
                            '.vue-recycle-scroller__item-view img',
                            'img[src*="sinaimg.cn"]',
                            'img[src*="weibo.cn"]',
                        ];
                        for (const sel of selectors) {
                            document.querySelectorAll(sel).forEach(img => {
                                if (img.src && img.src.startsWith('http')) {
                                    urls.push(img.src);
                                }
                                // 有些图片在data-src中
                                if (img.dataset && img.dataset.src && img.dataset.src.startsWith('http')) {
                                    urls.push(img.dataset.src);
                                }
                            });
                        }
                        return [...new Set(urls)];
                    }
                """)

                before_len = len(urls)
                for u in img_urls:
                    if u not in self.existing_urls and u not in urls:
                        urls.append(u)

                new_found = len(urls) - before_len
                print(f"  [Scroll {scroll+1}/{max_scrolls}] Found {new_found} new URLs (total: {len(urls)})")

                if len(urls) >= target_num:
                    break

                if new_found == 0:
                    no_new_count += 1
                    if no_new_count >= 3:
                        print(f"  [INFO] No new images for 3 scrolls, stopping")
                        break
                else:
                    no_new_count = 0

                # 滚动页面
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(self.delay + random.uniform(0.5, 1.5))

        except Exception as e:
            print(f"  [WARN] Error during search for '{keyword}': {e}")

        return urls[:target_num]

    async def _download_images(self, urls: List[str]):
        """下载图片"""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            for i, url in enumerate(urls, 1):
                if url in self.existing_urls:
                    continue

                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            if len(content) < 1024:
                                continue

                            # 检查是否是图片
                            content_type = resp.headers.get("content-type", "")
                            if not ("image" in content_type or len(content) > 5000):
                                continue

                            filename = f"weibo_{len(self.existing_urls) + 1:05d}.jpg"
                            save_path = self.output_dir / filename

                            with open(save_path, "wb") as f:
                                f.write(content)

                            self.existing_urls.add(url)
                            self.stats["saved"] += 1

                            if i % 10 == 0:
                                self._save_progress()

                except Exception as e:
                    self.stats["failed"] += 1
                    print(f"  [WARN] Download failed: {url[:60]}... | {e}")

                await asyncio.sleep(random.uniform(0.3, 0.8))

    async def crawl(self, keywords: List[str], num_per_keyword: int = 50):
        """主爬取流程"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            # 先访问微博首页，建立cookie
            try:
                await page.goto("https://weibo.com", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
            except:
                pass

            for i, keyword in enumerate(keywords, 1):
                print(f"\n[{i}/{len(keywords)}] Searching Weibo: {keyword}")
                self.stats["searched"] += 1

                urls = await self._scroll_and_collect(page, keyword, num_per_keyword)
                self.stats["found"] += len(urls)
                print(f"  Collected {len(urls)} URLs, downloading...")

                await self._download_images(urls)
                self._save_progress()

                # 关键词间休息
                if i < len(keywords):
                    rest = random.uniform(3, 6)
                    print(f"  Resting {rest:.1f}s before next keyword...")
                    await asyncio.sleep(rest)

            await browser.close()

        print(f"\n{'='*50}")
        print("Weibo crawling completed!")
        print(f"  Keywords searched: {self.stats['searched']}")
        print(f"  URLs found: {self.stats['found']}")
        print(f"  Images saved: {self.stats['saved']}")
        print(f"  Failed: {self.stats['failed']}")
        print(f"  Total unique: {len(self.existing_urls)}")
        print(f"  Output: {self.output_dir}")


def main():
    parser = argparse.ArgumentParser(description="微博图片爬虫 (Playwright)")
    parser.add_argument("--keywords", type=str, default=None, help="逗号分隔的关键词")
    parser.add_argument("--keywords-file", type=str, default=None, help="关键词文件路径")
    parser.add_argument("--output", type=str, required=True, help="输出目录")
    parser.add_argument("--num", type=int, default=50, help="每关键词目标数量")
    parser.add_argument("--delay", type=float, default=2.0, help="请求间隔(秒)")
    parser.add_argument("--headless", action="store_true", default=True, help="无头模式")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="显示浏览器窗口")
    args = parser.parse_args()

    # 读取关键词
    keywords = []
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",")]
    elif args.keywords_file:
        with open(args.keywords_file, "r", encoding="utf-8") as f:
            keywords = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    else:
        print("[ERROR] 请提供 --keywords 或 --keywords-file")
        return

    print(f"Keywords: {len(keywords)}")
    print(f"Output: {args.output}")
    print(f"Headless: {args.headless}")
    print("=" * 50)

    crawler = WeiboPlaywrightCrawler(
        output_dir=args.output,
        delay=args.delay,
        headless=args.headless,
    )
    asyncio.run(crawler.crawl(keywords, args.num))


if __name__ == "__main__":
    main()
