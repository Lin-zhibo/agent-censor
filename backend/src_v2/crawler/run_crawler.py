#!/usr/bin/env python3
"""爬虫入口脚本

Usage:
    python run_crawler.py --category safe --output ../../data/safe --num 200
    python run_crawler.py --category neutral --output ../../data/neutral --num 80
"""
import argparse
import sys
import time
from pathlib import Path

from baidu_image import BaiduImageCrawler


def main():
    parser = argparse.ArgumentParser(description="百度图片爬虫")
    parser.add_argument("--category", choices=["safe", "neutral", "harmful", "harmful_v2"], required=True)
    parser.add_argument("--keyword-file", type=str, default=None, help="自定义关键词文件路径")
    parser.add_argument("--output", type=str, required=True)
    parser.add_argument("--num", type=int, default=100, help="每关键词下载数量")
    parser.add_argument("--delay", type=float, default=0.5, help="请求间隔(秒)")
    parser.add_argument("--concurrent", type=int, default=20, help="并发数")
    args = parser.parse_args()

    # 读取关键词
    if args.keyword_file:
        keyword_file = Path(args.keyword_file)
    else:
        keyword_file = Path(__file__).parent / f"keywords_{args.category}.txt"
    if not keyword_file.exists():
        print(f"[ERROR] Keyword file not found: {keyword_file}")
        sys.exit(1)

    keywords = [line.strip() for line in keyword_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]

    print(f"Category: {args.category}")
    print(f"Keywords: {len(keywords)}")
    print(f"Output: {args.output}")
    print("-" * 50)

    crawler = BaiduImageCrawler(delay=args.delay)
    all_urls = []

    # 第一阶段: 收集URL
    for i, kw in enumerate(keywords, 1):
        print(f"[{i}/{len(keywords)}] Searching: {kw}")
        urls = crawler.search_images(kw, num=args.num)
        print(f"  Found {len(urls)} URLs")
        all_urls.extend(urls)

    # URL去重 (保持顺序)
    seen = set()
    unique_urls = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    print(f"\nTotal unique URLs: {len(unique_urls)}")

    # 第二阶段: 下载
    print("\nDownloading...")
    start = time.time()
    stats = crawler.download_images(
        unique_urls,
        output_dir=args.output,
        max_concurrent=args.concurrent,
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


if __name__ == "__main__":
    main()
