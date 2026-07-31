# User Guide

This guide is for users running from source, installing from release packages, and integrating clients. It aims to stay consistent with `pyproject.toml`, `requirements.txt`, `run_default_uv.cmd`, and `run_default_pip.cmd`. The project is currently in the Alpha tightening stage; the Runtime API is ready for integration, but compatibility and documentation may continue to be tightened before a stable release.

## 1. Environment Requirements

- Operating system: Windows / Linux / macOS are all supported; Windows users can use the `.cmd` scripts in the repository.
- Python runtime: the project requires Python 3.14 or higher; Python 3.14 is officially available and is no longer treated as a prerelease version.
  - With uv: usually you only need to install uv first; uv will automatically select or download a suitable Python runtime according to `requires-python = ">=3.14"` in `pyproject.toml`.
  - With pip: you need to install Python 3.14+ yourself, then run pip with this Python.
- Package manager: `uv` is recommended; pip is also supported.
- LLM Provider: prepare at least one model service.
  - Ollama: runs locally, recommended by default, free but requires installing Ollama and pulling a model first.
  - OpenAI / OpenRouter / DeepSeek / OpenAI Responses: require the corresponding API key; underlying dependency is the OpenAI SDK.
  - Claude: requires Anthropic API key and Anthropic SDK.
  - Gemini: requires Google API key and Google GenAI SDK.

If using uv, check that uv is available and let it prepare Python according to project requirements:

```bash
uv --version
uv python install 3.14
```

If using pip, check the current Python version:

```bash
python --version
```

If there are multiple Python versions on the system, on Windows you can also try:

```bat
py -3.14 --version
```

## 2. Get the Project

```bash
git clone https://github.com/Patchouli-CN/GensokyoAI.git
cd GensokyoAI
```

If you downloaded a zip archive, extract it and run the following commands from the project root directory.

## 3. Installation

### 3.1 Using uv (Recommended)

uv will automatically create a virtual environment and select a Python that meets the version requirements based on `pyproject.toml`. Install the default local Ollama dependency:

```bash
uv sync --extra ollama
```

Or install a specific provider extra:

```bash
uv sync --extra openai
uv sync --extra openrouter
uv sync --extra deepseek
uv sync --extra openai_responses
uv sync --extra claude
uv sync --extra gemini
uv sync --extra all
```

Common extra mapping:

| extra | Installs | Provider |
|-------|----------|----------|
| `ollama` / `default` | `ollama` | `ollama` |
| `openai` | `openai>=1.0.0` | `openai` |
| `openrouter` | `openai>=1.0.0` | `openrouter` |
| `deepseek` | `openai>=1.0.0` | `deepseek` |
| `openai_responses` | `openai>=1.0.0` | `openai_responses` |
| `claude` | `anthropic>=0.20.0` | `claude` |
| `gemini` | `google-genai>=1.0.0` | `gemini` |
| `all` | Ollama / OpenAI / Anthropic / Gemini SDKs | all built-in providers |

### 3.2 Using pip

Install only the minimum runtime dependencies:

```bash
python -m pip install -r requirements.txt
```

It is recommended to install the corresponding provider extra in editable package mode, which also registers command-line scripts. `requirements.txt` only contains minimum runtime dependencies and will not force installation of any LLM Provider SDK; if you want to use the default local Ollama startup method, install the `default` or `ollama` extra:

```bash
python -m pip install -e .[default]
python -m pip install -e .[ollama]
python -m pip install -e .[openai]
python -m pip install -e .[openrouter]
python -m pip install -e .[deepseek]
python -m pip install -e .[openai_responses]
python -m pip install -e .[claude]
python -m pip install -e .[gemini]
python -m pip install -e .[all]
```

If you only want to add a provider SDK on demand, you can also install manually:

```bash
python -m pip install ollama
python -m pip install openai
python -m pip install anthropic
python -m pip install google-genai
```

### 3.3 Windows Quick Start Scripts

Two scripts are provided in the project root:

```bat
run_default_uv.cmd
run_default_pip.cmd
```

- `run_default_uv.cmd` runs `uv run --extra ollama -m GensokyoAI.cli.main --character "characters\zh_cn\KirisameMarisa.yaml" --new-session`.
- `run_default_pip.cmd` runs `python -m GensokyoAI.cli.main --character "characters\zh_cn\KirisameMarisa.yaml" --new-session`.

Before first use of `run_default_uv.cmd`, please install uv; usually no manual Python 3.14 installation is needed because uv can prepare the runtime automatically, and it will install the default Ollama SDK via `--extra ollama`. Before first use of `run_default_pip.cmd`, please manually install Python 3.14+, then install the dependencies needed for default startup via `python -m pip install -e .[default]` or `python -m pip install -e .[ollama]`; running only `python -m pip install -r requirements.txt` will not install the Ollama SDK.

## 4. Configure the Main Chat Model

On first launch, the local config `config/local.yaml` is generated automatically from the template `tmp/template-conf.yaml`. Edit only `config/local.yaml` for daily use — never the release templates under tmp/; you can also point `--config <path>` at another file explicitly.

### 4.1 Ollama (Default Local Provider)

First install and start Ollama, then pull a model:

```bash
ollama pull qwen3.5:9b
```

Example configuration:

```yaml
config_schema_version: 1
model:
  provider: "ollama"
  name: "qwen3.5:9b"
  base_url: null
```

Ollama usually does not require `api_key` or `api_path`. If `api_path` is configured, the current configuration diagnostics will report an error because the Ollama provider does not support custom API paths.

### 4.2 OpenAI Chat Completions

```yaml
model:
  provider: "openai"
  name: "gpt-4o"
  api_key: "sk-..."
  base_url: null
```

For third-party OpenAI-compatible services, configure `base_url`:

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  api_key: "sk-..."
  base_url: "https://your-api.example.com/v1"
```

### 4.3 OpenRouter

```yaml
model:
  provider: "openrouter"
  name: "openai/gpt-4o"
  api_key: "sk-or-..."
  base_url: null
  extra_headers:
    HTTP-Referer: "https://your-site.example"
    X-Title: "GensokyoAI"
```

### 4.4 DeepSeek

We recommend using the standalone `deepseek` provider rather than configuring DeepSeek under the generic `openai` provider.

```yaml
model:
  provider: "deepseek"
  name: "deepseek-v4-pro"
  api_key: "sk-..."
  base_url: null
  thinking_enabled: true
  reasoning_effort: "high"
```

If `thinking_enabled: false` but `reasoning_effort` is still set, configuration diagnostics will warn that this field will be ignored.

### 4.5 OpenAI Responses API

```yaml
model:
  provider: "openai_responses"
  name: "gpt-5"
  api_key: "sk-..."
  web_search_enabled: true
  web_search_strategy: "explicit"
  web_search_context_size: "medium"
```

### 4.6 Claude

```yaml
model:
  provider: "claude"
  name: "claude-sonnet-4-20250514"
  api_key: "sk-ant-..."
```

### 4.7 Gemini

```yaml
model:
  provider: "gemini"
  name: "gemini-2.0-flash"
  api_key: "AIza..."
  web_search_enabled: true
  web_search_strategy: "explicit"
```

## 5. Configure Embedding Model (Optional)

The embedding model is configured independently from the main chat model. It is only needed when calling embeddings, connecting to vector retrieval, or external vector storage.

```yaml
embedding:
  provider: "openai"
  name: "text-embedding-3-small"
  api_key: "sk-..."
  base_url: null
  dimensions: 1024
  encoding_format: "float"
```

Note: Claude does not provide its own embeddings. If the main chat model uses Claude, configure embedding to OpenAI, Gemini, Ollama, or another compatible provider.

## 6. Configuration Diagnostics and Upgrade Compatibility

The current configuration schema version is `1`. New default configs write:

```yaml
config_schema_version: 1
```

Old configs may omit this field; if an older version is written, diagnostics will warn. If a number higher than the currently supported version is written, diagnostics will error and prompt you to upgrade the program or use a configuration supported by the current version.

You can obtain structured diagnostics before initializing the Agent via the Runtime RPC `config.validate`; clients can display `diagnostics`, `error_count`, and `warning_count`.

Post-P3 configuration diagnostics focus on:

- Type checks, old-version, and future-version prompts for top-level `config_schema_version`.
- Ranges, enums, and combinations for `resource_control`.
- Known invalid combinations, such as `api_path` configured for Ollama, or provider built-in web search fields configured for DeepSeek / Claude.
- Obviously invalid or masked resource limits, such as child resource concurrency greater than Runtime total concurrency.
- Provider field matrix tiered diagnostics: fields that are definitely unsupported return error; fields that are only not recommended or suitable only for custom gateways remain warning.

## 7. Resource Control Configuration

`resource_control` is used to limit high-cost entry-level Runtime actions, avoiding excessive concurrent requests that cause lag, resource exhaustion, or abnormal API bills.

```yaml
resource_control:
  enabled: true
  runtime_max_concurrent: 4
  runtime_queue_size: 8
  session_max_concurrent: 1
  provider_max_concurrent: 2
  stream_max_concurrent: 1
  model_max_concurrent: 2
  tool_max_concurrent: 2
  web_search_max_concurrent: 1
  image_generation_max_concurrent: 1
  dependency_install_max_concurrent: 1
  acquire_timeout_seconds: 0.25
  default_timeout_seconds: 120.0
  dependency_install_timeout_seconds: 600
  overflow_policy: "reject"
```

Notes:

- `overflow_policy: "reject"`: quickly returns `resource.limit_exceeded` when resources are full.
- `overflow_policy: "wait"`: allows waiting in queue; `acquire_timeout_seconds` must be greater than 0 in this case.
- If child resource concurrency is greater than `runtime_max_concurrent`, it will be actually limited by the Runtime total gate, and diagnostics will warn.
- Dependency installation is usually slow; we recommend `dependency_install_timeout_seconds` not less than `default_timeout_seconds`.

## 8. Create and Validate Characters

Example character YAML:

```yaml
name: "Rinnosuke Morichika"
system_prompt: |
  You are Rinnosuke Morichika, the owner of Kourindou...

greeting: "「Welcome to Kourindou.」"

begin_scene: "Rinnosuke is wiping a newly acquired Outside World item behind the counter. Kourindou is piled with strange goods."

example_dialogue:
  - user: "What is this item?"
    assistant: "「This is no ordinary Outside World item.」"
```

Field descriptions:

- `name`: character name.
- `system_prompt`: system prompt defining character identity, personality, and speaking style.
- `greeting`: static greeting displayed when creating a new session.
- `begin_scene`: scene opening description (optional). When `begin_scene: true` is set in the top-level config, the model will proactively generate a description of the character's current state based on this description, rather than reciting the static `greeting`.
- `example_dialogue`: example dialogue to help the model understand the character style.
- `metadata`: character metadata for retrieval or display.
- `motivation_weights`: four-dimension motivation weights (optional), deciding which drive dominates the urge to speak.
  Dimensions: `expression_drive`, `emotional_charge`, `relational_need`, `situational_relevance`;
  defaults `0.3 / 0.35 / 0.2 / 0.15` (sum 1); missing dimensions fall back to defaults.
- `emotion_baseline`: eight-dimension emotion baseline (optional), the character's resting mood
  and the target emotions decay toward. Dimensions: `anger`, `sorrow`, `fear`, `happy`, `love`,
  `surprised`, `disgust`, `shame` (0~1 each; omit for a calm baseline). The LLM self-reports
  emotions during conversation, and the state slowly decays toward this baseline (default
  half-life: 30 minutes); current emotion is injected into reply tone and modulates the
  speaking-drive threshold. Examples: cheerful `{happy: 0.4}`; melancholic `{sorrow: 0.3}`.

Opening mode is controlled by the top-level config `begin_scene`:

```yaml
# Proactive scene opening: prefer the character YAML's begin_scene, fallback to greeting
begin_scene: true

# User-initiated opening: only output the static greeting and wait for user input
begin_scene: false
```

`begin_scene` is suitable for scenarios where the character is "living their own life and you happen to bump into them"; `greeting` is suitable for traditional "character welcomes you" interaction. Both can coexist, with `begin_scene` switch determining which takes priority.

Standalone CLI validation entry point:

```bash
python -m GensokyoAI.cli.character_cli characters/zh_cn/HakureiReimu.yaml --json
python -m GensokyoAI.cli.character_cli characters/zh_cn --recursive
```

After installing as a package, you can also use:

```bash
gensokyoai-character characters/zh_cn/HakureiReimu.yaml --json
gensokyoai-character characters/zh_cn --recursive
```

The exit code is `1` when error-level diagnostics exist, and `0` when only warnings remain.

## 9. Character Package Usage

A `.gensokyo-character` character package is essentially a security-restricted zip archive. The root directory must contain `manifest.yaml`; the current schema version is `1`. Before import, the package checks for path traversal, duplicate files, file size, character YAML, resource declarations, external links, checksums, and basic trust metadata.

Runtime API support:

- `character_package.validate`: validates package structure, manifest, internal paths, file size, character YAML, resource list, ecosystem fields, external links, and checksum.
- `character_package.preview`: returns character preview, manifest summary, file list, and `trust` and `security` summaries.
- `character_package.import`: imports the character package into the `characters` directory; import will not proceed if error-level diagnostics exist.
- `character_package.export`: exports a local character YAML into a character package, automatically writing `checksums.sha256` inside the package.

Recommended character package manifest declarations:

- `author` / `author_url`: author or maintainer, and optional homepage.
- `license` / `license_url` / `license_detail`: license, license link, and supplementary authorization notes.
- `source`: original release page, repository, or trusted distribution page; external URLs must use `https`.
- `attribution`: list of referenced sources for declaring original settings, images, or text materials.
- `external_links`: list of external links shown to users before package installation, each containing `label`, `url`, and optional `purpose`.
- `repository`: reserved package repository index metadata, e.g. `id`, `namespace`, `url`, `homepage`, `download_url`.
- `signature`: optional signature field; current version only validates `algorithm` / `value` / `signer` format, not real cryptographic verification.
- `checksums.sha256`: SHA-256 of character YAML and resource files inside the package; automatically generated on export.

Missing `author`, `license`, `source`, `signature`, or `checksums` will produce warnings so users can judge trustworthiness; non-`https` external links such as `http`, `file`, `javascript` will produce errors.

JSON Lines RPC examples:

```json
{"method":"character_package.validate","params":{"package_path":"packages/reimu.gensokyo-character"}}
```

```json
{"method":"character_package.import","params":{"package_path":"packages/reimu.gensokyo-character","locale":"zh_cn","overwrite":false}}
```

```json
{"method":"character_package.export","params":{"character_path":"characters/zh_cn/HakureiReimu.yaml","output_path":"packages/reimu.gensokyo-character","package_id":"HakureiReimu","author":"Patchouli-CN","license":"Apache-2.0","source":"https://example.com/packages/reimu","external_links":[{"label":"Release page","url":"https://example.com/packages/reimu","purpose":"source"}]}}
```

## 10. Start a Conversation

With uv:

```bash
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --config config/local.yaml --new-session
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --resume <session_id>
uv run --extra ollama -m GensokyoAI.cli.main --list-sessions
```

With pip / regular Python:

```bash
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --config config/local.yaml --new-session
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --resume <session_id>
python -m GensokyoAI.cli.main --list-sessions
```

After installing as a package, you can also use the `gensokyoai` script entry point:

```bash
gensokyoai --character characters/zh_cn/KirisameMarisa.yaml --new-session
gensokyoai --list-sessions
```

Command-line arguments:

| Argument | Short | Description |
|----------|-------|-------------|
| `--character` | `-c` | Character config file path |
| `--config` |  | Application config file path; default `config/local.yaml` (auto-generated on first launch) |
| `--new-session` |  | Create a new session |
| `--resume` |  | Resume a session by specified ID |
| `--list-sessions` |  | List all historical sessions |
| `--no-stream` |  | Disable streaming output |

## 11. Commands During Conversation

### 11.1 Prompt Tags (Passed to AI)

- `<know>Gensokyo is located in Japan...</know>`: dynamically inject reference material.
- `<meta>Current scene: Hakurei Shrine...</meta>`: set scene / metadata.
- `<attention>Remember, you are very sleepy now...</attention>`: remind or correct AI behavior.

### 11.2 System Commands

- `/help`: show help.
- `/exit` or `/quit`: exit the program.
- `/save`: save current session.
- `/new`: create a new session.
- `/back`: roll back the last turn.
- `/sessions`: list historical sessions.
- `/stream on/off`: toggle streaming output.
- `/clear`: clear prompt context.
- `/errors`: view recent error statistics.

### 11.3 Initiative Timer Commands

When the initiative timer is enabled, after each normal AI reply, the AI can save only a `pending_summary` of its later proactive speaking intent and a trigger time. If the user sends a new message before the trigger, the old timer becomes invalid; when the time is reached or manually triggered, the system regenerates the actual proactive message to the user based on the summary, current context, and pre-speech thinking.

Whether to speak up is decided by the speaking-drive threshold model: the ThinkEngine scores four mood dimensions (expression drive / emotional charge / relational need / situational relevance), and when `total_drive` exceeds `initiative_timer.drive_threshold` (default `0.6`), a proactive message is scheduled, otherwise the character stays silent. When the AI does not want to speak, it does not speak—no accumulator, no hesitation re-judgment chain, no forced fallback (the old `fallback_*` forced fallback chain, the `hesitation_*` hesitation chain, and the `drive_*` accumulator settings have all been removed; legacy config keys only receive a migration warning).

In the console, slash commands can be used:

```text
/timer
/timer update delay 120
/timer update due 2026-06-07T21:30:00+08:00
/timer summary Remind the user to continue the previous topic later
/timer cancel
/timer trigger
```

Equivalent tag commands can also be used:

```text
<timer>summary Remind the user to continue the previous topic later</timer>
<timer>trigger</timer>
```

In configuration, it is recommended to use `initiative_timer.allow_frontend_edit_summary` to control whether the frontend can edit `pending_summary`; the old field `initiative_timer.allow_frontend_edit_message` is still read as a compatibility alias, but new configurations should migrate to `allow_frontend_edit_summary`. `initiative_timer.drive_threshold` controls the speaking-drive threshold (0-1, default `0.6`)—raise it for a more reserved character, lower it for a more talkative one.

Proactive reply master switch: to completely disable AI proactive replies, set both `initiative_timer.enabled` and `think_engine.enabled` to `false`. `initiative_timer.enabled` controls the initiative timer, and `think_engine.enabled` controls the silent thinking engine; when both are off, the AI will not speak proactively based on time or idle state.

Common uses:

- `/timer`: view current initiative timer state, trigger time, remaining seconds, and summary.
- `/timer update delay <seconds>`: modify remaining trigger delay.
- `/timer update due <ISO time>`: directly modify trigger time.
- `/timer summary <summary>`: edit `pending_summary`.
- `/timer cancel [reason]`: cancel the current initiative timer.
- `/timer trigger`: immediately trigger the current initiative timer and generate the real proactive message.

### 11.4 History Message Editing Commands

The console CLI can directly view and edit the complete historical messages of the current session. History editing reuses the session management layer's full replacement logic, keeping working memory, persisted messages, and session turn count in sync.

```text
/history
/history export session_history.json
/history import session_history.json
/history delete 3
/history insert 2 assistant Insert an assistant message
/history regen 6
```

Equivalent tag commands can also be used:

```text
<history>import session_history.json</history>
<history>regen 6</history>
```

Common uses:

- `/history [count]`: show the most recent messages; default shows the latest 20.
- `/history export [json path]`: export the current session's complete history as a JSON draft for manual editing.
- `/history import <json path>`: read `messages` from the JSON file and fully replace the current session history.
- `/history delete <message index>`: delete the message at the specified index.
- `/history insert <index> <role> <content>`: insert a `system`, `user`, `assistant`, or `tool` message at the specified position.
- `/history regen <message index>`: find the most recent `user` message from the specified index backward, truncate subsequent history, and regenerate the assistant reply.

### 11.5 Chat Commands (Local Display Only, Not Sent to AI)

- `<think>inner monologue</think>`: express character's inner thoughts.
- `<whisper>whisper</whisper>`: speak softly.
- `<ooc>out-of-character content</ooc>`: out-of-character communication.
- `<describe>environment description</describe>`: scene description.
- `<action>character action</action>`: action description.

### 11.6 Web Search Tool Configuration

GensokyoAI's own `web_search` tool now uses DuckDuckGo (`ddgs` package) by default and requires no API key. Bing HTML search, generic JSON API search, and mixed mode remain available as optional providers.

```yaml
tool:
  enabled: true
  builtin_tools: ["time", "moon", "system", "web_search"]
  web_search:
    enabled: true
    provider: "ddg"        # ddg / bing / api / mixed
    max_results: 10
    timeout: 10
    region: null           # optional, e.g. zh-CN / en-US
    safe_search: "moderate" # off / moderate / strict
```

- `provider: "ddg"`: default; calls DuckDuckGo search (the synchronous API is executed via `asyncio.to_thread` in the async context).
- `provider: "api"`: connect to a generic search API such as Tavily or BoCha; configure `tool.web_search.api.endpoint` and related fields.
- `provider: "mixed"`: runs `ddg` and `api` in parallel, then deduplicates, ranks, and truncates results by source priority and quality.
- `provider: "bing"`: Bing HTML search, kept for compatibility.

On success, the search tool returns JSON containing `items` and `diagnostics`; diagnostic failures such as disabled config, unsupported provider, provider failure, or no results are returned as structured tool errors like `web_search.disabled`, `web_search.unsupported_provider`, `web_search.provider_failed`, and `web_search.no_results`.

## 12. Runtime / HTTP Entry Point

JSON Lines RPC:

```bash
python bridge_main.py
```

HTTP / WebSocket adapter (recommended new launch method):

```bash
python -m GensokyoAI.backends.web_server --host 127.0.0.1 --port 8765
```

`runtime_http.py` remains as a compatibility wrapper; the following command still works:

```bash
python runtime_http.py --host 127.0.0.1 --port 8765
```

Common endpoints:

- `GET /health`: health check.
- `GET /info`: methods, capabilities, version, and schema information; the returned `methods`, `legacy_methods`, and `method_specs` are the preferred source for clients to discover RPC capabilities.
- `POST /rpc`: JSON RPC requests.
- `WebSocket /ws`: WebSocket RPC; `agent.send_message_stream` can push stream results event by event.

Current non-legacy Runtime RPC methods include `runtime.info`, `runtime.health`, `runtime.shutdown`, `config.validate`, `character.validate`, `character.list`, `character_package.validate`, `character_package.preview`, `character_package.import`, `character_package.export`, `agent.init`, `agent.send_message`, `agent.send_message_stream`, `model.list`, `model.info`, `session.*`, `dependency.*`, `external_tool.status`, `initiative_timer.*`, and `memory.*`. Old non-namespaced methods remain compatible but are deprecated; new clients should migrate to namespaced methods according to `method_specs[].replacement`.

## 13. Upgrade Process

Upgrade from source repository:

```bash
git pull
uv sync --extra ollama
```

If using pip:

```bash
git pull
python -m pip install -e .[default]
```

Upgrade recommendations:

1. First back up your own `config/local.yaml`, `characters/`, `sessions/`, memory data, and custom resources.
2. Check `tmp/template-conf.yaml` for new fields, especially `config_schema_version`, provider fields, and `resource_control`.
3. Before starting, use the client or Runtime `config.validate` to check the configuration.
4. Old session / memory data will be migrated automatically when read, with backups created; migration results can be viewed via `runtime.info.migration_diagnostics`.
5. See `docs/en/versioning.md` and `docs/en/changelog.md` for version policy and changelog templates.

## 14. Common Troubleshooting

### Python Version Mismatch

Symptom: installation or runtime reports Python version too low.

Solution: if using uv, run `uv python install 3.14` and then `uv sync --extra ollama` again; if using pip, manually install Python 3.14+ and confirm that the current `python` or `py -3.14` points to the new version.

### uv Not Found

Symptom: `uv` is not recognized as an internal or external command.

Solution: install uv first, or switch to pip installation.

### Provider SDK Not Installed

Symptom: using OpenAI / Claude / Gemini / Ollama reports missing module.

Solution: install the corresponding extra, for example:

```bash
uv sync --extra openai
python -m pip install -e .[openai]
```

### API Key Error or Missing

Symptom: 401 / 403 / unauthorized / invalid api key.

Solution: check `model.api_key` or environment variables; local Ollama does not need an API key.

### Proxy or Network Issues

Symptom: connection timeout, gateway HTML error page, DNS failure.

Solution: check network, proxy, `base_url`, and `use_proxy`; for third-party compatible services confirm the URL contains the correct `/v1` path.

### Ollama Not Started or Model Not Found

Symptom: connection to `localhost:11434` fails, or model not found.

Solution: start Ollama and run:

```bash
ollama pull qwen3.5:9b
```

### Wrong Configuration Field

Symptom: startup reports `Unknown config field`, range error, or enum error.

Solution: correct field names and values against `tmp/template-conf.yaml`; prefer using `config.validate` to view complete diagnostics.

### Resource Limit Error

Symptom: Runtime returns `resource.limit_exceeded`.

Solution: reduce concurrent requests, or adjust `resource_control.runtime_max_concurrent`, `session_max_concurrent`, `stream_max_concurrent`, `overflow_policy`, and `acquire_timeout_seconds`.

## 15. Test and Development Commands

Before release, it is recommended to run the full test suite, lint, format check, and type check:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m pyright
```

If the current interpreter environment lacks dev tools, we recommend using the uv development environment:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

You can also keep partial regression or compile checks as quick validation:

```bash
python -m unittest tests.test_claude_provider_conversion tests.test_deepseek_provider tests.test_model_client_embeddings
python -m compileall GensokyoAI tests
uv run python -m compileall GensokyoAI tests
```

Security regression tests are located in `tests_security/`; `python -m pytest` and `uv run pytest` will automatically collect them. You can also run them separately:

```bash
python -m pytest tests_security/
```

## 16. Security Guide

Although this project runs the Agent in a user-local process, there are still some attack surfaces to be aware of when deploying and using.

### 16.1 Credentials and Keys

- Never write real API keys into `tmp/template-conf.yaml` or `config/local.yaml`.
- Recommended injection via environment variables:
  - `GENSOKYOAI_API_KEY`: model provider API key.
  - `GENSOKYOAI_RUNTIME_TOKEN`: Runtime HTTP/WebSocket authentication token.
- Runtime Token, if enabled, should be at least 16 random characters; an empty string is treated as authentication disabled.

### 16.2 Runtime HTTP/WebSocket Security

- `allowed_origins` will reject all cross-origin requests by default; to fully open for local debugging, explicitly configure `["*"]`.
- Authentication token is only passed via `Authorization: Bearer <token>` or `X-Runtime-Token` headers; `token` in URL query string is ignored.
- Consecutive authentication failures trigger IP-based memory-level rate limiting (default 10 times within a 60-second window).
- WebSocket single message size limit is 1MB; HTTP request body size is controlled by `max_request_body_size`.

### 16.3 SSRF and External URLs

- `model.base_url`, `embedding.base_url`, and `tool.web_search.api.endpoint` will reject URLs pointing to internal networks, loopback, link-local, or cloud metadata services during configuration validation.
- The `ollama` provider is additionally allowed local private addresses, but `169.254.169.254` and other metadata addresses remain blocked.

### 16.4 Commands and Dependency Security

- External MCP tool sources that expose startup commands will reject high-risk commands such as `bash/sh/cmd/powershell/python -c` and shell metacharacters.
- Provider dependency installation uses a whitelist mechanism and supports `only_binary=True` to force wheel-only installation, reducing sdist supply chain risks.
- Do not install MCP servers or third-party tools from untrusted sources.

### 16.5 Prompt Injection

- User input is checked by lightweight heuristic detection before entering the memory system; suspicious matches trigger `security.prompt_injection_detected` on the event bus and mark `prompt_injection_suspected` in the working memory message.
- This detection does not block the message directly; instead, downstream memory summarization and topic retrieval can deprioritize or manually review flagged content.

### 16.6 External Tool Permissions

- External tools carry `permissions` metadata, including tags such as `safe`, `read-only`, `network`, `filesystem`, `destructive`, and `costly`.
- The default policy allows `safe/read-only/network`; `filesystem/destructive/costly` requires explicit client confirmation before execution.
- The allowed permission set can be customized via `ExternalToolExecutionPolicy`.

### 16.7 Beyond Code-Level Recommendations

- If a real API key has ever been committed to git history, rotate the key; to completely remove it from history, use `git filter-repo` and notify all collaborators.
- For multi-instance deployments, cross-process resource limits require an external gateway or Redis; single-process gates cannot share state.
- For untrusted MCP servers, it is recommended to use OS-level sandboxing (seccomp-bpf, Windows Job Object, etc.).
