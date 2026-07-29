# 使用指南

本指南面向从源码运行、发布包安装和客户端集成用户。本文档会尽量与 `pyproject.toml`、`requirements.txt`、`run_default_uv.cmd` 和 `run_default_pip.cmd` 保持一致。当前项目处于 Alpha 收口阶段，Runtime API 已可用于集成，但正式 release 前仍可能继续做兼容性和文档收口。

## 1. 环境要求

- 操作系统：Windows / Linux / macOS 均可；Windows 用户可以直接使用仓库内的 `.cmd` 启动脚本。
- Python 运行时：项目要求 Python 3.14 或更高版本；Python 3.14 已正式可用，不再按预发布版本处理。
  - 使用 uv 时：通常只需要先安装 uv；uv 会按 `pyproject.toml` 的 `requires-python = ">=3.14"` 自动选择或下载合适的 Python 运行时。
  - 使用 pip 时：需要你自己先安装 Python 3.14+，再用这个 Python 执行 pip。
- 包管理器：推荐使用 uv；也支持 pip。
- LLM Provider：至少准备一种模型服务。
  - Ollama：本地运行，默认推荐，免费但需要先安装 Ollama 并拉取模型。
  - OpenAI / OpenRouter / DeepSeek / OpenAI Responses：需要对应 API Key，底层依赖 OpenAI SDK。
  - Claude：需要 Anthropic API Key 和 Anthropic SDK。
  - Gemini：需要 Google API Key 和 Google GenAI SDK。

如果使用 uv，可以检查 uv 可用性，并让 uv 按项目要求准备 Python：

```bash
uv --version
uv python install 3.14
```

如果使用 pip，请检查当前 Python 版本：

```bash
python --version
```

如果系统里有多个 Python，Windows 上也可以尝试：

```bat
py -3.14 --version
```

## 2. 获取项目

```bash
git clone https://github.com/Patchouli-CN/GensokyoAI.git
cd GensokyoAI
```

如果你已经下载了 zip 包，解压后在项目根目录执行后续命令即可。

## 3. 安装方式

### 3.1 使用 uv（推荐）

uv 会根据 `pyproject.toml` 自动创建虚拟环境并选择满足版本要求的 Python。安装默认本地 Ollama 依赖：

```bash
uv sync --extra ollama
```

或安装指定 Provider extra：

```bash
uv sync --extra openai
uv sync --extra openrouter
uv sync --extra deepseek
uv sync --extra openai_responses
uv sync --extra claude
uv sync --extra gemini
uv sync --extra all
```

常用 extra 对照：

| extra | 安装内容 | 适用 Provider |
|------|----------|---------------|
| `ollama` / `default` | `ollama` | `ollama` |
| `openai` | `openai>=1.0.0` | `openai` |
| `openrouter` | `openai>=1.0.0` | `openrouter` |
| `deepseek` | `openai>=1.0.0` | `deepseek` |
| `openai_responses` | `openai>=1.0.0` | `openai_responses` |
| `claude` | `anthropic>=0.20.0` | `claude` |
| `gemini` | `google-genai>=1.0.0` | `gemini` |
| `all` | Ollama / OpenAI / Anthropic / Gemini SDK | 全部内置 Provider |

### 3.2 使用 pip

只安装最小运行依赖：

```bash
python -m pip install -r requirements.txt
```

推荐以可编辑包形式安装对应 Provider extra，这样会注册命令行脚本。`requirements.txt` 只包含最小运行依赖，不会强制安装任何 LLM Provider SDK；如果要使用默认本地 Ollama 启动方式，请安装 `default` 或 `ollama` extra：

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

如果只想按需补 Provider SDK，也可以手动安装：

```bash
python -m pip install ollama
python -m pip install openai
python -m pip install anthropic
python -m pip install google-genai
```

### 3.3 Windows 快速启动脚本

项目根目录提供两个脚本：

```bat
run_default_uv.cmd
run_default_pip.cmd
```

- `run_default_uv.cmd` 会执行 `uv run --extra ollama -m GensokyoAI.cli.main --character "characters\zh_cn\KirisameMarisa.yaml" --new-session`。
- `run_default_pip.cmd` 会执行 `python -m GensokyoAI.cli.main --character "characters\zh_cn\KirisameMarisa.yaml" --new-session`。

首次使用 `run_default_uv.cmd` 前请先安装 uv；通常不需要另外手动安装 Python 3.14，uv 可以自动准备运行时，并会按 `--extra ollama` 安装默认 Ollama SDK。首次使用 `run_default_pip.cmd` 前请先手动安装 Python 3.14+，再用 `python -m pip install -e .[default]` 或 `python -m pip install -e .[ollama]` 安装默认启动所需依赖；只执行 `python -m pip install -r requirements.txt` 不会安装 Ollama SDK。

## 4. 配置主聊天模型

默认配置文件位于 `config/default.yaml`。建议复制一份自定义配置，例如 `config/local.yaml`，再通过 `--config config/local.yaml` 启动。

### 4.1 Ollama（默认本地 Provider）

先安装并启动 Ollama，然后拉取模型：

```bash
ollama pull qwen3.5:9b
```

配置示例：

```yaml
config_schema_version: 1
model:
  provider: "ollama"
  name: "qwen3.5:9b"
  base_url: null
```

Ollama 通常不要配置 `api_key` 或 `api_path`。如果配置了 `api_path`，当前配置诊断会报错，因为 Ollama Provider 不支持自定义 API path。

### 4.2 OpenAI Chat Completions

```yaml
model:
  provider: "openai"
  name: "gpt-4o"
  api_key: "sk-..."
  base_url: null
```

第三方 OpenAI 兼容服务可配置 `base_url`：

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

推荐使用独立 `deepseek` Provider，而不是把 DeepSeek 配到通用 `openai` Provider 下。

```yaml
model:
  provider: "deepseek"
  name: "deepseek-v4-pro"
  api_key: "sk-..."
  base_url: null
  thinking_enabled: true
  reasoning_effort: "high"
```

如果 `thinking_enabled: false` 但仍填写 `reasoning_effort`，配置诊断会提示该字段会被忽略。

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

## 5. 配置 Embedding 模型（可选）

Embedding 模型与主聊天模型独立配置。只有在调用 embeddings、接入向量检索或外部向量存储时才需要配置。

```yaml
embedding:
  provider: "openai"
  name: "text-embedding-3-small"
  api_key: "sk-..."
  base_url: null
  dimensions: 1024
  encoding_format: "float"
```

注意：Claude 官方不提供 Claude 自家的 embeddings。如果主聊天模型使用 Claude，需要把 embedding 配到 OpenAI、Gemini、Ollama 或其他兼容 Provider。

## 6. 配置诊断与升级兼容

当前配置 schema 版本为 `1`。新默认配置会写入：

```yaml
config_schema_version: 1
```

旧配置可以不写该字段；如果写了较旧版本，诊断会给 warning；如果写了高于当前支持版本的数字，诊断会给 error，提示你升级程序或使用当前版本支持的配置。

可以通过 Runtime RPC 的 `config.validate` 在初始化 Agent 前获得结构化诊断；客户端可以展示 `diagnostics`、`error_count` 和 `warning_count`。

P3 后配置诊断重点覆盖：

- 顶层 `config_schema_version` 的类型、旧版本和未来版本提示。
- `resource_control` 的范围、枚举和组合关系。
- 确定失败组合，例如 Ollama 配置 `api_path`、DeepSeek / Claude 配置 Provider 内置 web search 字段。
- 明显无效或被遮蔽的资源限制，例如子资源并发大于 Runtime 总并发。
- Provider 字段矩阵分级诊断：确定不支持的字段返回 error；仅不推荐或只适合自定义网关的字段保留 warning。

## 7. 资源控制配置

`resource_control` 用于限制 Runtime 入口级高成本动作，避免并发请求过多导致卡顿、资源耗尽或 API 账单异常。

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

说明：

- `overflow_policy: "reject"`：资源满时快速返回 `resource.limit_exceeded`。
- `overflow_policy: "wait"`：允许等待队列；此时 `acquire_timeout_seconds` 必须大于 0。
- 子资源并发如果大于 `runtime_max_concurrent`，会被 Runtime 总闸门实际限制，诊断会给 warning。
- 依赖安装通常较慢，建议 `dependency_install_timeout_seconds` 不小于 `default_timeout_seconds`。

## 8. 创建和校验角色

角色 YAML 示例：

```yaml
name: "森近霖之助"
system_prompt: |
  你是森近霖之助，是香霖堂的店主……

greeting: "「欢迎来到香霖堂。」"

begin_scene: "霖之助正在柜台后擦拭一件刚收来的外界道具，香霖堂里堆满了稀奇古怪的货物。"

example_dialogue:
  - user: "这个道具是什么？"
    assistant: "「这可不是普通的外界道具。」"
```

字段说明：

- `name`：角色名称。
- `system_prompt`：系统提示词，定义角色身份、性格、说话风格。
- `greeting`：静态开场白，创建新会话时直接输出。
- `begin_scene`：场景开场描述（可选）。配置 `begin_scene: true` 时，模型会基于该描述主动生成一段角色当前状态的叙述，而不是朗读静态 `greeting`。
- `example_dialogue`：示例对话，帮助模型理解角色风格。
- `metadata`：角色元数据，可用于检索或展示。

开场模式由顶层配置 `begin_scene` 控制：

```yaml
# 主动场景开场：优先使用角色 YAML 中的 begin_scene，没有则回退到 greeting
begin_scene: true

# 用户主动开场：只输出静态 greeting，等待用户先开口
begin_scene: false
```

`begin_scene` 适合想让角色"正在过自己的日子，被你撞见"的场景；`greeting` 则适合传统的"角色欢迎你"的交互。两者可同时存在，由 `begin_scene` 开关决定优先使用哪一个。

内置角色位于 `characters/zh_cn/`。

独立 CLI 校验入口：

```bash
python -m GensokyoAI.cli.character_cli characters/zh_cn/HakureiReimu.yaml --json
python -m GensokyoAI.cli.character_cli characters/zh_cn --recursive
```

安装为包后也可以使用：

```bash
gensokyoai-character characters/zh_cn/HakureiReimu.yaml --json
gensokyoai-character characters/zh_cn --recursive
```

存在 error 级诊断时退出码为 `1`；仅有 warning 时退出码为 `0`。

## 9. 角色包使用

`.gensokyo-character` 角色包本质是安全受限 zip 包，根目录必须包含 `manifest.yaml`，当前 schema version 为 `1`。导入前会检查包内路径穿越、重复文件、文件大小、角色 YAML、资源声明、外部链接、校验和和基础信任元数据。

Runtime API 支持：

- `character_package.validate`：校验包结构、manifest、包内路径、文件大小、角色 YAML、资源列表、生态字段、外部链接和 checksum。
- `character_package.preview`：返回角色预览、manifest 摘要、文件列表、`trust` 和 `security` 摘要。
- `character_package.import`：导入角色包到 `characters` 目录；存在 error 级诊断时不会导入。
- `character_package.export`：从本地角色 YAML 导出角色包，并自动写入包内 `checksums.sha256`。

建议角色包 manifest 声明：

- `author` / `author_url`：作者或维护者，以及可选主页。
- `license` / `license_url` / `license_detail`：许可证、许可证链接和补充授权说明。
- `source`：原始发布页、仓库或可信分发页面；外部 URL 仅允许 `https`。
- `attribution`：引用来源列表，用于声明原始设定、图片或文本素材来源。
- `external_links`：包安装前展示给用户的外部链接列表，每项包含 `label`、`url` 和可选 `purpose`。
- `repository`：包仓库索引元数据预留，例如 `id`、`namespace`、`url`、`homepage`、`download_url`。
- `signature`：可选签名字段；当前版本只校验 `algorithm` / `value` / `signer` 格式，不做真实加密验签。
- `checksums.sha256`：包内角色 YAML 和资源文件的 SHA-256；导出时会自动生成。

缺少 `author`、`license`、`source`、`signature` 或 `checksums` 会产生 warning，方便用户判断是否可信；`http`、`file`、`javascript` 等非 `https` 外部链接会产生 error。

JSON Lines RPC 示例：

```json
{"method":"character_package.validate","params":{"package_path":"packages/reimu.gensokyo-character"}}
```

```json
{"method":"character_package.import","params":{"package_path":"packages/reimu.gensokyo-character","locale":"zh_cn","overwrite":false}}
```

```json
{"method":"character_package.export","params":{"character_path":"characters/zh_cn/HakureiReimu.yaml","output_path":"packages/reimu.gensokyo-character","package_id":"HakureiReimu","author":"Patchouli-CN","license":"Apache-2.0","source":"https://example.com/packages/reimu","external_links":[{"label":"发布页","url":"https://example.com/packages/reimu","purpose":"source"}]}}
```

## 10. 启动对话

uv：

```bash
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --config config/local.yaml --new-session
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --resume <session_id>
uv run --extra ollama -m GensokyoAI.cli.main --list-sessions
```

pip / 普通 Python：

```bash
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --config config/local.yaml --new-session
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --resume <session_id>
python -m GensokyoAI.cli.main --list-sessions
```

安装为包后也可以使用 `gensokyoai` 脚本入口：

```bash
gensokyoai --character characters/zh_cn/KirisameMarisa.yaml --new-session
gensokyoai --list-sessions
```

命令行参数：

| 参数 | 简写 | 说明 |
|------|------|------|
| `--character` | `-c` | 角色配置文件路径 |
| `--config` |  | 应用配置文件路径，默认 `config/default.yaml` |
| `--new-session` |  | 创建新会话 |
| `--resume` |  | 恢复指定 ID 的会话 |
| `--list-sessions` |  | 列出所有历史会话 |
| `--no-stream` |  | 禁用流式输出 |

## 11. 对话中的命令

### 11.1 提示词标签（传递给 AI）

- `<know>幻想乡位于日本...</know>`：动态注入参考资料。
- `<meta>当前场景：博丽神社...</meta>`：设定场景 / 元数据。
- `<attention>记住，你现在很困...</attention>`：提醒或纠正 AI 行为。

### 11.2 系统命令

- `/help`：显示帮助。
- `/exit` 或 `/quit`：退出程序。
- `/save`：保存当前会话。
- `/new`：创建新会话。
- `/back`：回滚上一轮对话。
- `/sessions`：列出历史会话。
- `/stream on/off`：切换流式输出。
- `/clear`：清空提示词上下文。
- `/errors`：查看最近错误统计。

### 11.3 主动定时器命令

主动定时器启用后，AI 每次正常回答完成时可以只保存稍后主动发言意图摘要 `pending_summary` 和触发时间。用户在触发前发送新消息会让旧定时器失效；到点或手动触发时，系统会基于摘要、当前上下文和说话前思考重新生成真正发给用户的主动消息。

「要不要主动开口」由对话欲阈值模型决定：ThinkEngine 用四维心情模型（表达欲/情感驱动力/关系需求/情景相关性）打分，`total_drive` 超过 `initiative_timer.drive_threshold`（默认 `0.6`）即安排主动发言，否则保持沉默。AI 不想说就是不说——无累积器、无犹豫复判链、无强制兜底（旧的 `fallback_*` 强制兜底链、`hesitation_*` 犹豫链与 `drive_*` 累积器配置均已删除，旧配置键仅给出迁移警告）。

控制台中可以使用斜杠命令：

```text
/timer
/timer update delay 120
/timer update due 2026-06-07T21:30:00+08:00
/timer summary 稍后提醒用户继续刚才的话题
/timer cancel
/timer trigger
```

也可以使用等价标签命令：

```text
<timer>summary 稍后提醒用户继续刚才的话题</timer>
<timer>trigger</timer>
```

配置中推荐使用 `initiative_timer.allow_frontend_edit_summary` 控制前端是否可编辑 `pending_summary`；旧字段 `initiative_timer.allow_frontend_edit_message` 仍作为兼容别名读取，但新配置建议迁移到 `allow_frontend_edit_summary`。`initiative_timer.drive_threshold` 控制对话欲阈值（0~1，默认 `0.6`），调高则角色更沉得住气，调低则更主动。

主动回话总开关：如需完全关闭 AI 主动回话，请将 `initiative_timer.enabled` 与 `think_engine.enabled` 同时设为 `false`。`initiative_timer.enabled` 控制主动定时器，`think_engine.enabled` 控制静默思考引擎；二者都关闭后，AI 不会基于时间或空闲状态主动开口。

常见用途：

- `/timer`：查看当前主动定时器状态、触发时间、剩余秒数和摘要。
- `/timer update delay <秒数>`：修改剩余触发延迟。
- `/timer update due <ISO时间>`：直接修改触发时间。
- `/timer summary <摘要>`：编辑 `pending_summary`。
- `/timer cancel [原因]`：取消当前主动定时器。
- `/timer trigger`：立即触发当前主动定时器，并生成真正的主动消息。

### 11.4 历史消息编辑命令

控制台 CLI 可以直接查看和编辑当前会话的完整历史消息。历史编辑会复用会话管理层的全量替换逻辑，保持工作记忆、持久化消息和会话轮数同步。

```text
/history
/history export session_history.json
/history import session_history.json
/history delete 3
/history insert 2 assistant 插入一条助手消息
/history regen 6
```

也可以使用等价标签命令：

```text
<history>import session_history.json</history>
<history>regen 6</history>
```

常见用途：

- `/history [数量]`：显示最近若干条历史消息，默认显示最近 20 条。
- `/history export [json路径]`：把当前会话完整历史导出为 JSON 草稿，便于手动编辑。
- `/history import <json路径>`：从 JSON 文件读取 `messages` 并全量替换当前会话历史。
- `/history delete <消息索引>`：删除指定索引消息。
- `/history insert <索引> <role> <content>`：在指定位置插入 `system`、`user`、`assistant` 或 `tool` 消息。
- `/history regen <消息索引>`：从指定索引向前寻找最近 `user` 消息，截断后续历史并重新生成助手回复。

### 11.5 聊天命令（仅本地显示，不发送给 AI）

- `<think>内心独白</think>`：表达角色内心想法。
- `<whisper>悄悄话</whisper>`：小声说话。
- `<ooc>出戏内容</ooc>`：戏外交流。
- `<describe>环境描写</describe>`：场景描述。
- `<action>角色动作</action>`：动作描写。

### 11.6 联网搜索工具配置

GensokyoAI 自有的 `web_search` 工具默认使用 DuckDuckGo（`ddgs` 包）搜索，无需 API Key；也保留 Bing HTML 搜索、通用 JSON API 搜索以及混合模式作为可选 Provider。

```yaml
tool:
  enabled: true
  builtin_tools: ["time", "moon", "memory", "system", "web_search"]
  web_search:
    enabled: true
    provider: "ddg"        # ddg / bing / api / mixed
    max_results: 10
    timeout: 10
    region: null           # 可选，例如 zh-CN / en-US
    safe_search: "moderate" # off / moderate / strict
```

- `provider: "ddg"`：默认，调用 DuckDuckGo 搜索（同步 API 在异步上下文中通过 `asyncio.to_thread` 执行）。
- `provider: "api"`：接入 Tavily、BoCha 等通用搜索 API，需配置 `tool.web_search.api.endpoint` 等字段。
- `provider: "mixed"`：并行调用 `ddg` 与 `api`，按来源优先级与结果质量排序、去重、截断。
- `provider: "bing"`：Bing HTML 搜索，作为兼容保留。

搜索工具成功时返回包含 `items` 与 `diagnostics` 的 JSON；配置禁用、Provider 不支持、Provider 失败或无结果时会通过结构化工具错误返回，例如 `web_search.disabled`、`web_search.unsupported_provider`、`web_search.provider_failed`、`web_search.no_results`。

## 12. Runtime / HTTP 入口

JSON Lines RPC：

```bash
python bridge_main.py
```

HTTP / WebSocket adapter（推荐新启动方式）：

```bash
python -m GensokyoAI.backends.web_server --host 127.0.0.1 --port 8765
```

`runtime_http.py` 仍作为兼容包装器保留，以下命令继续有效：

```bash
python runtime_http.py --host 127.0.0.1 --port 8765
```

常用端点：

- `GET /health`：健康检查。
- `GET /info`：方法、能力、版本和 schema 信息；返回的 `methods`、`legacy_methods` 和 `method_specs` 是客户端发现 RPC 能力的首选来源。
- `POST /rpc`：JSON RPC 请求。
- `WebSocket /ws`：WebSocket RPC；`agent.send_message_stream` 可逐事件推送 stream 结果。

当前非 legacy Runtime RPC 方法包括 `runtime.info`、`runtime.health`、`runtime.shutdown`、`config.validate`、`character.validate`、`character.list`、`character_package.validate`、`character_package.preview`、`character_package.import`、`character_package.export`、`agent.init`、`agent.send_message`、`agent.send_message_stream`、`model.list`、`model.info`、`session.*`、`dependency.*`、`external_tool.status`、`initiative_timer.*` 和 `memory.*`。旧的非命名空间方法仍兼容但已废弃，新客户端应以 `method_specs[].replacement` 迁移到命名空间方法。

## 13. 升级流程

源码仓库升级：

```bash
git pull
uv sync --extra ollama
```

如果使用 pip：

```bash
git pull
python -m pip install -e .[default]
```

升级建议：

1. 先备份自己的 `config/local.yaml`、`characters/`、`sessions/`、记忆数据和自定义资源。
2. 对照 `config/default.yaml` 检查新增字段，特别是 `config_schema_version`、Provider 字段和 `resource_control`。
3. 启动前先用客户端或 Runtime 的 `config.validate` 检查配置。
4. 旧 session / memory 数据会在读取时尽量自动迁移，并创建备份；迁移结果可通过 `runtime.info.migration_diagnostics` 查看。
5. 查看 `docs/versioning.md` 和 `docs/changelog.md` 了解版本策略和变更记录模板。

## 14. 常见故障排查

### Python 版本不符

现象：安装或运行时提示 Python 版本过低。

处理：如果使用 uv，执行 `uv python install 3.14` 后重新 `uv sync --extra ollama`；如果使用 pip，请手动安装 Python 3.14+，并确认当前 `python` 或 `py -3.14` 指向新版本。

### uv 找不到

现象：`uv` 不是内部或外部命令。

处理：先安装 uv，或改用 pip 安装方式。

### Provider SDK 未安装

现象：使用 OpenAI / Claude / Gemini / Ollama 时提示缺少模块。

处理：安装对应 extra，例如：

```bash
uv sync --extra openai
python -m pip install -e .[openai]
```

### API Key 错误或缺失

现象：401 / 403 / unauthorized / invalid api key。

处理：检查 `model.api_key` 或环境变量；本地 Ollama 不需要 API Key。

### 代理或网络问题

现象：连接超时、网关 HTML 错误页、DNS 失败。

处理：检查网络、代理、`base_url` 和 `use_proxy`；第三方兼容服务确认 URL 是否包含正确 `/v1` 路径。

### Ollama 未启动或模型不存在

现象：连接 `localhost:11434` 失败，或提示 model not found。

处理：启动 Ollama，并执行：

```bash
ollama pull qwen3.5:9b
```

### 配置字段写错

现象：启动前提示 `Unknown config field`、范围错误或枚举错误。

处理：对照 `config/default.yaml` 修正字段名和值；优先使用 `config.validate` 查看完整 diagnostics。

### 资源限制错误

现象：Runtime 返回 `resource.limit_exceeded`。

处理：减少并发请求，或调整 `resource_control.runtime_max_concurrent`、`session_max_concurrent`、`stream_max_concurrent`、`overflow_policy` 和 `acquire_timeout_seconds`。

## 15. 测试与开发命令

发布前建议运行完整测试、lint、格式检查和类型检查：

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m pyright
```

如果当前解释器环境缺少 dev 工具，推荐使用 uv 开发环境：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

安全回归测试独立放在 `tests_security/` 目录，`python -m pytest` 与 `uv run pytest` 都会自动收集。也可以单独运行：

```bash
python -m pytest tests_security/
```

也可以保留局部回归或编译检查作为快速验证：

```bash
python -m unittest tests.test_claude_provider_conversion tests.test_deepseek_provider tests.test_model_client_embeddings
python -m compileall GensokyoAI tests
uv run python -m compileall GensokyoAI tests
```

## 16. 安全指南

本项目在架构上把 Agent 跑在用户本地进程内，但仍有一些攻击面需要在部署和使用时注意。

### 16.1 凭证与密钥

- 永远不要把真实 API Key 写进 `config/default.yaml` 或 `config/local.yaml`。
- 推荐通过环境变量注入：
  - `GENSOKYOAI_API_KEY`：模型 Provider 的 API Key。
  - `GENSOKYOAI_RUNTIME_TOKEN`：Runtime HTTP/WebSocket 的认证 Token。
- Runtime Token 如果启用，建议至少 16 位随机字符串；空字符串会被视为未启用认证。

### 16.2 Runtime HTTP/WebSocket 安全

- `allowed_origins` 默认会拒绝所有跨域 `Origin` 请求；如需完全开放本地调试，请显式配置 `["*"]`。
- 认证 Token 仅通过请求头 `Authorization: Bearer <token>` 或 `X-Runtime-Token` 传递，URL query string 中的 `token` 已被忽略。
- 连续认证失败会触发按 IP 的内存级限流（默认 60 秒窗口内 10 次）。
- WebSocket 单条消息大小上限为 1MB，HTTP 请求体大小由 `max_request_body_size` 控制。

### 16.3 SSRF 与外部 URL

- `model.base_url`、`embedding.base_url`、`tool.web_search.api.endpoint` 在配置校验阶段会拒绝指向内网、回环、链路本地或云元数据服务的 URL。
- `ollama` Provider 会被额外放行本地私有地址，但仍禁止 `169.254.169.254` 等元数据地址。

### 16.4 命令与依赖安全

- 外部 MCP 工具源如果暴露启动命令，会拒绝 `bash/sh/cmd/powershell/python -c` 等高风险命令以及 shell 元字符。
- Provider 依赖安装使用白名单机制，并支持 `only_binary=True` 强制只安装 wheel，降低 sdist 供应链风险。
- 不要安装来源不明的 MCP server 或第三方工具。

### 16.5 Prompt Injection

- 用户输入进入记忆系统前会进行轻量启发式检测，命中可疑模式时会在事件总线触发 `security.prompt_injection_detected`，并在工作记忆消息中标记 `prompt_injection_suspected`。
- 该检测不直接拦截消息，而是让后续记忆摘要、话题检索可以做降权或人工复核。

### 16.6 外部工具权限

- 外部工具带有 `permissions` 元数据，包含 `safe`、`read-only`、`network`、`filesystem`、`destructive`、`costly` 等标签。
- 默认策略允许 `safe/read-only/network`，`filesystem/destructive/costly` 会要求客户端显式确认后才可调用。
- 可通过 `ExternalToolExecutionPolicy` 自定义允许的权限集合。

### 16.7 超出代码层面的建议

- 如果历史提交中曾包含真实 API Key，请轮换密钥；需要彻底从历史中移除时，使用 `git filter-repo` 并通知所有协作者。
- 多实例部署时，跨进程资源限制需要外部网关或 Redis，单进程闸门无法共享状态。
- 对不可信 MCP server，建议配合操作系统级沙箱（seccomp-bpf、Windows Job Object 等）。
