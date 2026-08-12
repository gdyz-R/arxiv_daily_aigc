# Daily AI Research Gazette 技术实现与二次开发指南

本文面向维护者和二次开发者，说明系统边界、数据流、公开数据契约、扩展点与测试要求。部署和日常使用请先阅读根目录的 `README.md` 或 `README_ZH.md`。

## 1. 运行时与设计原则

- Python 3.11；依赖由 `requirements.txt` 管理。
- 抓取适配器相互独立，单一来源失败不应直接破坏其他来源。
- LLM 只负责语义判断和写作；数量、来源证据、隐私和发布资格由确定性代码校验。
- 单元测试必须离线运行，网络调用通过 fake session 或 mock 隔离。
- `daily_json/`、`daily_html/`、`assets/report.css`、`assets/figures/` 和 `reports.json` 是可发布产物。
- 私有概念记忆只存在于配置的 Gist 中，`memory_payload` 不得进入公开 JSON/HTML。

## 2. 端到端数据流

入口为 `src/main.py:generate_daily_report()`：

1. **加载配置**：`src/config.py` 读取 `.env` 和 `config.yaml`，执行结构与路径校验。
2. **调度主题/视角**：`src/scheduler.py` 根据历史日报计算 topic cooldown、starvation guard 和编辑视角。
3. **读取私有记忆**：`src/memory.py` 从可选 GitHub Gist 读取概念账本；失败时使用空记忆。
4. **抓取与合并**：`src/crawl.py` 从 arXiv、RSS fallback、近期 OpenAlex、历史 OpenAlex 和 NeurIPS 官方页面收集候选，按 arXiv ID 或标准化标题去重。
5. **元数据增强**：可选 Semantic Scholar 提供引用量和 venue；`src/prominence.py` 生成知名度证据。
6. **有界粗筛**：`prefilter_coarse_candidates()` 在调用粗筛模型前限制候选数，同时保留历史知名焦点论文。
7. **语义分类**：`coarse_classify_papers()` 产生 topic、创新度、影响力、清晰度和受控贡献标签；模型不可用时走规则降级。
8. **shortlist 与日级编辑**：`rank_candidate_shortlist()` 产生候选短名单，`generate_memory_aware_edition()` 允许编辑模型在配置区间内决定篇数和版面层级。
9. **确定性发布校验**：模型输出必须满足 hero/major、篇幅和知名论文约束；否则回退到规则选文。`src/main.py` 在写文件前再次 fail closed 校验。
10. **图片与图解**：只对最终 major 论文抓取、校验和缓存 Figure 1，再按模型可用性生成图解。
11. **隐私、渲染与归档**：`src/privacy.py` 清洗公开数据，`src/render.py` 通过 Jinja 模板渲染 HTML，`src/archive.py` 生成月度路径，最后刷新 `reports.json`。

## 3. 主要模块职责

| 模块 | 职责 |
| --- | --- |
| `src/config.py` | 配置加载、环境变量解析、跨字段校验、目录逃逸保护 |
| `src/scheduler.py` | 公平主题轮换、视角轮换、arXiv 检索式生成 |
| `src/crawl.py` | 网络适配器、元数据规范化、去重合并、图片抓取 |
| `src/prominence.py` | 顶会/引用知名判定、历史锚点、日报级配额校验 |
| `src/filter.py` | 粗筛、评分、shortlist、规则选文、LLM 输出解析和内容生成 |
| `src/prompts.py` | 日级编辑 JSON prompt 与输出契约 |
| `src/memory.py` | 私有概念账本读取、校验、合并和写回 |
| `src/privacy.py` | 公开字段清洗、密钥与本地路径扫描 |
| `src/render.py` | schema 兼容、视图模型准备、Jinja 渲染、样式发布 |
| `src/archive.py` | 安全文件名、月度 JSON/HTML 路径、相对资源 URL |
| `src/main.py` | 全流程编排、拒绝空日报、拒绝不合规日报、落盘和索引更新 |

## 4. 配置结构

### `project`

- 报纸名称、副标题、时区、规则降级选文规模和 major 上限。
- `edition_size/focus_count/cross_topic_count` 主要服务规则回退；编辑模型正常工作时最终篇数由 `editorial_policy` 控制。

### `scheduler` 与 `topics`

- `scheduler.topic_pool` 决定可调度主题。
- `topics.<id>.categories/keywords` 同时影响 arXiv、RSS、OpenAlex 和规则 topic 评分。
- `concepts` 用于从私有记忆中提取当天相关概念。

### `selection`

- `top_venues/venue_aliases`：venue 规范化、排序和 major 判断。
- `well_known_venues`：只有这里的会议可作为“顶会入选”的知名证据；引用量证据不受此列表限制。
- `high_citation_threshold`：知名论文引用阈值。
- `recent_days`：历史锚点边界；知名论文早于此边界才算 `historical_anchor`。
- `min_well_known_papers/max_well_known_papers`：成功发布日报必须包含的焦点领域知名论文范围，要求 `1 <= min <= max <= 3`。
- `coarse_candidate_limit/coarse_focus_minimum`：控制昂贵粗筛调用规模。

### `sources`

- `arxiv`：近期主来源；API 遇到 429/连接错误可切换 RSS。
- `openalex`：近期顶会论文发现。
- `openalex_historical`：只为当天焦点领域查询早于 `recent_days` 的论文，分别用引用过滤和 OpenAlex source display-name 过滤检索候选后合并；通过 `max_pages` 控制游标分页成本。
- `semantic_scholar`：可选引用量和 venue 增强；主流程仅在配置 API Key 时启用。
- `neurips`：会议窗口附近从官方页面获取 venue 和 Oral/Spotlight/Poster 证据。
- `arxiv_html`：最终 major 论文图片下载、安全校验和缓存。

### `editorial_policy`

- 控制编辑模型可选篇数、候选短名单、总字数和输出 token。
- `min_selected_papers/max_selected_papers` 不应与知名论文数量混淆。

## 5. 论文数据契约

抓取阶段的核心字段：

```json
{
  "paper_id": "arxiv:2401.00001",
  "source": "arxiv",
  "sources": ["arxiv", "openalex_historical"],
  "title": "...",
  "summary": "...",
  "published_date": "2024-01-01T00:00:00+00:00",
  "candidate_topics": ["cot_agentic_ai"],
  "citation_count": 123,
  "venue": "International Conference on Learning Representations",
  "venue_tags": ["ICLR"]
}
```

知名度派生字段由 `annotate_well_known_paper()` 生成，模型不得自行修改其含义：

```json
{
  "well_known": true,
  "well_known_reasons": ["top_venue:ICLR", "high_citation:123"],
  "historical_anchor": true
}
```

- `well_known=true` 当且仅当命中 `well_known_venues` 或引用量达到阈值。
- venue 名称含 workshop、tutorial、challenge、companion 等附属活动标记时不作为顶会证据。
- `historical_anchor=true` 还要求论文年龄超过 `recent_days`。
- 粗筛后，日报配额只统计 `primary_topic` 属于当天焦点领域的论文；粗筛前仅用 `candidate_topics` 为历史候选预留位置。

粗筛后会增加 `primary_topic/topic_scores/novelty_score/potential_impact_score/clarity_score`。最终选文会增加 `content_tier/is_hero/newspaper_title/dek/...` 等编辑字段。

公开报告采用 schema v2：

```json
{
  "schema_version": 2,
  "report": {
    "date": "YYYY-MM-DD",
    "focus_topic": "topic_id",
    "selected_count": 6,
    "well_known_paper_count": 2,
    "historical_anchor_count": 1
  },
  "papers": []
}
```

`src/render.py:normalize_report_payload()` 仍支持旧版论文数组，但新功能只保证 schema v2 的完整元数据。

## 6. 知名论文约束闭环

规则为：每份成功发布的日报包含 **1–3 篇当天对应领域的知名论文**，其中至少一篇为历史锚点。

约束存在于四层：

1. 历史 OpenAlex 源提供旧论文候选。
2. prefilter/shortlist 为历史知名焦点论文预留位置。
3. 编辑 prompt 明示规则，输出解析器拒绝违规模型结果并回退。
4. `src/main.py` 写公开文件前再次调用 `prominence_policy_errors()`；仍不满足则拒绝发布。

不要删除最后一层校验。提示词不是数据完整性边界，规则回退也可能因上游来源空缺而无法满足要求。

## 7. 渲染和自适应版面

- `templates/index.html` 定义日报语义结构。
- `templates/styles.css` 是样式唯一源码；每次渲染会复制到 `assets/report.css`。
- Hero、Major 和 Brief 由 `src/render.py:_prepare_view()` 从 `content_tier/is_hero` 分区。
- Brief 桌面端使用 6 个 CSS 轨道：通常每卡占 2 轨；最后剩 1 张时占满行，剩 2 张时各占半行。中屏奇数尾卡占满行，移动端单列。因此不要通过补空卡或限制文章数量处理排版。

修改模板后，已有日报不会自动变化。可执行：

```bash
python src/main.py --date 2026-08-12 --offline-render
```

这只读取既有 JSON，不调用抓取或 LLM API。

## 8. 常见二次开发

### 新增主题

1. 在 `topics` 中添加唯一 ID、双语名称、分类、关键词、贡献标签和 concepts。
2. 将 ID 加入 `scheduler.topic_pool`；如有旧调用需求，可同步 `topic_rotation`。
3. 为 scheduler、query 和 filter 添加测试。

### 新增顶会

1. 添加到 `selection.top_venues` 和 `venue_aliases`。
2. 如果该 venue 可作为“顶会入选”证据，再添加到 `well_known_venues`。
3. 添加 venue 精确匹配测试，防止短字符串误命中。

### 新增来源

1. 在 `src/crawl.py` 建立独立 adapter，接受 config 和可注入 session。
2. 输出现有标准论文字段，不在 adapter 内做最终选文。
3. 在 `crawl_papers_with_diagnostics()` 中接入，并提供公开安全的 status/result_count。
4. 更新 `merge_papers()` 以保留来源独有证据。
5. 使用 fake response 编写完全离线测试。

### 修改选文规则

- 知名度定义放在 `src/prominence.py`。
- 排名和候选保留放在 `src/filter.py`。
- 最终不可绕过的发布资格放在 `src/main.py`。
- 同步更新 prompt，但不要只改 prompt。

### 修改版面

- 改 `templates/index.html` 和 `templates/styles.css`。
- 添加 `tests/test_render.py` 回归测试。
- 离线重渲染需要立即生效的历史日报。

## 9. 降级与失败策略

| 场景 | 行为 |
| --- | --- |
| arXiv API 429/连接错误 | 尝试 RSS fallback |
| 单个来源空或失败 | 记录 diagnostics，继续其他来源 |
| Semantic Scholar 未配置 | 使用来源已有 citation/venue，不阻塞 |
| 粗筛模型不可用/格式错误 | 规则 topic 与质量评分 |
| 编辑模型不可用/格式错误/违反约束 | 规则选文 + 逐篇降级精编 |
| 历史知名候选仍不足 | 拒绝发布，不生成新的不合规 JSON/HTML |
| 图片不存在或校验失败 | 无占位符布局，正文继续发布 |
| Gist 读取/写入失败 | 空记忆或跳过写入，公开日报继续 |
| 无候选论文 | 默认拒绝空日报；仅显式 `--allow-empty` 可覆盖 |

## 10. 测试与质量门禁

本地和 CI 使用：

```bash
python -m compileall src
python -m unittest discover -s tests -v
python src/privacy.py daily_json daily_html reports.json assets/report.css assets/figures
```

测试目录约定：

- `test_config.py`：配置不变量和环境变量。
- `test_crawl.py`：来源解析、fallback、图片与历史论文发现。
- `test_filter.py`：评分、选文、知名配额、LLM 违规回退。
- `test_prompts.py`：prompt 数据和输出契约。
- `test_render.py`：HTML、资源相对路径和响应式版面。
- `test_main.py`：文件写入边界、空日报/不合规日报拒绝、隐私记忆隔离。
- `test_archive_privacy.py`：归档命名、workflow 配置、公开扫描。

任何网络功能都必须允许注入 session/client，测试不得依赖真实网络、真实 Key 或当天数据。

## 11. GitHub Actions 与公开产物

`.github/workflows/daily_arxiv.yml` 每天执行：安装依赖 → 离线测试 → 校验模型变量 → 生成日报 → 隐私扫描 → 提交产物。

需要提交的生成产物：

- `daily_json/YYYY-MM/*.json`
- `daily_html/YYYY-MM/*.html`
- `assets/report.css`
- `assets/figures/**/*.png`（如存在）
- `reports.json`

根目录 `index.html` 只负责读取 `reports.json` 并将最新日报载入 iframe；`list.html` 是独立归档页面。

## 12. 修改检查清单

- [ ] 配置有校验，路径不会逃逸仓库。
- [ ] 新来源不会覆盖更强的 citation/venue/官方证据。
- [ ] 知名度派生字段由确定性代码生成。
- [ ] LLM 失败和无 Key 均有测试覆盖。
- [ ] 不合规日报在写文件前失败。
- [ ] 公开 JSON 不含 memory payload、密钥或本地绝对路径。
- [ ] 模板支持零图、变篇数和非整行快讯。
- [ ] 单元测试完全离线通过。
- [ ] 如修改模板，已重渲染需要立即修复的既有日报。