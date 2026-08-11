# Daily AI Research Gazette

[中文说明](README_ZH.md) | English

An automated AI/ML research brief that collects papers from arXiv, OpenAlex, Semantic Scholar, and official conference pages, then uses two OpenAI-compatible models for candidate triage and daily editorial generation. Reports are archived as static HTML/JSON and published through GitHub Pages.

## Highlights

- Fair topic and editorial-angle rotation without using paper volume as scheduler weight.
- Configurable topics, venues, source adapters, edition size, and output paths in `config.yaml`.
- Optional private Gist concept memory with safe empty-memory fallback.
- Validated figure downloads and responsive no-placeholder layouts.
- GitHub Actions generation, privacy scanning, artifact commits, and Pages deployment.

## Deploy a fork

1. Fork the repository and enable workflows in the **Actions** tab.
2. Open **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**.
3. Open **Settings → Pages → Build and deployment → Source** and select **GitHub Actions**.
4. Add the Repository Secrets and Variables below under **Settings → Secrets and variables → Actions**.
5. Run **Actions → Daily AI Research Gazette → Run workflow**.


## Model configuration

### Required Repository Secrets

| Name | Purpose |
| --- | --- |
| `COARSE_LLM_API_KEY` | Credential for candidate triage. |
| `EDITORIAL_LLM_API_KEY` | Credential for final selection, writing, and optional figure explanation. |

### Required Repository Variables

| Name | Purpose |
| --- | --- |
| `COARSE_LLM_BASE_URL` | Coarse-model API root, without `/chat/completions`. |
| `COARSE_LLM_MODEL` | Coarse-model identifier. |
| `EDITORIAL_LLM_BASE_URL` | Editorial-model API root, without `/chat/completions`. |
| `EDITORIAL_LLM_MODEL` | Editorial-model identifier. |


### Optional Repository Variables

| Name | Default | Purpose |
| --- | --- | --- |
| `COARSE_LLM_TOKEN_FIELD` | `max_tokens` | Request field used for the output-token limit. |
| `EDITORIAL_LLM_TOKEN_FIELD` | `max_tokens` | Use `max_completion_tokens` when required by the endpoint. |
| `COARSE_LLM_REASONING_FORMAT` | `none` | `none`, `flat`, or `nested`. |
| `EDITORIAL_LLM_REASONING_FORMAT` | `none` | `none`, `flat`, or `nested`. |
| `COARSE_LLM_REASONING_EFFORT` | empty | Provider-supported effort value such as `low` or `high`. |
| `EDITORIAL_LLM_REASONING_EFFORT` | empty | Provider-supported effort value such as `high` or `max`. |
| `OPENALEX_MAILTO` | empty | Optional OpenAlex polite-pool contact email. |

- `none`: send no reasoning field.
- `flat`: send `"reasoning_effort": "<value>"`.
- `nested`: send `"reasoning": {"effort": "<value>"}`.
- If the effort value is empty, no reasoning field is sent.

### Optional Repository Secrets

| Name | Purpose |
| --- | --- |
| `SEMANTIC_SCHOLAR_API_KEY` | Enables Semantic Scholar metadata enrichment. |
| `GIST_ID` | Secret/unlisted Gist containing `concept_ledger.json`. |
| `GIST_TOKEN` | Token allowed to read and update that Gist. |

Minimal Gist content:

```json
{"schema_version": 1, "updated_at": null, "concepts": {}}
```

Gist failures do not block publication. A secret Gist is unlisted, not end-to-end encrypted; do not store credentials or sensitive personal data in it.

## Local setup

```bash
python -m venv .venv
```

Activate the environment:

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

Install and configure:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then fill in the same model settings used by Actions. The local `.env` is ignored by Git and never overrides existing process variables.

## Run and validate

```bash
python -m compileall src
python -m unittest discover -s tests -v

python src/main.py
python src/main.py --date 2026-08-10 --force
python src/main.py --date 2026-08-10 --offline-render
python src/privacy.py daily_json daily_html reports.json assets/report.css assets/figures
```

Preview the static site from the repository root:

```bash
python -m http.server 8000
```

Open <http://localhost:8000/>. Do not use `file://`, because browsers may block `reports.json` requests.

## Main configuration and outputs

- `config.yaml`: topics, scheduling, sources, editorial policy, memory, rendering, timeouts, retries, and temperatures.
- `templates/index.html` and `templates/styles.css`: report layout and style source.
- `daily_json/YYYY-MM/`: generated report data.
- `daily_html/YYYY-MM/`: generated report pages.
- `assets/figures/YYYY-MM/`: validated cached figures.
- `reports.json`: reverse-chronological archive index.

The workflow runs daily at `00:00 UTC`, executes offline tests, validates required model configuration, generates the report, scans public outputs, commits report artifacts, and triggers the Pages deployment workflow.

## Common issues

- **Missing model configuration:** verify the exact Repository Secret/Variable names above in the current fork.
- **401/404:** verify the API key, model identifier, base URL, and that `/chat/completions` is not duplicated.
- **Invalid model response:** verify JSON mode, token-field name, and reasoning format support.
- **arXiv 429:** keep RSS fallback enabled, reduce candidate limits, or rerun later.
- **Push rejected:** enable workflow read/write permission and check branch protection rules.
- **Pages 404:** select GitHub Actions as the Pages source and confirm generated artifacts reached `main`.