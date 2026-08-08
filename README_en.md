<div align="center">
  <h1>🌸 GensokyoAI - Gensokyo AI Roleplay Engine</h1>

  [![Python Version](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/)
  [![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
  [![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
</div>

> A general-purpose Python AI Agent toolkit and runtime designed for roleplay. Supports multiple LLM providers including Ollama, OpenAI, OpenRouter, DeepSeek, OpenAI Responses, Claude, and Gemini. Features a two-layer memory system, session management, tool calling, provider abstraction, and a stable Runtime API.
>
> The project is currently in Alpha: the core runtime, providers, sessions, memory, and tool capabilities are solid and ready for integration validation. Version numbers, documentation, end-to-end acceptance, and compatibility notes will continue to be tightened before a stable release.
>
> This project uses Python 3.14+ syntax and runtime features. Python 3.14 is officially available and is the intentional minimum runtime requirement for this project. We recommend managing the environment with `uv`; `uv` will automatically select or install a compatible Python based on `pyproject.toml`. Manual downgrades for older Python versions are not recommended.

📖 [English README](README_en.md) | [中文 README](README.md)

🚀 [Quickstart for Newcomers](./QUICKSTART.md) ·
[User Guide](./docs/en/user_guide.md) ·
[Project Design](./docs/en/project_design.md) ·
[Runtime API Contract](./docs/en/runtime_api.md) ·
[Versioning](./docs/en/versioning.md) ·
[Changelog Template](./docs/en/changelog.md) ·
[Config Template](./tmp/template-conf.yaml)

> The English docs are a community translation. The Chinese version remains the authoritative source in case of ambiguity.

## Project Positioning

GensokyoAI is a pure Python backend toolkit. It is not tied to any specific UI, desktop app, web app, or chat platform (though it includes a CLI, so it can be used directly). It packages the core capabilities of a roleplay Agent into a reusable Python package and Runtime API.

Core boundaries:

- The Python package handles Agents, memory, sessions, tools, provider calls, and optional dependency management.
- External callers use these capabilities through a public Python API or Runtime RPC.
- Real call logic for OpenAI, Claude, Gemini, Ollama, and other providers lives in the Python backend.
- Provider SDK dependencies remain optional; not all model service dependencies are forced on users.
- Any client, script, server adapter, or third-party program can call the Runtime API without understanding the internal implementation.

## Version Management & Changelog

GensokyoAI release versions use calendar versioning. The latest release is `v2026.8.8.0`; the Python package version omits the `v` prefix: `2026.8.8.0`. The Runtime protocol uses independent semantic versioning, currently `2.2.0` with major version `2`; session and memory schemas are `2`, world session schema is `1`, while all other public schemas remain `1`.

The [Changelog Index](./docs/en/changelog.md) distinguishes public releases from development snapshots; the current release notes are in [`docs/en/changelog/v2026.8.8.0.md`](./docs/en/changelog/v2026.8.8.0.md).

## Runtime API

GensokyoAI provides a frontend-agnostic Runtime boundary. Machine-readable version, capabilities, method list, and deprecated method migration information are available through `runtime.info`; protocol details are in the [Runtime API Contract](./docs/en/runtime_api.md).

- `GensokyoAI/runtime/service.py`: generic `RuntimeService`.
- `GensokyoAI/runtime/rpc.py`: RPC method registration, dispatch, and legacy method compatibility.
- `GensokyoAI/runtime/dependencies.py`: optional provider dependency detection and whitelist-based installation.
- `GensokyoAI/backends/web_server/http_adapter.py`: HTTP / WebSocket Runtime adapter.
- `GensokyoAI/backends/web_server/main.py`: HTTP / WebSocket entry main function.
- `bridge_main.py`: generic JSON Lines RPC entry point, runnable by local clients or other processes.
- `runtime_http.py`: HTTP / WebSocket entry compatibility wrapper pointing to `GensokyoAI.backends.web_server`.

Current Runtime RPC support includes:

- `runtime.info` / `runtime.health` / `runtime.shutdown`
- `config.validate`: returns structured configuration diagnostics before initialization.
- `character.validate` / `character.list`: validate, preview, and list character YAML files.
- `character_package.validate` / `character_package.preview` / `character_package.import` / `character_package.export`
- `agent.init` / `agent.send_message` / `agent.send_message_stream`
- `model.list` / `model.info`
- `session.create` / `session.list` / `session.current` / `session.resume`
- `session.delete` / `session.export` / `session.rename` / `session.messages` / `session.replace_messages` / `session.regenerate_from` / `session.rollback`
- `initiative_timer.current` / `initiative_timer.update` / `initiative_timer.cancel` / `initiative_timer.trigger`
- `memory.list` / `memory.search` / `memory.get` / `memory.update` / `memory.delete` / `memory.graph`
- `scene.current` / `scene.list` / `scene.get` / `scene.switch` / `scene.graph`
- `dependency.status` / `dependency.install`
- `external_tool.status`

Legacy method names remain compatible: `init`, `send_message`, `send_message_stream`, `list_characters`, `create_session`, `list_sessions`, `resume_session`, `shutdown`, `dependency_status`, `install_dependencies`, `external_tool_status`, etc. Clients should prefer the `methods`, `legacy_methods`, and `method_specs` returned by `runtime.info`.

## Optional Provider Dependencies

Provider SDKs remain optional:

- `ollama = ["ollama"]`
- `openai = ["openai>=1.0.0"]`
- `openrouter = ["openai>=1.0.0"]`
- `deepseek = ["openai>=1.0.0"]`
- `openai_responses = ["openai>=1.0.0"]`
- `claude = ["anthropic>=0.20.0"]`
- `gemini = ["google-genai>=1.0.0"]`
- `all = [...]`

Dependency detection and installation are controlled by a backend whitelist. Callers only request provider names, for example:

```json
{"providers":["openai","deepseek"]}
```

The backend maps these to allowed Python packages; arbitrary pip package names or shell commands are not accepted.

## ✨ Core Highlights

> A quick overview of what GensokyoAI enables.

### Human-like Conversation Experience

GensokyoAI is not a simple Q&A bot. It is a dialogue engine built around roleplay. Characters can have stable personas, speech habits, greetings, and example dialogues, making it easier to maintain a consistent personality and expression style over long conversations.

### More Authentic Memory

Conversations do not end with the current sentence. Characters retain recent context, compress long-term exchanges into memories, and build connections around topics. In later conversations, the system retrieves relevant memories to help characters naturally recall the past.

Memory management is not simply "stuff everything into context," nor does it rely on the model remembering to take notes. As the conversation progresses, the system periodically distills recent exchanges into lasting "precious memories" (facts, preferences, relationship changes, emotionally significant events) and links them by topic. In later conversations, relevant memories are retrieved into the prompt so characters naturally recall the past. Together with topic awareness and forgetting curves, memories feel like real impressions rather than rigid records.

### Natural Character Activity

With silent thinking enabled, characters can review existing topics and organize thoughts during idle time. When the system judges the timing appropriate, they may also speak proactively. This makes characters feel less passive and more like they have their own inner world.

With the initiative timer enabled, a character can, after a normal reply, only store a brief summary of something they want to say later and schedule a trigger time. When the timer fires, the system regenerates the actual proactive message based on the summary, current context, and pre-speech thinking—rather than saving a full line of dialogue that may become stale.

### Characters Grounded in Real Scenes

With the scene system enabled, the environment a character is in (the Hakurei Shrine, the Forest of Magic, the Scarlet Devil Mansion...) becomes persistent, structured state instead of something the character has to restate in every message. Characters open the conversation already situated in a scene, can switch scenes on their own as the story moves (`scene_switch`), and can look up where they are when they lose track (`get_current_scene`).

Two direct benefits: the character's attention stays on personality and dialogue rather than on remembering their location—so even smaller models perform more consistently in-scene; and the current scene is persisted with the session, so resuming later keeps the character right where they left off. The scene library is globally shared and defined in YAML, with all characters drawing from the same set of Gensokyo locations. See the [Quickstart](./QUICKSTART.md) and the `scene` section of the [config template](./tmp/template-conf.yaml).

### A Whole Play: Multi-Character World (GensokyoWorld)

With the `world` config enabled, you go from "playing one character" to "one model performing an entire play": multiple characters share one stage, and a Director decides—based on the story, who's present, and dramatic timing—who speaks each turn, when to hand over, and when to give the mic back to you. It is not a round-robin group chat.

- **One model, every role**: all characters share a single ModelClient (one brain, unified rate limits), but each keeps its own session, working memory, and long-term memory—no cross-talk; characters who left the scene don't know what happened there.
- **Shared transcript and stage**: public dialogue is recorded per scene in a SharedTranscript, and movement is managed by the WorldStage; the user can automatically follow the current actor across scenes.
- **Everyone remembers their own angle**: after each performance segment, the world writes a perspective memory for every character present—the same event, Marisa remembers as a triumph, the mansion's owner as an intruder.
- **The world moves the story on its own**: there is exactly one initiative timer per world; after each segment the world plans the next push, and when it fires the Director picks a speaker from whoever is present right now. Your messages always take priority and cancel pending intents.
- **Save and resume**: world sessions persist independently (stage, transcript, each actor's private session) and restore completely, with structured diagnostics for roster differences instead of silently mixing up characters.

Run the console with `--world` (or `world.enabled: true`) to enter; see the [world config template](./tmp/temp-world.yaml) for a complete setup, and `world.*` RPC in the [Runtime API](./docs/runtime_api.md) for clients. Single-character mode is completely unaffected.

### Better Session Management

Supports creating, saving, resuming, listing, deleting, rolling back, exporting, renaming, and fully editing session history. The Runtime RPC exposes `session.current`, `session.delete`, `session.export`, `session.rename`, `session.messages`, `session.replace_messages`, `session.regenerate_from`, `session.rollback`, and more. Mistakes can be retracted, past sessions can be continued, and complete machine-readable session packages can be exported for use by other programs. Different characters can maintain their own separate conversation records.

### Choose Your Model Service

You can choose local models, OpenAI-compatible services, DeepSeek, Claude, or Gemini according to your needs. Whether you want to run locally for free, connect to cloud models, or mix different services, it can all be done through configuration.

### More Stable API Calls

GensokyoAI optimizes stability for external AI service calls:

- Automatically waits and retries on transient errors such as 500/502/503/504, reducing interruptions caused by network fluctuations.
- Embedding vector calls reuse the same retry and error handling logic, making memory retrieval and semantic search more stable.
- Large HTML error pages returned by proxies or gateways are converted into more understandable error messages.
- API address formats for OpenAI, OpenAI-compatible services, OpenAI Responses, OpenRouter, and custom proxies are more tolerant.
- Supports truly arbitrary `api_path`: default paths continue through the SDK; proxy paths that the SDK's fixed resource path cannot express automatically fall back to a custom HTTP call layer.
- Supports `extra_headers`, provider capability declarations, `ProviderDefinition` control plane, model list queries, and more complete streaming metadata.
- Supports explicit real web search execution layer: OpenAI Responses can inject `web_search_preview`; Gemini can map Google Search grounding; GensokyoAI also provides its own `web_search` tool via DuckDuckGo (`ddgs`) search, with Bing/API remaining as optional providers; disabled by default, no automatic web search.
- Tool injection is centrally decided by `ToolBuildService`, selecting tool schemas and additional instructions based on model tool capabilities, global tool switches, builtin tool whitelist, and provider built-in search configuration.
- Tool errors retain legacy `content` / `is_error` fields while providing structured `error_code`, `technical_message`, `user_message`, `recoverable`, `action_hint`, and `details`, making UI display and recovery actions easier.
- Retry policy can be adjusted via `retry_max_attempts`, `retry_initial_delay`, `retry_backoff_factor`, `retry_status_codes`.
- Optional OAuth / Bearer token refresh infrastructure can refresh tokens and retry once after `401`; authentication events automatically sanitize sensitive fields.
- Supports model call timing observation, recording total request time, first chunk, first token, first reasoning, reasoning segment statistics, usage, and finish_reason.
- Supports unified image generation and vision input abstractions; OpenAI image generation and vision message conversion for OpenAI / Responses / Gemini / Claude are integrated.
- Streaming output adds first-chunk and mid-stream stall timeout protection to avoid indefinite waiting when model services are unresponsive.
- Non-OpenAI providers such as Ollama, Gemini, and Claude have more unified streaming tool calls and end events.
- When streaming tool call argument parsing fails, `raw_arguments` is preserved to facilitate troubleshooting of model output or gateway truncation issues.
- OpenAI Responses streaming `failed` / `incomplete` events are converted into clearer error messages.
- Runtime RPC provides `agent.send_message_stream`: JSON Lines / HTTP RPC returns a stable list of events; WebSocket Runtime can push events frame by frame as generation progresses, allowing clients to consume streaming results in their preferred transport form.

The full configuration example is in [Config Template](./tmp/template-conf.yaml).

## P0 Stability & Upgrade Capability

Four recent P0 stability tracks have been completed: configuration validation, character YAML validation, data migration foundation, and Runtime resource control.

### Configuration Validation & Diagnostics

Configuration loading first passes through a unified validator that checks structure, field names, field types, value ranges, enums, cross-field combinations, and provider field compatibility. The Runtime also provides `config.validate`, allowing clients to obtain machine-readable `diagnostics`, `error_count`, and `warning_count` before initializing the Agent.

Example RPC:

```json
{
  "method": "config.validate",
  "params": {
    "config": {
      "model": {"provider": "openai", "temperature": 3}
    }
  }
}
```

### Character YAML Validation & Preview

Character files are checked for required fields, unknown fields, field types, example dialogue format, metadata types, and prompt / greeting length hints. The Runtime provides `character.validate`, returning character previews and structured diagnostics; `character.list` also retains diagnostic information for broken files, making it easier for clients to show repair suggestions.

### Data Migration Foundation

Session files and memory topic stores now write schema version, format name, creator, and migration history. Reading old unversioned sessions or old topic stores migrates them to the current format, preserving pre-migration files as `.bak`; `session.export` writes session, memory, and export package schema versions to facilitate future upgrade compatibility.

### Runtime Resource Control

The `resource_control` configuration limits high-cost entry-level Runtime actions, preventing abnormal clients from causing request pile-up, memory growth, or abnormal API quota consumption. It currently covers Runtime total concurrency, message concurrency, streaming concurrency, dependency installation concurrency, queue size, and wait timeout for `agent.send_message`, `agent.send_message_stream`, and `dependency.install`; deeper rate-limiting fields for providers / tools / web_search / image_generation are reserved.

```yaml
resource_control:
  enabled: true
  runtime_max_concurrent: 4
  runtime_queue_size: 8
  session_max_concurrent: 1
  stream_max_concurrent: 1
  dependency_install_max_concurrent: 1
  acquire_timeout_seconds: 0.25
  overflow_policy: "reject"
```

When resource limits are triggered, the Runtime returns a structured `resource.limit_exceeded` error with `resource`, `reason`, `active`, `waiting`, `max_concurrent`, and `queue_size` in `details`. Clients can view current gate snapshots via `runtime.info.resource_control`.

## Quick Provider Configuration

### OpenAI Official Chat Completions

```yaml
model:
  provider: "openai"
  name: "gpt-4o"
  api_key: "sk-..."
  base_url: null
```

### OpenAI Responses API

```yaml
model:
  provider: "openai_responses"
  name: "gpt-5"
  api_key: "sk-..."
  base_url: null
  web_search_enabled: true
  web_search_strategy: "explicit"
  web_search_context_size: "medium"
```

### OpenRouter

```yaml
model:
  provider: "openrouter"
  name: "openai/gpt-4o"
  api_key: "sk-or-..."
  base_url: null  # default https://openrouter.ai/api/v1
  extra_headers:  # optional; overrides built-in HTTP-Referer / X-Title
    HTTP-Referer: "https://your-site.example"
    X-Title: "GensokyoAI"
```

OpenRouter also supports the legacy style: `provider: "openai"` + `base_url: "https://openrouter.ai/api"`. We recommend the standalone `openrouter` provider because it includes OpenRouter-recommended headers and preserves fields such as `context_length`, `input_modalities`, `output_modalities`, `supported_parameters`, `supported_features`, `pricing`, `top_provider`, and `per_request_limits` from `/models` metadata.

The OpenRouter provider maps common model metadata to unified capabilities: `tools`, `vision`, `reasoning`, `web_search`, `structured_output`. If OpenRouter returns incomplete metadata, you can correct capability detection via `model_capabilities_add` / `model_capabilities_remove`.

### Web Search Execution Layer

Real web search is disabled by default. Provider built-in search must be explicitly enabled in model configuration:

```yaml
model:
  provider: "openai_responses"  # or gemini
  web_search_enabled: true
  web_search_strategy: "explicit"
  web_search_allow_fallback: true
```

OpenAI Responses injects the `web_search_preview` tool and converts annotations such as `url_citation` into unified citations; Gemini maps Google Search grounding and extracts citations from `grounding_metadata`. Both non-streaming responses and streaming finish chunks can carry `web_search_references` and `web_search_diagnostics` for source display, search status logging, and troubleshooting fallback reasons.

For models that do not support provider built-in search but do support tool calling, you can enable GensokyoAI's own `web_search` tool:

```yaml
tool:
  enabled: true
  web_search:
    enabled: true
    provider: "ddg"   # ddg / bing / api / mixed
    max_results: 10
```

The built-in `web_search` tool now defaults to DuckDuckGo (`ddgs`) search and requires no API key. To connect to Tavily, BoCha, enterprise search, or other JSON APIs, use the generic API provider:

```yaml
tool:
  web_search:
    enabled: true
    provider: "api"
    api:
      endpoint: "https://api.tavily.com/search"
      method: "POST"
      api_key: "tvly-..."
      results_path: "results"
      title_path: "title"
      url_path: "url"
      snippet_path: "content"
```

`provider: "mixed"` runs DuckDuckGo and API searches in parallel, then deduplicates, ranks, and truncates results by source priority and quality. On success, the search tool returns JSON containing `items` and `diagnostics` so the model can cite sources and troubleshoot search status; diagnostic failures such as disabled configuration, unsupported provider, provider failure, or no results are returned as structured tool errors like `web_search.disabled`, `web_search.unsupported_provider`, `web_search.provider_failed`, `web_search.no_results`.

### Custom OpenAI-Compatible Service

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  api_key: "sk-..."
  base_url: "https://your-api.example.com"
```

### Custom Proxy Path

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  api_key: "sk-..."
  base_url: "https://proxy.example.com"
  api_path: "/custom/chat/completions"  # also supports non-standard paths like /custom/generate
  extra_headers:
    X-Custom-Gateway: "gensokyo"
```

### Retry Policy

```yaml
model:
  retry_max_attempts: 3
  retry_initial_delay: 1.0
  retry_backoff_factor: 2.0
  retry_status_codes: [500, 502, 503, 504]
```

By default, only transient server-side errors are retried. If your provider uses `429` as a "retry later" signal, you can add it explicitly:

```yaml
model:
  retry_status_codes: [500, 502, 503, 504, 429]
```

We do not recommend blindly retrying `400`, `401`, `403`, `404`, as these usually indicate configuration, authentication, or request parameter issues.

### Model Capability Overrides

Model capabilities are inferred from provider declarations, remote `/models` metadata, and model names. For OpenAI-compatible services, OpenRouter, Responses, and Gemini, the system can flag model-level capabilities such as `reasoning`, `vision`, `web_search`, `structured_output`. Official OpenAI endpoints default to declaring image input and image generation capabilities; third-party OpenAI-compatible endpoints default to only general text, tools, embedding, and custom endpoint capabilities to avoid misjudging all compatible services as image-capable.

If provider metadata is incomplete or you know a capability is misjudged, you can add or remove capabilities via configuration:

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  model_capabilities_add:
    - "web_search"
  model_capabilities_remove:
    - "image_generation"
```

Note: the `web_search` capability metadata alone does not automatically perform web search; the provider only injects built-in search configuration when `web_search_enabled: true` and `web_search_strategy` is not `off`. Model capability queries are unified through `ModelRegistryService`, which merges provider remote lists, built-in fallback snapshots, in-memory cache, and user capability overrides.

### Configuration Merge Semantics

User configuration files preserve field presence information for `model` fields, so the system can distinguish between "not configured" and "explicitly set to default value." For example, if the default or upstream config sets `temperature` to `1.2`, the user can still explicitly write `0.7` in their own config, and it will be honored:

```yaml
model:
  temperature: 0.7
  max_tokens: 2048
  timeout: 60
  retry_max_attempts: 3
```

This applies to `provider`, `name`, `stream`, `think`, `api_path`, `extra_headers`, `model_capabilities_add` / `model_capabilities_remove`, and other `model` fields. Environment variables still serve as the final override after configuration file merging.

### OAuth / Token Refresh

Suitable for OpenAI-compatible services, Responses API, or internal gateways requiring dynamic Bearer tokens:

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  base_url: "https://your-api.example.com"
  auth:
    auth_type: "bearer"
    token_url: "https://auth.example.com/oauth/token"
    client_id: "your-client-id"
    client_secret: "your-client-secret"
    refresh_token: "your-refresh-token"
    refresh_before_seconds: 60
    allow_401_refresh: true
```

Common authentication fields can also be overridden via environment variables:

- `GENSOKYOAI_AUTH_TYPE`
- `GENSOKYOAI_TOKEN_URL`
- `GENSOKYOAI_ACCESS_TOKEN`
- `GENSOKYOAI_REFRESH_TOKEN`
- `GENSOKYOAI_CLIENT_ID`
- `GENSOKYOAI_CLIENT_SECRET`

### Image Generation

The OpenAI provider offers a unified image generation entry point:

```python
from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.config import ModelConfig

client = ModelClient(ModelConfig(provider="openai", name="gpt-image-1", api_key="sk-..."))

result = await client.generate_image(
    "Draw a cat drinking tea at the Hakurei Shrine",
    size="1024x1024",
    n=1,
)

print(result.images[0].url or result.images[0].data)
```

The return value is unified as `ImageGenerationResult`, with each image as a `GeneratedImage` supporting `url`, `data`, `mime_type`, `revised_prompt`, and `metadata`.

### Vision Input

Chat message `content` can continue to use strings, or unified multimodal fragments:

```python
from GensokyoAI.core.agent.types import ImageInput, MessageContentPart

messages = [
    {
        "role": "user",
        "content": [
            MessageContentPart(type="text", text="Please describe this image"),
            MessageContentPart(
                type="image",
                image=ImageInput(url="https://example.com/image.png", detail="low"),
            ),
        ],
    }
]

response = await client.chat(messages)
```

The provider automatically converts to the target service format:

- OpenAI Chat Completions: `text` / `image_url`
- OpenAI Responses: `input_text` / `input_image`
- Gemini: `text` / `inline_data` / `file_data`
- Claude: `text` / `image` content blocks

## Runtime RPC Capabilities

GensokyoAI provides a frontend-agnostic Runtime service boundary, currently accessible via [`bridge_main.py`](./bridge_main.py) as JSON Lines RPC and via `python -m GensokyoAI.backends.web_server` as HTTP / WebSocket adapter ([`runtime_http.py`](./runtime_http.py) remains as a compatibility wrapper). Core services are provided by [`RuntimeService`](./GensokyoAI/runtime/service.py); RPC method mapping is reused by [`dispatch_rpc()`](./GensokyoAI/runtime/rpc.py). Clients can query currently supported methods via `runtime.info`.

Main capabilities currently exposed:

- `runtime.info`: returns protocol version, capabilities, methods, legacy methods, deprecated methods, external tool status, and resource control summary.
- `config.validate`: validates configuration files, inline config, model overrides, and embedding overrides, returning structured diagnostics.
- `character.validate`: validates character files or inline character data, returning character preview, errors, and warnings.
- `agent.init`: initializes character, configuration, and session.
- `agent.send_message`: sends a non-streaming message and returns the final reply.
- `agent.send_message_stream`: returns a stable list of JSON events containing `content` and final `finish`, and may pass through `status`, `error`, `usage`, `finish_reason`, etc.
- `character.list`: lists available character configurations, returning structured diagnostics for broken files.
- `character_package.validate` / `character_package.preview` / `character_package.import` / `character_package.export`: validate, preview, import, and export `.gensokyo-character` character packages.
- `model.list` / `model.info`: query the current provider's model list and model metadata.
- `session.create` / `session.list` / `session.current` / `session.resume`: create, list, query current, and resume sessions.
- `session.delete`: deletes a session; after deleting the current session, returns an empty current session along with remaining session count and list.
- `session.messages` / `session.replace_messages` / `session.regenerate_from`: read full history, fully replace edited messages, and regenerate subsequent assistant replies from a specified history position.
- `session.rollback`: rolls back the current session, returning the number of turns and messages before and after rollback for client UI refresh.
- `session.export`: exports a complete machine-readable session package including format version, schema version, export time, character, session metadata, message list, message count, and basic Runtime information.
- `session.rename`: renames a session; the title is saved to session `metadata.title` without changing the old session file structure.
- `initiative_timer.current` / `initiative_timer.update` / `initiative_timer.cancel` / `initiative_timer.trigger`: view, edit, cancel, or immediately trigger AI initiative timer summaries.
- `memory.list` / `memory.search` / `memory.get` / `memory.update` / `memory.delete` / `memory.graph`: manage current session semantic memory and topic graph.
- `scene.current` / `scene.list` / `scene.get` / `scene.switch` / `scene.graph`: query and switch the current scene, list the scene library and connectivity graph—so frontends can orchestrate the stage programmatically.
- `world.init` / `world.start` / `world.send_message` / `world.send_message_stream` / `world.state` / `world.roster` / `world.transcript` / `world.move` / `world.session.*` / `world.shutdown`: multi-character World orchestration—assemble and resume a World, drive user turns (returning per-actor `turns`), query stage/roster/shared transcript, move the user across scenes, and manage World sessions; WebSocket streams `world.actor.*` / `world.waiting_user` / `world.finish` events frame by frame.
- `dependency.status` / `dependency.install`: query and install whitelist provider optional dependencies; installation actions are protected by Runtime resource gates.
- `external_tool.status`: query external tool source status.

The recommended CLI entry points are module-based or installed script entry points:

```bash
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
gensokyoai --character characters/zh_cn/KirisameMarisa.yaml --new-session
```

Windows users can also use `run_default_uv.cmd`, which launches the default character via `uv run --extra ollama -m GensokyoAI.cli.main`. If using pip / regular Python, please install Python 3.14+ and dependencies yourself.

The built-in CLI chat interface supports slash commands and tag commands. Besides basic commands like `/help`, `/save`, `/new`, `/back`, `/sessions`, you can use `/timer` or `<timer>...</timer>` to view, edit, cancel, and trigger initiative timer summaries, and `/history` or `<history>...</history>` to view, export, import, insert, and delete historical messages, as well as regenerate replies from a specified history position.

Character validation also has a standalone CLI entry point:

```bash
python -m GensokyoAI.cli.character_cli characters/zh_cn/HakureiReimu.yaml --json
python -m GensokyoAI.cli.character_cli characters/zh_cn --recursive
gensokyoai-character characters/zh_cn/HakureiReimu.yaml --json
```

After installation, `gensokyoai-character` is available as a script; the exit code is `1` when error-level diagnostics exist, and `0` when only warnings remain.

HTTP / WebSocket adapter startup example (recommended):

```bash
python -m GensokyoAI.backends.web_server --host 127.0.0.1 --port 8765
```

Compatibility wrapper:

```bash
python runtime_http.py --host 127.0.0.1 --port 8765
```

Available endpoints:

- `GET /health`: returns Runtime health status.
- `GET /info`: returns Runtime method list and capability information.
- `POST /rpc`: receives JSON RPC requests in the form `{"id": 1, "method": "runtime.health", "params": {}}`; `agent.send_message_stream` is aggregated into a single response containing an `events` list in HTTP RPC.
- `WebSocket /ws`: receives the same JSON RPC requests; ordinary methods return a single-frame response, while `agent.send_message_stream` pushes event frames as generation progresses via `RuntimeService.iter_message_stream()`, ending with a `done: true` result frame.
- `GET /events`: Runtime event subscription SSE endpoint, filterable by event type or category.

Note: JSON Lines RPC and HTTP `POST /rpc` are still one-request-one-response; WebSocket `/ws` forwards Runtime streaming events frame by frame, suitable for clients needing real-time token / tool call / finish events. `RuntimeService` already provides `iter_message_stream()` in async iterator form; `send_message_stream()` retains the aggregated event list response form for compatibility with existing JSON Lines and HTTP callers.

## API Call Layer Capabilities

GensokyoAI's model call layer uses provider abstraction to unify differences between model services.

- Providers declare their supported capabilities, e.g., chat, stream, tools, embeddings, reasoning, vision, image_generation, image_edit, responses_api, custom_endpoint, web_search.
- OpenAI-compatible providers support fetching `/models`; on failure, the configured model is returned as a fallback; Claude currently returns the configured model as a stable fallback.
- Official OpenAI endpoints default to declaring image input and image generation capabilities; third-party OpenAI-compatible endpoints default to only general text, tools, embedding, and custom endpoint capabilities to avoid misjudging all compatible services as image-capable.
- Model-level capabilities are inferred from remote metadata, model name, and `model_capabilities_add` / `model_capabilities_remove` configuration; real web search is explicitly controlled by `web_search_enabled` and `web_search_strategy`.
- chat, chat_stream, embeddings, and generate_image reuse unified error normalization, automatic retry, authentication preparation, and event logging as much as possible.
- Streaming responses now also have first-chunk and iteration timeout protection, giving clear timeout errors when model services are unresponsive for a long time.
- Streaming response chunks can carry `status`, `error`, `usage`, `finish_reason`, making it easier for UI, logs, and upper-layer runtimes to sense retries, end reasons, and token usage.
- Ollama, Gemini, and Claude tool calls and end events are normalized to unified `tool_call` / `finish` forms as much as possible.
- When streaming tool call argument parsing fails, the original argument text is preserved in `tool_info.raw_arguments` to help troubleshoot tool call issues.
- OpenAI Responses streaming failure or incomplete events are converted into clear errors to avoid silent interruption.
- `MODEL_CALL_TIMING` events can record chat, chat_stream, embeddings, and generate_image call durations and reasoning statistics.
- `MODEL_AUTH` events can observe token refresh start, completion, and failure; event data sanitizes keys and tokens.
- Custom providers can join the unified capability system through capabilities, supports, and list_models.
- The provider control plane is centrally described by `ProviderDefinition`; adding a provider mainly means supplementing one definition table and a provider implementation.
- Model metadata queries are handled uniformly by `ModelRegistryService`, which can use cache or built-in snapshot fallback when provider API fails.
- Tool schemas and tool descriptions are built uniformly by `ToolBuildService`; `ToolRegistry` is mainly responsible for discovery and registration.
- Tool execution failures produce structured `ToolError`; `ToolExecutor` returns tool messages compatible with legacy fields while providing `error` field and `TOOL_CALL_FAILED` events with error_code, user prompt, technical reason, recovery suggestion, and details.

## Discussion & Feedback

**Welcome feature suggestions, bug reports, and casual chat!**

- QQ Group: 675608356 (Chinese community)
- GitHub Issues / Discussions

## Contributing

Issues and Pull Requests are welcome! Please see [DCO](./DCO.md) and [CODE_OF_CONDUCT](./CODE_OF_CONDUCT.md) for detailed rules.

If you:

- Write new character config files, feel free to share them in the `characters/` directory.
- Develop new providers, tools, memory capabilities, or Runtime adapters, PRs are welcome.
- Find bugs or have feature suggestions, please open an Issue or join the QQ group.

## TODO

- [ ] Multi-character simultaneous dialogue
- [ ] Voice input / output
- [ ] More built-in tools

## License

Apache License 2.0 - see [LICENSE](LICENSE) for details.

## 🙏 Acknowledgments

- [Ollama](https://ollama.ai/) - local model running
- [OpenAI](https://openai.com/) - OpenAI API and compatible ecosystem
- [UV](https://github.com/astral-sh/uv) - Python package manager
- [Rich](https://github.com/Textualize/rich) - terminal beautification
- [msgspec](https://github.com/jcrist/msgspec) - high-performance serialization
- [ayafileio](https://github.com/Patchouli-CN/ayafileio) - high-performance async file I/O
- [Team Shanghai Alice](http://www16.big.or.jp/~zun/) - creator of Gensokyo

## 🌟 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Patchouli-CN/GensokyoAI&type=Date)](https://star-history.com/#Patchouli-CN/GensokyoAI&Date)

---

**Made with ❤️ and 🍵 in Gensokyo**

*"Danmaku isn't just about being showy, the most important thing is firepower DA⭐ZE!" — Marisa Kirisame*
