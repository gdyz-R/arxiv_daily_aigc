# Daily AI Research Gazette

A memory-aware personal AI/ML research intelligence brief built from arXiv, OpenAlex, Semantic Scholar, and verified NeurIPS pages. It uses volume-independent cooldown scheduling, rotating editorial angles, an optional private Gist concept ledger, and responsive static monthly archives.

## Key behavior

- Figures are included only after successful download and image decoding. Papers without a figure use a full-width layout with no placeholder or empty media area.
- New archives use `daily_html/YYYY-MM/YYYY-MM-DD-edition-title.html`, matching JSON paths, and monthly figure directories. Existing legacy archives remain indexed.
- Search topics, keywords, edition size, topic allocation, venue aliases, source switches, models, and output paths are edited in `config.yaml`.
- Layout CSS is maintained in `templates/styles.css` and published as `assets/report.css`.
- Topic selection uses equal base weights, days-unselected cooldown recovery, and starvation protection; paper volume never enters the scheduler.
- The daily model can select 3–7 papers according to quality and applies a standardized total volume budget.
- Mastered concepts bypass introductory teaching, while first-contact concepts receive only concise background.

## Sources and venues

The default sources are arXiv, OpenAlex, Semantic Scholar, and official NeurIPS pages. The configured venue set covers NeurIPS, ICLR, ICML, AAAI, IJCAI, ACL, EMNLP, NAACL, COLM, CVPR, ICCV, ECCV, KDD, WWW, SIGIR, AISTATS, UAI, CoRL, RSS, JMLR, and TMLR. Extend coverage through `selection.top_venues` and `selection.venue_aliases` without changing Python.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python src/main.py --date 2026-08-10 --force
```

The local `.env` is ignored by Git. Never store real keys in YAML, JSON, documentation, or workflow files.

## Configuration interfaces

- Edition size and allocation: `project.edition_size`, `focus_count`, `cross_topic_count`, `max_major_features`
- Fair topic/angle scheduling: `scheduler.*`
- Search focus and memory concepts: `topics.*.categories/keywords/concepts`
- Variable edition volume: `editorial_policy.*`
- Private Gist storage: `memory.*`
- Conference scope: `selection.top_venues` and `selection.venue_aliases`
- Source settings: `sources.arxiv`, `sources.openalex`, `sources.semantic_scholar`, `sources.neurips`
- Models: `llm.coarse` and `llm.editorial`
- Template, stylesheet, and archive directories: `render.*`

## GitHub Actions

Create these **Repository Secrets** under **Settings → Secrets and variables → Actions**:

- `DEEPSEEK_API_KEY` — required
- `DASUAPI_API_KEY` — required
- `SEMANTIC_SCHOLAR_API_KEY` — optional
- `GIST_ID` — optional secret/unlisted Gist containing `concept_ledger.json`
- `GIST_TOKEN` — optional token that can read/update that Gist

Optionally add the non-secret repository variable `OPENALEX_MAILTO`.

The daily workflow runs tests, verifies required Secrets, generates the report, scans public outputs for injected secret values and local paths, and commits only report artifacts. The Pages workflow assembles a restricted `_site` containing only entry pages, indexes, reports, JSON, images, and CSS.

Gist configuration is deliberately optional. Missing credentials, network errors, invalid JSON, or write failures switch the pipeline to empty-memory mode without failing report publication.

Initialize `concept_ledger.json` with:

```json
{"schema_version": 1, "updated_at": null, "concepts": {}}
```

Security note: a GitHub secret Gist is unlisted, not end-to-end encrypted. This release stores readable JSON behind the Gist access controls. Storage and encoding are separated by `MemoryCodec`; unsupported future codec settings fail closed rather than silently writing plaintext.

## Validation

```powershell
python -m compileall src
python -m unittest discover -s tests -v
python src/privacy.py daily_json daily_html reports.json assets/report.css
```

Images are byte- and pixel-limited, decoded with Pillow, restricted to PNG/JPEG/WebP, re-encoded to PNG, and written atomically. Invalid images never create an HTML media slot.

Public reports intentionally contain paper metadata and editorial results. API keys, authorization headers, `.env`, local absolute paths, internal `_meta`, and raw request exceptions are removed or rejected before publication. If configured research interests are sensitive, use a private repository and do not enable public GitHub Pages.

The full ledger, prompt memory context, and model `memory_payload` remain private runtime data and are never persisted in public report JSON/HTML. Only non-sensitive memory status strings and update counts may appear in report metadata.
