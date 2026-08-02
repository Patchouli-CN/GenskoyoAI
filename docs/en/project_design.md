# Project Design

## Functional Design

### Character Configuration & Consistency

- **YAML Character Configuration**: Define character name, persona, greeting, and example dialogues with simple config files.
- **System Prompt Templates**: Support long prompts and example dialogues to quickly shape character personality.
- **Character Consistency Maintenance**: Through working memory and semantic memory, characters maintain context and personality consistency across long conversations.

### Two-Layer Memory System

| Memory Type | Purpose | Implementation |
|-------------|---------|----------------|
| **Working Memory** | Full conversation of current session | Sliding window, retains the most recent N turns |
| **Semantic Memory** | Long-term knowledge storage and retrieval | Topic-aware storage + scheduled distillation + forgetting curve; no vector database required by default |

### Memory Management

> **Design Philosophy**: Long-term memory should be distilled by the system on a schedule, not dependent on the model remembering to take notes.

- **Periodic memory distillation**: every `memory.distill_turns` conversation turns, the ThinkEngine automatically distills "precious memories" (facts, preferences, relationship changes, emotionally significant events) from recent working memory into semantic memory — deterministic periodic triggering with one short JSON call; can be disabled via `memory.distill_enabled`.
- **Automatic retrieval injection**: before generating replies, relevant memories are retrieved and injected into the prompt, so characters naturally recall the past without calling any tool.
- **Topic-aware storage**: automatically categorizes memories into topics and builds association graphs, feeding the long-term thinking random walk.
- **Forgetting curve**: memory weight adjustment mechanism based on importance, emotional valence, and access frequency.
- **Topic heat decay**: at read time, topic heat is computed with a half-life (default 72h, `memory.topic_half_life_hours`); cold topics are hidden from the silent-thinking random walk rather than deleted, and naturally revive when brought up again. Topics reaching `memory.topic_pin_importance` are immune, and the feature can be disabled via `memory.topic_decay_enabled`. On the write side there is also a topic cap (`memory.semantic_max_topics`, default 50): once full, adding a new topic evicts the non-pinned topic with the lowest recall weight.
- **World memory projection**: in multi-character mode, after each performance segment, the world writes a first-person perspective memory for every character present.

### Silent Thinking Engine (ThinkEngine)

> **Design Philosophy**: Characters should possess natural thinking ability, not just respond.

Give AI its own "psychological time":

- **Natural thinking**: AI actively reviews past topics when idle and there are reviewable topics.
- **Random topic paths**: Simulates human associative thinking.
- **Emotion-driven priority**: Prioritizes thinking about high-emotional-value topics.
- **Autonomous decision on dialogue timing**: Judges whether to initiate dialogue through action planning; not every thought results in speaking.

### Action Planning System

| Action Type | Description |
|-------------|-------------|
| **SPEAK** | Respond to user message |
| **INITIATIVE_SPEAK** | Proactively initiate dialogue |
| **WAIT** | Do nothing (silence is also an action) |

### Session Management

- Create, save, resume, and list sessions.
- Supports automatic persistence; background save process uses async I/O.
- Session rollback: wrong things can be retracted.
- Sessions are saved per character; selecting different characters at startup maintains their own separate conversation records.

### Tool Calling

Built-in tools give characters "superpowers":

- `get_current_time`: get current time.
- `get_current_dateinfo`: get date and weekday.
- `get_moon_phase`: get moon phase.
- `get_system_info`: get system information.
- `web_search`: web search (requires `web_search` in `tool.builtin_tools` and `tool.web_search.enabled`).
- `scene_switch` / `get_current_scene`: scene switching and querying (requires the scene system).

Tool calling has been uniformly adapted for multiple providers: OpenAI / DeepSeek / OpenAI Responses / Ollama / Claude / Gemini are converted to their respective official tool-calling formats. DeepSeek uses a separate provider to handle the `reasoning_content` round-trip required for tool calling in thinking mode; Claude uses the official Messages API `tool_use` / `tool_result` content blocks, not the OpenAI-style `role: tool`.

### Special Tags

| Command Type | Example | Description |
|--------------|---------|-------------|
| **Prompt tags** | `<know>content</know>` | Dynamically inject reference material |
| | `<meta>content</meta>` | Set scene / metadata |
| | `<attention>content</attention>` | Remind or correct AI |
| **System commands** | `/help`, `/save`, `/new` | Control program behavior |
| **Chat commands** | `<think>`, `<whisper>` | Local display only, not sent to AI |

### Multi LLM Provider Support

| Provider | Chat | Tool Calling | Embeddings | Notes |
|----------|------|--------------|------------|-------|
| **Ollama** | ✅ | ✅ | ✅ | Local model, default provider |
| **OpenAI** | ✅ | ✅ | ✅ | Chat Completions API, compatible with SiliconFlow / vLLM / Groq and other third-party services |
| **DeepSeek** | ✅ | ✅ | ❌ | DeepSeek official OpenAI-compatible API, supports thinking mode and `reasoning_content` round-trip |
| **OpenAI Responses** | ✅ | ✅ | ✅ | Official OpenAI Responses API |
| **Claude** | ✅ | ✅ | ❌ | Anthropic Claude series; official embedding models not provided |
| **Gemini** | ✅ | ✅ Basic | ✅ | Google Gemini series; tool results currently returned as text |

> Supports custom provider registration; can be extended to other LLM APIs. See [Advanced Usage](#advanced-usage) for details.

### Event-Driven Architecture

- Fully asynchronous design based on `asyncio`.
- Event bus decouples Agent, backend, tools, memory, and persistence components.
- Background task queue handles async persistence.
- Supports streaming output and typewriter effect.
- Graceful signal handling and shutdown process; Ctrl+C safe exit, minimizing data loss.

### Extensible Backend

- Abstract adapter base class `RuntimeAdapter` (`GensokyoAI.adapters`; console/nb2/web_server all inherit).
- Built-in Rich-beautified console backend.
- Command system decoupled from backend, easily extended to WebUI, QQ bot, Discord Bot, etc.

## File Structure

```text
GensokyoAI/
├── GensokyoAI/                 # Main package directory
│   ├── backends/               # Backend abstraction and implementation
│   │   ├── web_server/         # HTTP / WebSocket Runtime adapter
│   │   │   ├── http_adapter.py # aiohttp HTTP / WebSocket entry
│   │   │   ├── main.py         # CLI entry and web.run_app
│   │   │   └── __main__.py     # supports python -m GensokyoAI.backends.web_server
│   ├── background/             # Background task system
│   ├── commands/               # Command system
│   ├── core/                   # Core modules
│   │   ├── agent/              # Agent, model client, providers, response handling
│   │   │   ├── providers/      # Ollama / OpenAI / DeepSeek / OpenAI Responses / Claude / Gemini etc.
│   │   │   ├── _impl.py        # Agent main class
│   │   │   ├── model_client.py # LLM client facade
│   │   │   └── types.py        # Unified response, message, tool call types
│   │   ├── config.py           # Configuration management (YAML + environment variables)
│   │   ├── events.py           # Event bus
│   │   └── exceptions.py       # Custom exceptions
│   ├── memory/                 # Working memory, semantic memory
│   ├── session/                # Session management and persistence
│   ├── tools/                  # Tool registration, execution, built-in tools
│   └── utils/                  # Utility functions
├── characters/                 # Character config files
│   ├── example.yaml            # Character template
│   └── zh_cn/                  # Built-in Chinese characters
├── config/
│   └── default.yaml            # Default configuration
├── tests/                      # Regression tests
├── bridge_main.py              # JSON Lines Runtime RPC entry point
├── runtime_http.py             # HTTP / WebSocket Runtime entry compatibility wrapper (points to GensokyoAI/backends/web_server)
├── pyproject.toml              # Project configuration (UV / packaging scripts)
├── requirements.txt            # pip dependency list
├── run_default_uv.cmd          # Windows UV quick start script
├── run_default_pip.cmd         # Windows pip quick start script
└── README.md
```
