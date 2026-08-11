# Daily AI Research Gazette

面向个人研究者的每日 AI/ML 论文报纸生成器。项目从 arXiv、OpenAlex、Semantic Scholar 和 NeurIPS 官方页面获取论文，按可配置主题筛选与精编，生成静态 HTML，并通过 GitHub Actions 自动归档和发布到 GitHub Pages。

## 核心行为

- **无图自动补位**：只有成功下载并解码验证的图片才会写入 HTML。无图论文直接使用单栏排版，不生成图片占位、状态说明或空白区域；浏览器加载失败时也会自动移除媒体栏。
- **月度归档**：新生成文件使用：
  - `daily_html/YYYY-MM/YYYY-MM-DD-日报标题.html`
  - `daily_json/YYYY-MM/YYYY-MM-DD-日报标题.json`
  - `assets/figures/YYYY-MM/YYYY-MM-DD-日报标题/<paper-id>-figure1.png`
- **旧归档兼容**：既有 `daily_html/YYYY_MM_DD.html` 不迁移、不删除；`reports.json` 同时索引旧路径和新的月度路径。
- **配置优先**：主题、关键词、类别、日报篇数、主/跨主题比例、重点论文数量、会议名单、回溯天数、数据源开关和模型均在 `config.yaml` 中修改。
- **样式外置**：页面样式维护在 `templates/styles.css`；渲染时同步为公开的 `assets/report.css`。
- **云端运行**：API Key 只从 GitHub Repository Secrets 注入；Action 在提交前运行测试和隐私扫描。

## 信息源与顶会覆盖

默认信息源为 arXiv、OpenAlex、Semantic Scholar 和 NeurIPS 官方页面。默认会议/期刊标签包括 NeurIPS、ICLR、ICML、AAAI、IJCAI、ACL、EMNLP、NAACL、COLM、CVPR、ICCV、ECCV、KDD、WWW、SIGIR、AISTATS、UAI、CoRL、RSS、JMLR 和 TMLR。

会议匹配由 `selection.top_venues` 与 `selection.venue_aliases` 控制，新增会议无需修改 Python。OpenAlex 元数据可能存在延迟或 venue 命名差异，因此系统会按配置别名本地核验；OpenAlex 请求失败时只会降级，不阻塞 arXiv 主流程。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python src/main.py --date 2026-08-10 --force
```

Linux/macOS 使用 `source .venv/bin/activate`。本地 `.env` 只用于开发，并被 `.gitignore` 忽略。不要把真实密钥写入 `config.yaml`、README、JSON 或工作流文件。

## 配置接口

| 目标 | 配置位置 |
| --- | --- |
| 日报总篇数 | `project.edition_size` |
| 主主题/跨主题篇数 | `project.focus_count` / `project.cross_topic_count` |
| 重点论文上限 | `project.max_major_features` |
| 归档标题 | `project.archive_title` |
| 每周主题轮换 | `topic_rotation` |
| 搜索重点与关键词 | `topics.*.categories` / `topics.*.keywords` |
| 顶会名单与别名 | `selection.top_venues` / `selection.venue_aliases` |
| 数据源开关与回溯天数 | `sources.*` |
| 模型、API 地址、超时和重试 | `llm.*` |
| 模板、CSS 和输出目录 | `render.*` |

`focus_count + cross_topic_count` 必须等于 `edition_size`。新增主题后，需要在 `topic_rotation` 的七个工作日中引用有效主题键。

## 本地运行与验证

```powershell
python src/main.py
python src/main.py --date 2026-08-10 --force
python src/main.py --date 2026-08-10 --offline-render
python src/render.py --input tests/fixtures/report_v2.json --output daily_html/preview.html

python -m compileall src
python -m unittest discover -s tests -v
python src/privacy.py daily_json daily_html reports.json assets/report.css
```

程序使用 `project.timezone` 计算默认日期。请通过 HTTP 服务或 GitHub Pages 查看站点；直接打开 `file://index.html` 时，浏览器可能阻止 `fetch('reports.json')`。

## GitHub Secrets 与 Actions

在 GitHub 仓库进入 **Settings → Secrets and variables → Actions**。

### Repository Secrets

- `DEEPSEEK_API_KEY`：必需，粗筛模型。
- `DASUAPI_API_KEY`：必需，精编/图解模型。
- `SEMANTIC_SCHOLAR_API_KEY`：可选；缺失时使用匿名接口，限流更严格。

### Repository Variable

- `OPENALEX_MAILTO`：可选、非密钥，用于 OpenAlex polite pool 联系邮箱。

`.github/workflows/daily_arxiv.yml` 每天 `04:00 UTC` 运行，也支持手动输入 `report_date`。工作流会执行离线测试、检查必需 Secrets、生成日报、扫描公开输出，并且只提交 `daily_json/`、`daily_html/`、`assets/figures/`、`assets/report.css` 和 `reports.json`。

Pages 工作流只发布 `index.html`、`list.html`、`reports.json`、`daily_html/`、`daily_json/` 和 `assets/`，不会把源码、工作流、README 或 `.env.example` 打包为站点文件。

## 图片安全策略

- 只请求最终入选的重点论文图片，减少网络请求和仓库体积。
- 限制下载字节数与像素数。
- 只接受 PNG/JPEG/WebP；SVG、GIF 和伪造 `image/*` 内容会被拒绝。
- 使用 Pillow 完整解码，再统一重编码为 PNG 并原子写入归档目录。
- 缓存不存在或验证失败时清空 `figure_url`，HTML 不生成媒体栏。

## 公开数据与隐私边界

日报 JSON/HTML 会公开论文标题、摘要、作者、venue、链接、筛选分数、模型编辑结果和已缓存论文图片。以下内容不会进入公开输出：API Key、Authorization Header、Token、`.env` 内容、本机绝对路径、项目 `_meta`、原始请求异常和 GitHub Actions 环境配置。

如果研究主题本身也属于敏感信息，请使用私有仓库，并不要启用公开 GitHub Pages。

## 主要目录

```text
src/archive.py       # 月度归档路径与安全文件名
src/config.py        # YAML/环境变量加载与校验
src/crawl.py         # arXiv/OpenAlex/NeurIPS/图片抓取
src/filter.py        # 分类、选文和精编
src/main.py          # 主流水线
src/privacy.py       # 输出清理与隐私扫描
src/render.py        # HTML 渲染与资源路径计算
templates/index.html # Jinja 页面结构
templates/styles.css # 唯一样式编辑入口
tests/               # 离线单元测试
```
