# daily-posts

每日自动抓取技术 RSS、生成摘要与标签，并输出可订阅 Feed（JSON Feed / RSS XML）与可导入订阅列表（OPML）。

## 功能总览

项目提供两条可用流程：

1. `run_full_pipeline.py`（步骤 1~5，推荐）
- 步骤1：抓取博客 RSS 源（支持 fallback）
- 步骤2：拉取当天文章并输出原始 JSON
- 步骤3：AI 摘要与标签增强（支持 `openai` / `hf-local` / `none`）
- 步骤4：生成 RSS XML + OPML（NetNewsWire 导入）
- 步骤5：复制 RSS/OPML 到发布路径（默认仓库根目录）

2. `run_daily_pipeline.py`（步骤 2~4，偏定时任务）
- 从 `feeds.txt` 抓取当天文章
- 执行 AI 增强
- 生成 JSON Feed（`output/YYYY-MM-DD.feed.json`）
- 输出 fetch/ai/pipeline 统计与日志

## 环境准备

```bash
cd /Users/xuying/playground/daily-posts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

可选（OpenAI 模式需要）：

```bash
export OPENAI_API_KEY="your_api_key"
```

## 快速开始

### 方案A：一键跑完整 1~5 步

```bash
source .venv/bin/activate
python scripts/run_full_pipeline.py \
  --allow-fallback \
  --skip-ai-if-no-key \
  --pretty \
  --feed-link "https://<YOUR_GH_USERNAME>.github.io/daily-posts/" \
  --feed-self-link "https://<YOUR_GH_USERNAME>.github.io/daily-posts/post.xml" \
  --feed-output "post.xml" \
  --publish-path "post.xml" \
  --skip-opml
```

产物默认位置：
- `data/blog_feeds.json`
- `data/blog_feeds.csv`
- `feeds.txt`（由步骤1结果自动生成）
- `output/YYYY-MM-DD.json`
- `output/YYYY-MM-DD.enriched.json`
- `post.xml`（仓库根目录，GitHub Pages 直接订阅）

说明：
- 如果未设置 `OPENAI_API_KEY`，加 `--skip-ai-if-no-key` 会自动跳过 AI 步骤并继续产出 RSS。
- 如果不需要步骤5复制，可加 `--skip-publish-copy`。

### 方案B：跑每日 JSON Feed 流程（步骤 2~4）

先准备 `feeds.txt`（每行一个 RSS URL，支持 `#` 注释），或从模板复制：

```bash
cp feeds.txt.example feeds.txt
```

执行：

```bash
source .venv/bin/activate
python scripts/run_daily_pipeline.py \
  --feeds-file feeds.txt \
  --provider none \
  --feed-title "Daily Tech Posts"
```

产物默认位置：
- `output/YYYY-MM-DD.json`（抓取结果）
- `output/YYYY-MM-DD.enriched.json`（AI 增强结果）
- `output/YYYY-MM-DD.feed.json`（JSON Feed）
- `output/YYYY-MM-DD.fetch.stats.json`
- `output/YYYY-MM-DD.ai.stats.json`
- `output/YYYY-MM-DD.pipeline.stats.json`
- `logs/YYYY-MM-DD.*.log`

`--provider` 说明：
- `openai`：调用 OpenAI 生成摘要和标签（需要 `OPENAI_API_KEY`）
- `hf-local`：本地 transformers summarization pipeline
- `none`：不调用 AI，输出空摘要/标签或保留回退行为

## 脚本清单

- `scripts/fetch_dailybit_feeds.py`：从来源页面抓 RSS 列表，输出 JSON/CSV
- `scripts/fetch_rss_today.py`：抓取 feeds 并按日期过滤文章，支持 SQLite 去重
- `scripts/step3_ai_extract.py`：对文章生成摘要/标签，输出 stats（含 token 与成本估算）
- `scripts/generate_json_feed.py`：从增强 JSON 生成 JSON Feed 1.1
- `scripts/generate_netnewswire_feed.py`：从增强 JSON 生成 RSS XML，并可导出 OPML（NetNewsWire Import）
- `scripts/run_daily_pipeline.py`：串联抓取 + AI + JSON Feed
- `scripts/run_full_pipeline.py`：串联步骤1~5并准备发布 RSS
- `scripts/smoke_test_step5.py`：本地端到端 smoke test（临时本地 RSS 源）

## 输入与字段兼容

### `step3_ai_extract.py` 输入

支持 JSON / JSONL。文章内容字段优先读取：
- `content`
- `body`
- `text`
- `article`

输出会附加：
- `summary`
- `tags`
- 失败时可能附加 `ai_error`

### `generate_netnewswire_feed.py` 字段兼容

可混用字段名：
- 标题：`title/headline/name`
- 链接：`url/link/source_url/origin_url`
- 摘要：`summary/description/abstract/excerpt`
- 正文：`content/full_content/body/text`
- 时间：`published/published_at/pub_date/date/created_at/updated_at`
- 标签：`tags/tag_list/labels/keywords`

## 验收与功能检查

### 1) 本地 smoke test（推荐）

```bash
source .venv/bin/activate
python scripts/smoke_test_step5.py
```

该测试会自动：
- 创建临时 RSS 源
- 运行 `fetch_rss_today.py`
- 运行 `step3_ai_extract.py --provider none`
- 运行 `generate_json_feed.py`
- 断言产物条目数

成功时输出：`Smoke test passed: fetch -> AI extract -> feed generation`

### 2) 语法快速检查

```bash
source .venv/bin/activate
python -m compileall -q scripts
```

### 3) 自动化回归脚本

```bash
source .venv/bin/activate
python tests/test_regression.py
```

该脚本会自动验证：
- `run_daily_pipeline.py` 非空文章场景（应产出 1 条 feed item）
- `run_daily_pipeline.py` 空结果场景（前一天日期，应产出 0 条）
- `run_full_pipeline.py` 本地 1~5 全链路（含步骤5发布复制）

## 定时任务（cron 示例）

创建日志目录：

```bash
mkdir -p /Users/xuying/playground/daily-posts/logs
```

编辑 crontab：

```bash
crontab -e
```

示例：每天 08:00 运行每日流程（`provider=none`）

```cron
0 8 * * * /Users/xuying/playground/daily-posts/.venv/bin/python /Users/xuying/playground/daily-posts/scripts/run_daily_pipeline.py --feeds-file /Users/xuying/playground/daily-posts/feeds.txt --provider none --output-dir /Users/xuying/playground/daily-posts/output --log-dir /Users/xuying/playground/daily-posts/logs >> /Users/xuying/playground/daily-posts/logs/cron.log 2>&1
```

## GitHub Actions 定时发布（推荐）

工作流文件：
- `.github/workflows/update-post-rss.yml`

触发方式：
- 每天 `08:00`（Asia/Shanghai）自动执行（cron 为 `0 0 * * *` UTC）。
- 支持手动触发（`workflow_dispatch`）。

工作流会自动执行：
- 运行 `run_full_pipeline.py` 生成 `post.xml`
- 自动提交并推送 `post.xml` 到 `main`
- GitHub Pages 从仓库发布该文件

首次启用步骤：
1. 确认仓库已开启 **GitHub Pages**（`Settings -> Pages`，Source 设为 `Deploy from a branch`，分支 `main` / `/root`）。
2. 在 GitHub Actions 页面启用并手动运行一次 `Update post.xml RSS`。
3. 生成成功后访问：

```text
https://xyz5020.github.io/daily-posts/post.xml
```

如果你之前启用了本地 `launchd` 自动更新，可停用避免重复发布：

```bash
launchctl bootout gui/$(id -u)/com.xuying.daily-posts.update || true
```

## 发布到 GitHub Pages（RSS + OPML）

推荐使用 GitHub Actions 自动更新并发布 `post.xml`，无需手动 commit/push。

典型订阅地址：

```text
https://<YOUR_GH_USERNAME>.github.io/daily-posts/post.xml
```

NetNewsWire 使用建议：
- 直接订阅：在 NetNewsWire 添加上面的 RSS URL。
- 当前自动化推荐订阅地址：`https://xyz5020.github.io/daily-posts/post.xml`（iPhone 与 MacBook 共用同一地址）。

## 常见问题

- `Missing OPENAI_API_KEY environment variable.`
  - 使用 OpenAI provider 时必须设置 API Key；或改用 `--provider none`；或在 full pipeline 中使用 `--skip-ai-if-no-key`。

- 步骤1来源不可达
  - 使用 `run_full_pipeline.py --allow-fallback`，脚本会使用内置官方 feed 列表继续执行。

- 当天文章数为 0
  - 属于正常场景，流程仍会产出空列表的 `.json/.enriched.json/.feed.json` 或 0 条目的 RSS XML。

- 所有源都拉取失败（例如 DNS/网络异常）
  - 现在流程会直接失败并停止发布，以避免把 `post.xml` 覆盖成空 feed。
  - 优先检查 GitHub Actions 日志里的 `feed_failures`、`NameResolutionError`、`SSL` 错误。

- 去重需求
  - 在抓取时传 `--db-path data/history.db`，通过 SQLite 记录历史文章键，避免重复处理。

## 目录结构

```text
daily-posts/
├── data/
│   ├── blog_feeds.json
│   └── blog_feeds.csv
├── feeds.txt.example
├── requirements.txt
├── scripts/
│   ├── fetch_dailybit_feeds.py
│   ├── fetch_rss_today.py
│   ├── step3_ai_extract.py
│   ├── generate_json_feed.py
│   ├── generate_netnewswire_feed.py
│   ├── run_daily_pipeline.py
│   ├── run_full_pipeline.py
│   └── smoke_test_step5.py
└── README.md
```
