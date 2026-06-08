这个目录下是一些脚本，比如爬虫脚本/模型训练脚本。

## 验证集爬虫

`crawl_validation_data.py` 用于把公开或授权网页采集为验证集 JSONL 样本。

示例：

```powershell
python script/crawl_validation_data.py --seed https://example.com --max-pages 20 --max-depth 1
```

默认输出：

- `data/validation/crawl_validation.jsonl`：验证集样本记录。
- `data/validation/raw/`：原始文本内容。
- `data/validation/crawl_state.json`：断点续爬状态。

脚本内置的反爬应对只包含合规稳定性措施：robots 检查、同域限制、限速和随机抖动、429/5xx 退避重试、断点续爬、内容去重、访问受限/验证码页跳过。不包含验证码破解、登录绕过、Cookie 窃取、代理池规避封禁等能力。
