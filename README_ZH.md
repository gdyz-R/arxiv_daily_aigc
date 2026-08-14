# Daily AI Research Gazette

[English](README.md) | 中文

一个自动化 AI/ML 学术日报项目：从 arXiv、OpenAlex、Semantic Scholar 和官方会议页面收集论文，使用两套 OpenAI-compatible 模型完成候选粗筛、日级选文与中文精编，最终归档为静态 HTML/JSON 并通过 GitHub Pages 发布。

## 主要能力

- 主题与编辑视角平权轮转
- 主题、会议、信息源、日报篇数和输出路径统一由 `config.yaml` 控制。
- 可选 Private Gist 概念记忆；未配置或读取失败时自动使用空记忆模式。
- 图片下载后进行格式、大小和解码校验；。
- GitHub Actions 自动生成、提交产物并部署 Pages。

## Fork 后快速部署

1. Fork 仓库，并在 **Actions** 页面启用工作流。
2. 打开 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions**。
3. 打开 **Settings → Pages → Build and deployment → Source**，选择 **GitHub Actions**。
4. 在 **Settings → Secrets and variables → Actions** 中添加下方 Secrets 和 Variables。
5. 打开 **Actions → Daily AI Research Gazette → Run workflow** 手动运行首次日报。


## 模型配置

### 必需 Repository Secrets

| 名称 | 用途 |
| --- | --- |
| `COARSE_LLM_API_KEY` | 候选论文粗筛模型凭据。 |
| `EDITORIAL_LLM_API_KEY` | 日级选文、中文精编和可选图片解读模型凭据。 |

### 必需 Repository Variables

| 名称 | 用途 |
| --- | --- |
| `COARSE_LLM_BASE_URL` | 粗筛模型 API 根地址，不包含 `/chat/completions`。 |
| `COARSE_LLM_MODEL` | 粗筛模型标识。 |
| `EDITORIAL_LLM_BASE_URL` | 编辑模型 API 根地址，不包含 `/chat/completions`。 |
| `EDITORIAL_LLM_MODEL` | 编辑模型标识。 |


### 可选 Repository Variables

| 名称 | 默认值 | 用途 |
| --- | --- | --- |
| `COARSE_LLM_TOKEN_FIELD` | `max_tokens` | 粗筛请求中的输出 Token 上限字段。 |
| `EDITORIAL_LLM_TOKEN_FIELD` | `max_tokens` | 接口要求时可设为 `max_completion_tokens`。 |
| `COARSE_LLM_REASONING_FORMAT` | `none` | 可选 `none`、`flat`、`nested`。 |
| `EDITORIAL_LLM_REASONING_FORMAT` | `none` | 可选 `none`、`flat`、`nested`。 |
| `COARSE_LLM_REASONING_EFFORT` | 空 | 服务支持的思考强度，例如 `low`、`high`。 |
| `EDITORIAL_LLM_REASONING_EFFORT` | 空 | 服务支持的思考强度，例如 `high`、`max`。 |
| `OPENALEX_MAILTO` | 空 | OpenAlex polite pool 联系邮箱。 |

- `none`：不发送思考参数。
- `flat`：发送 `"reasoning_effort": "<value>"`。
- `nested`：发送 `"reasoning": {"effort": "<value>"}`。

### 可选 Repository Secrets

| 名称 | 用途 |
| --- | --- |
| `SEMANTIC_SCHOLAR_API_KEY` | 启用 Semantic Scholar 元数据增强。 |
| `GIST_ID` | 保存 `concept_ledger.json` 的 secret/unlisted Gist ID。 |
| `GIST_TOKEN` | 可读取和更新该 Gist 的 Token。 |

Gist 最小初始内容：

```json
{"schema_version": 1, "updated_at": null, "concepts": {}}
```

Gist 配置缺失、网络失败或内容损坏不会阻止日报发布。Secret Gist 只是“不公开列出”，并非端到端加密；不要保存密钥或敏感个人信息。

## 本地运行

创建虚拟环境：

```bash
python -m venv .venv
```

激活环境：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bat
:: Windows CMD
.venv\Scripts\activate.bat
```

```bash
# Linux/macOS
source .venv/bin/activate
```

安装依赖：

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，填写与 Actions 相同的模型配置。`.env` 已被 Git 忽略，并且不会覆盖终端中已经存在的环境变量。

## 本地生成与预览

```bash
python src/main.py
python src/main.py --date 2026-08-10 --force
python src/main.py --date 2026-08-10 --offline-render
```

开发时如需验证，可在本地运行 `python -m unittest discover -s tests -v`；定时发布工作流不会执行该测试。

在仓库根目录启动本地静态服务：

```bash
python -m http.server 8000
```

访问 <http://localhost:8000/>。不要直接使用 `file://` 打开首页，浏览器可能阻止读取 `reports.json`。

## 核心配置与产物

- `config.yaml`：主题、调度、信息源、编辑策略、记忆、渲染、超时、重试和 temperature。
- `templates/index.html`、`templates/styles.css`：日报结构与样式源码。
- `daily_json/YYYY-MM/`：日报 JSON。
- `daily_html/YYYY-MM/`：日报 HTML。
- `assets/figures/YYYY-MM/`：通过校验的论文图片。
- `reports.json`：按日期倒序排列的归档索引。

工作流每天 `00:00 UTC` 运行：安装依赖、生成日报、提交产物，并触发 Pages 部署。若编辑模型或历史论文源临时失败，系统会使用规则选文发布，并在日报元数据中标记 `prominence_policy_status: degraded` 及具体原因；这避免了单个上游服务异常中断整期日报。若希望严格拒绝任何缺少焦点领域历史知名论文的期刊，可将 `config.yaml` 的 `selection.prominence_failure_mode` 改为 `block`。

## 常见问题

- **提示缺少模型配置**：检查当前 Fork 中 Repository Secret/Variable 的名称是否与上表完全一致。
- **模型返回 `HTTPError(status=401/404/429/5xx)`**：检查 Key、模型名、Base URL，并确认没有重复添加 `/chat/completions`；`429` 请等待限流窗口后重试，`5xx` 通常是上游服务暂时不可用。
- **模型返回格式错误**：确认服务支持 JSON mode、配置的 Token 字段和思考参数格式。
- **arXiv 429**：保持 RSS fallback 开启，减少候选规模，或稍后重试。
- **Action 无法 push**：开启工作流读写权限，并检查 Branch protection / Ruleset。
- **Pages 404**：确认 Pages Source 为 GitHub Actions，且生成产物已经提交到 `main`。

## 二次开发

系统架构、数据契约、扩展点、知名论文约束、渲染机制和测试规范见 [技术实现与二次开发指南](docs/secondary-development.md)。