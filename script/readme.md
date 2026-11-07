这个目录下是一些脚本，比如爬虫脚本/模型训练脚本。

## 验证集爬虫

`crawl_validation_data.py` 用于把公开或授权网页采集为验证集 JSONL 样本。

社交媒体/社区内容审核实验优先使用 `config/crawler/social_*.txt`：

```powershell
python script/crawl_validation_data.py --seeds-file config/crawler/social_discourse_forums_validation.txt --out data/validation/social_forums.jsonl --raw-dir data/validation/social_forums_raw --state data/validation/social_forums_state.json --max-pages 40 --max-depth 1 --min-delay 5 --max-delay 10 --source-type "公开社交媒体/社区讨论"
```

Hacker News 需要更慢，因为 robots 指定 Crawl-delay: 30：

```powershell
python script/crawl_validation_data.py --seeds-file config/crawler/social_hackernews_validation.txt --out data/validation/social_hn.jsonl --raw-dir data/validation/social_hn_raw --state data/validation/social_hn_state.json --max-pages 30 --max-depth 1 --min-delay 30 --max-delay 45 --source-type "公开社交新闻/评论"
```

通用示例：

```powershell
python script/crawl_validation_data.py --seed https://example.com --max-pages 20 --max-depth 1
```

默认输出：

- `data/validation/crawl_validation.jsonl`：验证集样本记录。
- `data/validation/raw/`：原始文本内容。
- `data/validation/crawl_state.json`：断点续爬状态。

脚本内置的反爬应对只包含合规稳定性措施：robots 检查、同域限制、限速和随机抖动、429/5xx 退避重试、断点续爬、内容去重、访问受限/验证码页跳过。不包含验证码破解、登录绕过、Cookie 窃取、代理池规避封禁等能力。

小红书、微博、知乎、Reddit、X/Twitter、Instagram、Facebook 这类平台不要直接放进默认批量爬取配置。它们经常有登录墙、安全验证、robots 限制、前端渲染或单独 API/授权要求；需要时使用官方 API、授权导出数据，或把已授权的公开 URL 写入 `config/crawler/social_chinese_authorized_template.txt` 后小批量试跑。

## 4chan JSON API 负类候选采集

`collect_4chan_api.py` 只使用 4chan 官方 read-only JSON API，默认只保存文本，不下载图片/视频，不请求媒体域名，并对邮箱、手机号、IP、地址、社交账号等个人信息做脱敏。

```powershell
python script/collect_4chan_api.py --max-samples 200 --max-threads-per-board 10 --min-delay 1.1 --quotas adult=50,violence=50,illegal=50,privacy=50
```

默认输出：

- `data/validation/4chan_negative.jsonl`：负类候选验证集样本记录。
- `data/validation/4chan_negative_raw/`：脱敏后的文本内容。
- `data/validation/4chan_api_state.json`：`Last-Modified`、线程刷新时间和去重状态。

这些样本的 `label_status` 是 `待人工复核`，不能直接当作人工真值。
