# 百度图片爬虫

## 安装依赖

```bash
pip install requests aiohttp Pillow imagehash
```

## 使用

### 1. 爬取 Safe 类（Kobe Bryant 正常照片）

```bash
cd src_v2/crawler
python run_crawler.py --category safe --output ../../data/safe --num 200
```

### 2. 爬取 Neutral 类（其他运动员）

```bash
python run_crawler.py --category neutral --output ../../data/neutral --num 80
```

### 3. 自定义关键词

编辑 `keywords_safe.txt` 或 `keywords_neutral.txt`，每行一个关键词。

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--category` | safe 或 neutral | 必填 |
| `--output` | 输出目录 | 必填 |
| `--num` | 每关键词下载数量 | 100 |
| `--delay` | 请求间隔(秒) | 0.5 |
| `--concurrent` | 并发下载数 | 20 |

## 去重机制

1. **URL去重**: 同一URL只下载一次
2. **感知哈希去重**: 相同/相似图片只保留一张
3. **尺寸过滤**: 小于224x224的图片丢弃

## Python API

```python
from baidu_image import BaiduImageCrawler

crawler = BaiduImageCrawler(delay=0.5)

# 搜索URL
urls = crawler.search_images("Kobe Bryant", num=50)

# 下载
stats = crawler.download_images(urls, output_dir="data/safe")
print(f"Saved: {stats['saved']}")
```
