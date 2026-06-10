# 验证集 URL 配置

这些文件供 `script/crawl_validation_data.py --seeds-file ...` 使用。

规则：

- 一行一个 URL。
- 空行会忽略。
- `#` 开头的行是注释。
- 只放公开或已授权来源。
- 运行前仍应低速、小批量试跑，脚本会继续检查 robots。

## 社交媒体/社区实验配置

默认做内容审核实验，应优先使用下面这些社交或社区讨论类配置，而不是论文、百科、文档站点。

公开论坛/社区讨论：

```powershell
python script\crawl_validation_data.py --seeds-file config\crawler\social_discourse_forums_validation.txt --out data\validation\social_forums.jsonl --raw-dir data\validation\social_forums_raw --state data\validation\social_forums_state.json --max-pages 40 --max-depth 1 --min-delay 5 --max-delay 10 --source-type "公开社交媒体/社区讨论"
```

Hacker News 社交新闻和评论。该站 robots 指定 Crawl-delay: 30，所以必须更慢：

```powershell
python script\crawl_validation_data.py --seeds-file config\crawler\social_hackernews_validation.txt --out data\validation\social_hn.jsonl --raw-dir data\validation\social_hn_raw --state data\validation\social_hn_state.json --max-pages 30 --max-depth 1 --min-delay 30 --max-delay 45 --source-type "公开社交新闻/评论"
```

Fediverse / Lemmy / Mastodon 公开页：

```powershell
python script\crawl_validation_data.py --seeds-file config\crawler\social_fediverse_validation.txt --out data\validation\social_fediverse.jsonl --raw-dir data\validation\social_fediverse_raw --state data\validation\social_fediverse_state.json --max-pages 30 --max-depth 1 --min-delay 8 --max-delay 15 --source-type "公开去中心化社交媒体"
```

中文社交平台不要直接默认批量爬。`social_chinese_authorized_template.txt` 只给小红书、微博、知乎这类平台的 URL 形态示例；只有在有授权、页面公开允许、robots 检查通过时才取消注释。

## 普通公开文本基线配置

下面这些不是社交媒体，只适合做普通文本基线或爬虫连通性验证：

```powershell
python script\crawl_validation_data.py --seeds-file config\crawler\wikimedia_zh_validation.txt --max-pages 20 --max-depth 0 --min-delay 2 --max-delay 5
```

```powershell
python script\crawl_validation_data.py --seeds-file config\crawler\open_docs_validation.txt --max-pages 20 --max-depth 1 --min-delay 2 --max-delay 5
```

arXiv 建议更慢：

```powershell
python script\crawl_validation_data.py --seeds-file config\crawler\arxiv_research_validation.txt --max-pages 10 --max-depth 0 --min-delay 15 --max-delay 20
```

Project Gutenberg 建议使用官方 robot harvest 入口：

```powershell
python script\crawl_validation_data.py --seeds-file config\crawler\gutenberg_books_validation.txt --max-pages 20 --max-depth 1 --min-delay 2 --max-delay 5
```
