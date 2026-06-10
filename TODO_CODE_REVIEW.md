# TODO — 代码审查发现 (lzb 分支, 2026-06-09)

> 审查级别: xhigh 召回模式 | 9 角度 → 10 项发现

---

## 🔴 CRITICAL — 必须立即修复

### TODO-1: `normalize_label` 去掉下划线，破坏所有复合标签名

- **文件**: `core/agent/rule_tree.py`
- **行号**: ~924
- **严重度**: 🔴 CRITICAL
- **状态**: [ ] 待修复

**问题**: `re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()` 会去掉下划线，导致
`"SUPPORT_SUICIDE"` → `"SUPPORTSUICIDE"`。所有约 60+ 个带下划线的复合标签名
在输出中丢失下划线，与 `settings.json` 中显式声明的 `"label"` 字段值不一致。

**触发**: 任何包含下划线的标签名（`SUPPORT_SUICIDE`, `SEX_SERVICE`, `ECO_DIVERSION` 等）

**修复**: 将正则改为 `r"[^A-Za-z0-9_]"` 保留下划线。

---

### TODO-2: `normalize_url` IDNA 编码对 IPv6/畸形 hostname 抛出未捕获异常

- **文件**: `script/crawl_validation_data.py`
- **行号**: ~438 (工作区未提交更改)
- **严重度**: 🔴 CRITICAL
- **状态**: [ ] 待修复

**问题**: `parsed.netloc.encode("idna").decode("ascii")` 对 IPv6 地址（如 `[::1]`）、
非标准 hostname、国际化编码失败的域名会抛出 `UnicodeError`，缺少 try/except。

**触发**: 爬取的网页中含有指向 IPv6 地址或畸形 hostname 的链接 → 爬虫直接崩溃。

**修复**: 添加 try/except，编码失败时回退到原始 netloc 或跳过该 URL。

---

### TODO-3: `settings.json` 存在重复标签 `SUPPORT_NEGATIVE`

- **文件**: `config/settings.json`
- **行号**: ~67 和 ~187
- **严重度**: 🔴 CRITICAL
- **状态**: [ ] 待修复

**问题**: 同一个标签 `SUPPORT_NEGATIVE` 出现在两个不同父节点下：
- `DISTORT_TRADITION → SUPPORT_NEGATIVE`（支持负面人物，行~67）
- `NEGATIVE_FIGURE → SUPPORT_NEGATIVE`（支持社会负面人物，行~187）

`normalize_label` 将两者都规范化为 `SUPPORTNEGATIVE`，导致同一违规被两个节点重复报告。

**触发**: 当外部系统传入 `rule_results` 包含 `SUPPORT_NEGATIVE` 时，两个叶子节点都命中。

**修复**: 将其中一个重命名为不同标签（如 `SUPPORT_HISTORICAL_NEGATIVE`），确保标签全局唯一。

---

## 🟡 HIGH — 尽快修复

### TODO-4: `RootAgent` 代码与文档架构不一致

- **文件**: `core/agent/agents.py:720-766`, `doc/09-树型多智能体审核设计.md`
- **严重度**: 🟡 HIGH
- **状态**: [ ] 待修复

**问题**: 文档明确说 "security 和 ecosystem 不对应一层智能体，只是一级标签的领域属性"，
但代码中 `from_settings` 用 `force_tree=True` 创建了 SecurityAgent 和 EcosystemAgent 
作为 0.5 级中间智能体层，RootAgent 通过它们间接调度一级智能体。

**影响**: 增加一层不必要的调度；后续 LLM 提示词会基于文档生成，与运行时行为不匹配。

**修复**: 二选一：
A. 修改代码：RootAgent 直接遍历一级子智能体，按 domain 归类结果
B. 修改文档：承认 security/ecosystem 为 0.5 级汇总 Agent

---

### TODO-5: 关键词纯子串匹配导致误命中

- **文件**: `core/agent/tool/local.py`
- **行号**: ~1405
- **严重度**: 🟡 HIGH
- **状态**: [ ] 待修复

**问题**: `keyword.lower() in text` 是纯子串匹配。关键词 `"ad"` 会匹配 `"bad"`、
`"graduate"` 等；单字关键词广泛命中。当前 settings.json 中所有 keywords 为空数组，
暂时不触发，但一旦有人填入短关键词，误命中立即出现。

**触发**: 配置 `"keywords": ["ad"]` → 所有含 "bad", "header", "graduate" 等内容均误命中。

**修复**: 使用词边界匹配（`\bkeyword\b`）或分词后精确匹配。

---

### TODO-6: `moderate()` 对缺少 `trace_id` 的 dict 输入崩溃

- **文件**: `core/agent/agents.py`
- **行号**: ~805
- **严重度**: 🟡 HIGH
- **状态**: [ ] 待修复

**问题**: `AuditContext(**context)` 中 `trace_id` 无默认值，调用方传入不含 `trace_id` 的
dict 时抛出 `TypeError: missing 1 required positional argument: 'trace_id'`。

**触发**: `moderator.moderate({"content": {"text": "hello"}})` → TypeError crash。

**修复**: 给 `trace_id` 添加默认值（如 `""`）并生成 fallback UUID，或在 `moderate` 中补全。

---

### TODO-7: 爬虫自定义 fetcher 绕过限速

- **文件**: `script/crawl_validation_data.py`
- **行号**: ~2079
- **严重度**: 🟡 HIGH
- **状态**: [ ] 待修复

**问题**: `crawl()` 直接调用 `self.fetcher(url)` 不经过 `rate_limiter.wait()`。
默认 `_fetch` 内部有限速，但通过构造函数传入的自定义 `fetcher` 替换了 `_fetch`，
限速被跳过。对比 4chan collector 的 `_fetch_json` 正确处理了这种情况。

**触发**: 生产环境使用自定义 fetcher → 无延迟轰炸目标服务器。

**修复**: 将 `rate_limiter.wait(url)` 移到 `crawl()` 方法中，在调用 `self.fetcher` 之前执行。

---

## 🟡 MEDIUM — 计划修复

### TODO-8: 4 个工具函数在多个文件中重复定义

- **文件**: `rule_tree.py`, `tool/local.py`, `crawl_validation_data.py`, `collect_4chan_api.py`
- **严重度**: 🟡 MEDIUM
- **状态**: [ ] 待修复

**重复函数**:

| 函数 | 文件 1 | 文件 2 |
|------|--------|--------|
| `_as_int` | `core/agent/rule_tree.py:1025` | `core/agent/tool/local.py:1493` |
| `normalize_text` | `script/crawl_validation_data.py:2277` | `script/collect_4chan_api.py:911` |
| `sha256_text` | `script/crawl_validation_data.py:2289` | `script/collect_4chan_api.py:915` |
| `as_posix` | `script/crawl_validation_data.py:2298` | `script/collect_4chan_api.py:919` |

**修复**: 提取到共享工具模块（`core/utils.py` 和 `script/utils.py`）。

---

### TODO-9: 启动时全量解析输出 JSONL 重建 hash 集合（与 state 文件重复 I/O）

- **文件**: `script/crawl_validation_data.py:2252`, `script/collect_4chan_api.py:535`
- **严重度**: 🟡 MEDIUM
- **状态**: [ ] 待修复

**问题**: `_load_seen_hashes_from_output` 逐行解析整个 JSONL 输出文件来重建 hash 集合，
但 `seen_content_hashes` 已经持久化在 state 文件中。重复 I/O。

**触发**: 10 万+ 条记录 → 启动耗时数秒甚至数十秒。

**修复**: 删除 `_load_seen_hashes_from_output` 调用，仅依赖 `_load_state` 恢复 hash 集合。

---

## 🔵 LOW — 后续优化

### TODO-10: Parquet 追加写入每批全量读+写，O(n²) I/O

- **文件**: `script/download_hf_4chan_datasets.py`
- **行号**: ~335-358
- **严重度**: 🔵 LOW
- **状态**: [ ] 待优化

**问题**: `_write_or_append` 每批（默认 10,000 行）读整个 Parquet → 列对齐 → 写回整个文件。
百万行数据 → 100+ 次全量读写。

**修复**: 写为独立的分片 Parquet 文件（如 `data_part_0001.parquet`），下载完成后可选合并。
