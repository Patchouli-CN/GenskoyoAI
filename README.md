<div align = center>
  <h1>🌸 GensokyoAI - 幻想乡 AI 角色扮演引擎</h1>
  
  [![Python Version](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/)
  [![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
  [![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
</div>

> 一个专为角色扮演设计的通用 Python AI Agent 工具包与运行时，支持 Ollama / OpenAI / OpenRouter / DeepSeek / OpenAI Responses / Claude / Gemini 等多种 LLM Provider，提供三层记忆系统、会话管理、工具调用、Provider 抽象和稳定 Runtime API。
>
> 当前项目处于 Alpha 阶段：核心 Runtime、Provider、会话、记忆和工具能力已经成型，可用于集成验证；正式发布前仍会继续收口版本号、文档、端到端验收和兼容性说明。
>
> 项目使用 Python 3.14+ 语法与运行时能力；Python 3.14 已正式可用，且是本项目有意选择的最低运行时要求。推荐通过 `uv` 启动和管理环境；`uv` 会按 `pyproject.toml` 自动选择或准备满足要求的 Python，不建议为兼容旧 Python 手动降级。

[**English README**](README_en.md) | [**中文 README**](README.md)

[**🚀 新手快速上手 QUICKSTART**](./QUICKSTART.md)
[**使用指南**](./docs/user_guide.md)
[**项目设计**](./docs/project_design.md)
[**多角色扮演设计（草案）**](./docs/multi_character_design.md)
[**Runtime API 契约**](./docs/runtime_api.md)
[**版本管理说明**](./docs/versioning.md)
[**Changelog 模板**](./docs/changelog.md)
[**配置模板**](./tmp/template-conf.yaml)
[**Q群！快来！**](https://qun.qq.com/universal-share/share?ac=1&authKey=2YjM%2FXyrxGTrkTDQMoxKM5QBzphCJzFxbXnKYDpF%2FVkmuNvH2%2BNaP2Z6l7d9LsB%2B&busi_data=eyJncm91cENvZGUiOiI2NzU2MDgzNTYiLCJ0b2tlbiI6IkROTnRsMVlMcWdPUzExZlp5T2RMbDI5eXBGRVNRcDV1blAxY2crWGhrUjdpaWVXSXoybE5CdFRSb3Q5Z3dCa0giLCJ1aW4iOiIyMjI2OTU2NTc5In0%3D&data=UBToZl_UF-gj5B9gKcj0YXcw7qCwC5DKmrw0Sh2-XNjTejEA31jAi1BONVOvh9v5PB98Y0f_Hz-MDvXiFrwnLA&svctype=4&tempid=h5_group_info)

## 项目定位

GensokyoAI 是一个 Python 纯后端工具包。它不绑定任何具体 UI、桌面程序、Web 程序或聊天平台（但自带CLI，所以也可以直接用），而是把角色扮演 Agent 的核心能力封装为可复用的 Python 包与 Runtime API。HTTP / WS Runtime 已支持单节点远程多用户隔离、JWT/RBAC、显式 Agent/Session 资源、乐观并发、幂等发送和事件回放；多节点部署仍需要 PostgreSQL 与共享对象存储后端。

核心边界：

- Python 包负责 Agent、记忆、会话、工具、Provider 调用和可选依赖管理。
- 外部调用方通过公开 Python API 或 Runtime RPC 使用这些能力。
- OpenAI、Claude、Gemini、Ollama 等 Provider 的真实调用逻辑位于 Python 后端。
- Provider SDK 依赖保持可选，不会强制安装全部模型服务依赖。
- 任意客户端、脚本、服务端适配器或第三方程序都可以在不理解内部实现的情况下调用 Runtime API。

## 版本管理与更新日志

GensokyoAI 的 release 版本号采用日期版本号，当前 release 为 `v2026.7.30.0`；Python 包版本不带 `v`，为 `2026.7.30.0`。Runtime 协议版本采用独立语义版本，当前为 `2.1.0`、主版本为 `2`；memory schema 当前为 `2`，world session schema 为 `1`，其他公开 schema 为 `1`。

[Changelog 索引](./docs/changelog.md) 说明正式发布与开发快照；当前最新正式发布记录见 [`docs/changelog/v2026.7.30.0.md`](./docs/changelog/v2026.7.30.0.md)。

## Runtime API

GensokyoAI 提供前端无关的 Runtime 边界。机器可读版本、能力、方法清单和废弃方法迁移信息可通过 `runtime.info` 获取；协议细节见 [Runtime API 契约](./docs/runtime_api.md)。

- `GensokyoAI/runtime/service.py`：通用 `RuntimeService`。
- `GensokyoAI/runtime/rpc.py`：RPC 方法注册、分发与 legacy 方法兼容。
- `GensokyoAI/runtime/dependencies.py`：可选 Provider 依赖检测与白名单安装。
- `GensokyoAI/backends/web_server/http_adapter.py`：HTTP / WebSocket Runtime 适配器。
- `GensokyoAI/backends/web_server/main.py`：HTTP / WebSocket 入口主函数。
- `bridge_main.py`：通用 JSON Lines RPC 入口，可被本地客户端或其他进程启动。
- `runtime_http.py`：HTTP / WebSocket 入口兼容包装器，指向 `GensokyoAI.backends.web_server`。

当前 Runtime RPC 支持：

- `runtime.info` / `runtime.health` / `runtime.shutdown`
- `config.validate`：在初始化前返回配置结构化 diagnostics。
- `character.validate` / `character.list`：校验、预览和列出角色 YAML。
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

旧方法名仍保留兼容：`init`、`send_message`、`send_message_stream`、`list_characters`、`create_session`、`list_sessions`、`resume_session`、`shutdown`、`dependency_status`、`install_dependencies`、`external_tool_status` 等。客户端应优先使用 `runtime.info` 返回的 `methods`、`legacy_methods` 和 `method_specs`。

## 可选 Provider 依赖

Provider SDK 保持可选安装：

- `ollama = ["ollama"]`
- `openai = ["openai>=1.0.0"]`
- `openrouter = ["openai>=1.0.0"]`
- `deepseek = ["openai>=1.0.0"]`
- `openai_responses = ["openai>=1.0.0"]`
- `claude = ["anthropic>=0.20.0"]`
- `gemini = ["google-genai>=1.0.0"]`
- `all = [...]`

依赖检测与安装由后端白名单控制。调用方只能请求 Provider 名称，例如：

```json
{"providers":["openai","deepseek"]}
```

后端会自行映射到允许安装的 Python 包，不接受任意 pip 包名或 shell 命令。

## ✨ 核心亮点

> 快速知道 GensokyoAI 能带来什么体验。

### 真人般的对话体验

GensokyoAI 不是简单的问答机器人，而是围绕“角色扮演”设计的对话引擎。角色可以拥有稳定的人设、说话习惯、问候语和示例对话，在长期交流中更容易保持一致的性格与表达方式。

### 具有更真实的记忆

对话不会只停留在当前一句话。角色可以保留近期上下文，也能把长期交流压缩成记忆，并围绕话题建立联系；后续对话中，系统会尝试检索相关记忆，帮助角色更自然地想起过去内容。

记忆管理不是简单地“全部塞进上下文”。在启用工具调用且模型选择调用记忆工具时，角色可以根据对话内容主动记住或回忆信息，并借助话题和遗忘机制让记忆更像真实交流中的印象，而不是僵硬的记录本。

### 角色有自然活动

启用静默思考后，角色可以在空闲时回顾已有话题、整理思绪；当系统判断时机合适时，还可以主动开口。这让角色不只是被动回答，而更像拥有自己的内心世界。

启用主动定时器后，角色还可以在一次正常回复结束后只积存稍后想主动表达的摘要，并设置触发时间。到点时系统会基于摘要、当前上下文和说话前思考重新生成真正发给用户的主动消息，而不是提前保存一条可能过期的完整话术。

### 角色身处真实场景

启用场景系统后，角色所处的环境（博丽神社、魔法森林、红魔馆……）被外置为常驻的结构化状态，而不是靠角色在每句对话里反复复述"我在哪"。角色开场就置身于某个场景，剧情推进时可以主动切换场景（`scene_switch`），一时想不起自己在哪也能主动查看（`get_current_scene`）。

这带来两个直接好处：一是角色扮演的注意力可以更集中在人设与对话本身，小模型也能有更稳定的临场表现；二是当前场景随会话持久化，退出重进仍停留在上次的地点。场景库全局共享、用 YAML 定义，所有角色共用同一套幻想乡地点。详见 [快速上手](./QUICKSTART.md) 与 [配置模板](./tmp/template-conf.yaml) 的 `scene` 节。

### 一台戏：多角色世界（GensokyoWorld）

启用 `world` 配置后，可以从"演一个角色"升级到"单模型演一整台戏"：多个角色同处一个舞台，由导演（Director）根据剧情、在场角色与戏剧时机决定每轮谁发言、何时切人、何时把话筒交还用户——不是按顺序轮流说话的群聊。

- **一个模型演全员**：所有角色共享同一个 ModelClient（共享大脑、统一限流），但每个角色拥有独立的会话、工作记忆与长期记忆，绝不串台；不在场的角色不知道该场景发生的事。
- **共享剧本与舞台**：公开对话按场景分片记入 SharedTranscript，角色移动由 WorldStage 统一管理；场景切换可让用户自动跟随当前演员。
- **各记各的视角**：一段表演结束后，世界会为在场角色各写一份"自己视角"的记忆——同一件事，魔理沙记的是得意，主人记的是有人擅闯。
- **世界会主动推进**：整个世界只有一个主动定时器；段落结束后世界统一规划下一次剧情推进，到点由导演从当下在场角色中选角开口。用户发言永远优先，自动取消旧的主动意图。
- **可存档可恢复**：World 会话独立持久化（舞台、剧本、各角色私有会话），退出后完整恢复，roster 差异会给出结构化诊断而不是静默串角色。

控制台加 `--world`（或 `world.enabled: true`）即可进入；完整示例见 [world 配置模板](./tmp/temp-world.yaml)，Runtime 客户端使用 `world.*` RPC（见 [Runtime API](./docs/runtime_api.md)）。单角色模式完全不受影响。

### 更好的会话管理

支持创建、保存、恢复、列出、删除、回滚、导出、重命名和完整历史编辑会话；Runtime RPC 已暴露 `session.current`、`session.delete`、`session.export`、`session.rename`、`session.messages`、`session.replace_messages`、`session.regenerate_from`、`session.rollback` 等会话管理方法。说错话可以撤回，历史会话可以继续，也可以把完整机器可读的会话包导出给其他程序使用，不同角色也可以分别维护自己的交流记录。

### 可选择不同模型服务

你可以按需求选择本地模型、OpenAI 兼容服务、DeepSeek、Claude 或 Gemini。想要本地免费运行、接入云端大模型，或混合使用不同服务，都可以通过配置完成。

### 更稳定的 API 调用

GensokyoAI 针对外部 AI 服务调用做了稳定性优化：

- 服务商偶发 500 / 502 / 503 / 504 等临时错误时，会自动等待并重试，减少网络波动导致的中断。
- embeddings 向量调用也复用同一套重试和错误处理逻辑，记忆检索、语义搜索等功能更稳定。
- 遇到代理或网关返回的大段 HTML 错误页时，会整理成更容易理解的错误提示。
- OpenAI、OpenAI 兼容服务、OpenAI Responses、OpenRouter、自定义代理等 API 地址写法更加宽容。
- 支持真正任意 `api_path`：默认路径继续走 SDK，SDK 固定 resource path 无法表达的代理路径会自动切换到自定义 HTTP 调用层。
- 支持 `extra_headers`、Provider 能力声明、ProviderDefinition 控制面、模型列表查询和更完整的流式元信息。
- 支持通过统一模型元数据注册表合并 Provider `/models`、内置快照、缓存和用户 override，从模型列表和元数据中推断 `web_search` 等模型级能力；第三方 OpenAI 兼容端点默认不会被高估为支持图片能力。
- 支持显式开启真实联网搜索执行层：OpenAI Responses 可注入 `web_search_preview`，Gemini 可映射 Google Search grounding；也支持自有 `web_search` 工具走 DuckDuckGo（`ddgs`）搜索，并保留 Bing/API 作为可选 Provider；默认关闭，不会自动联网。
- 工具注入由 ToolBuildService 统一决策，会根据模型 tools 能力、工具总开关、builtin_tools 白名单和 Provider 内置搜索配置选择工具 schema 与附加 instructions。
- 工具错误返回保留旧 `content` / `is_error` 字段，同时提供结构化 `error_code`、`technical_message`、`user_message`、`recoverable`、`action_hint` 和 `details`，便于 UI 展示与恢复动作。
- 可通过 `retry_max_attempts`、`retry_initial_delay`、`retry_backoff_factor`、`retry_status_codes` 调整自动重试策略。
- 支持可选 OAuth / Bearer token refresh 基础设施，可在 `401` 后刷新 token 并重试一次，认证事件会自动清洗敏感字段。
- 支持模型调用 timing 观测，记录请求总耗时、首 chunk、首 token、首 reasoning、推理片段统计、usage 和 finish_reason。
- 支持统一图片生成与视觉输入抽象，OpenAI 图片生成、OpenAI / Responses / Gemini / Claude 视觉消息转换已接入。
- 流式输出增加首包和中途卡住的超时保护，避免模型服务无响应时一直等待。
- Ollama、Gemini、Claude 等非 OpenAI Provider 的流式工具调用和结束事件更统一。
- 流式工具调用参数解析失败时会保留 `raw_arguments`，方便排查模型输出或网关截断问题。
- OpenAI Responses 流式 `failed` / `incomplete` 事件会转换成更明确的错误信息。
- Runtime RPC 提供 `agent.send_message_stream`：JSON Lines / HTTP RPC 会返回稳定事件列表，WebSocket Runtime 可按生成进度逐帧推送事件，方便客户端按自身传输形态消费流式结果。

完整配置示例见 [配置模板](./tmp/template-conf.yaml)。

## P0 稳定性与升级能力

近期 P0 工作已完成四条稳定性主线：配置校验、角色 YAML 校验、数据迁移基础和 Runtime 资源控制。

### 配置校验与诊断

配置加载会先通过统一校验器检查结构、字段名、字段类型、数值范围、枚举值、跨字段组合和 Provider 字段兼容性。Runtime 也提供 `config.validate`，客户端可以在初始化 Agent 前拿到机器可读的 `diagnostics`、`error_count` 和 `warning_count`。

示例 RPC：

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

### 角色 YAML 校验与预览

角色文件会检查必填字段、未知字段、字段类型、示例对话格式、metadata 类型和 prompt / greeting 长度提示。Runtime 提供 `character.validate`，可返回角色预览和结构化 diagnostics；`character.list` 也会为坏文件保留诊断信息，方便客户端展示修复建议。

### 数据迁移基础

会话文件和记忆 topic store 已写入 schema version、格式名、创建方和迁移历史。读取旧无版本会话或旧 topic store 时会迁移到当前格式，并通过 `.bak` 保留迁移前文件；`session.export` 会写入会话、记忆和导出包的 schema version，方便后续升级兼容。

### Runtime 资源控制

`resource_control` 配置用于限制 Runtime 入口级高成本动作，避免异常客户端造成请求堆积、内存上涨或 API 额度异常消耗。当前已覆盖 `agent.send_message`、`agent.send_message_stream` 和 `dependency.install` 的 Runtime 总并发、消息并发、流式并发、依赖安装并发、队列与等待超时；Provider / 工具 / web_search / image_generation 也使用同一组深层 gate。

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

资源限制触发时，Runtime 会返回 `resource.limit_exceeded` 结构化错误，并在 `details` 中携带 `resource`、`reason`、`active`、`waiting`、`max_concurrent` 和 `queue_size`。客户端可通过 `runtime.info.resource_control` 查看当前 gate 快照。

## 快速配置 Provider

### OpenAI 官方 Chat Completions

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
  base_url: null  # 默认 https://openrouter.ai/api/v1
  extra_headers:  # 可选；覆盖内置 HTTP-Referer / X-Title
    HTTP-Referer: "https://your-site.example"
    X-Title: "GensokyoAI"
```

OpenRouter 也兼容旧写法：`provider: "openai"` + `base_url: "https://openrouter.ai/api"`。推荐使用独立 `openrouter` Provider，因为它会内置 OpenRouter 推荐 headers，并从 `/models` 元数据中保留 `context_length`、`input_modalities`、`output_modalities`、`supported_parameters`、`supported_features`、`pricing`、`top_provider`、`per_request_limits` 等字段。

OpenRouter Provider 会把常见模型元数据映射为统一能力：`tools`、`vision`、`reasoning`、`web_search`、`structured_output`。如果 OpenRouter 返回的模型元数据不完整，可以继续通过 `model_capabilities_add` / `model_capabilities_remove` 修正能力判断。

### Web search 执行层

真实联网搜索默认关闭。Provider 内置搜索需要在模型配置中显式开启：

```yaml
model:
  provider: "openai_responses"  # 或 gemini
  web_search_enabled: true
  web_search_strategy: "explicit"
  web_search_allow_fallback: true
```

OpenAI Responses 会在请求中注入 `web_search_preview` 工具，并把 `url_citation` 等注解转换为统一引用；Gemini 会映射 Google Search grounding，并从 `grounding_metadata` 中提取引用。非流式响应和流式 finish chunk 都可携带 `web_search_references` 与 `web_search_diagnostics`，便于展示来源、记录搜索状态和排查降级原因。

不支持 Provider 内置搜索但支持工具调用的模型，可以开启 GensokyoAI 自有 `web_search` 工具：

```yaml
tool:
  enabled: true
  web_search:
    enabled: true
    provider: "ddg"   # ddg / bing / api / mixed
    max_results: 10
```

自有 `web_search` 工具默认走 DuckDuckGo（`ddgs`）搜索，无需 API Key；如果需要接入 Tavily、BoCha、企业搜索等 JSON API，可使用通用 API Provider：

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

`provider: "mixed"` 会并行执行 DuckDuckGo 和 API 搜索，并按来源优先级与结果质量排序、去重、截断结果。搜索工具成功时返回 JSON，包含 `items` 和 `diagnostics`，便于模型引用来源并排查搜索状态；配置禁用、Provider 不支持、Provider 失败或无结果等可诊断失败会通过结构化工具错误返回，例如 `web_search.disabled`、`web_search.unsupported_provider`、`web_search.provider_failed`、`web_search.no_results`。

### 自定义 OpenAI 兼容服务

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  api_key: "sk-..."
  base_url: "https://your-api.example.com"
```

### 自定义代理路径

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  api_key: "sk-..."
  base_url: "https://proxy.example.com"
  api_path: "/custom/chat/completions"  # 也支持 /custom/generate 这类非标准路径
  extra_headers:
    X-Custom-Gateway: "gensokyo"
```

### 自动重试策略

```yaml
model:
  retry_max_attempts: 3
  retry_initial_delay: 1.0
  retry_backoff_factor: 2.0
  retry_status_codes: [500, 502, 503, 504]
```

默认只重试临时服务端错误。如果你使用的服务商把 `429` 作为“稍后重试”，可以显式加入：

```yaml
model:
  retry_status_codes: [500, 502, 503, 504, 429]
```

不建议盲目重试 `400`、`401`、`403`、`404`，这些通常代表配置、鉴权或请求参数问题。

### 模型能力覆盖

模型列表中的能力会尽量从 Provider 声明、远端 `/models` 元数据和模型名中推断。对于 OpenAI 兼容服务、OpenRouter、Responses、Gemini 等，系统可以标记 `reasoning`、`vision`、`web_search`、`structured_output` 等模型级能力。OpenRouter 的 `supported_parameters`、`supported_features`、`pricing.internal_reasoning`、`input_modalities` 等字段会参与推断。

OpenAI 官方端点会默认声明图片输入与图片生成能力；第三方 OpenAI-compatible endpoint 默认只声明通用文本、工具、embedding 和自定义端点能力，避免把所有兼容服务都误判成支持图片。若第三方服务实际支持图片，可通过远端模型 metadata 或下面的配置显式补充。

如果服务商元数据不完整，或者你确定某个模型能力被误判，可以用配置增补或移除能力：

```yaml
model:
  provider: "openai"
  name: "your-model-name"
  model_capabilities_add:
    - "web_search"
  model_capabilities_remove:
    - "image_generation"
```

注意：`web_search` 能力元信息本身不会自动执行联网搜索；只有同时开启 `web_search_enabled: true` 且 `web_search_strategy` 不是 `off` 时，Provider 才会注入内置搜索配置。模型能力查询可通过 `ModelRegistryService` 统一获得，它会合并 Provider 远端列表、内置 fallback 快照、内存缓存和用户能力修正。

### 配置合并语义

用户配置文件会保留 `model` 字段出现信息，因此可以区分“未配置”和“显式配置为默认值”。例如默认配置或上游配置把 `temperature` 设为 `1.2` 时，用户仍可以在自己的配置中显式写回 `0.7`，并按用户意图覆盖：

```yaml
model:
  temperature: 0.7
  max_tokens: 2048
  timeout: 60
  retry_max_attempts: 3
```

这同样适用于 `provider`、`name`、`stream`、`think`、`api_path`、`extra_headers`、`model_capabilities_add` / `model_capabilities_remove` 等 `model` 字段。环境变量仍会在配置文件合并后作为最后一层覆盖。

### OAuth / token refresh

适用于需要动态 Bearer token 的 OpenAI 兼容服务、Responses API 或内部网关：

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

也可以通过环境变量覆盖常用认证字段：

- `GENSOKYOAI_AUTH_TYPE`
- `GENSOKYOAI_TOKEN_URL`
- `GENSOKYOAI_ACCESS_TOKEN`
- `GENSOKYOAI_REFRESH_TOKEN`
- `GENSOKYOAI_CLIENT_ID`
- `GENSOKYOAI_CLIENT_SECRET`

### 图片生成

OpenAI Provider 提供统一图片生成入口：

```python
from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.config import ModelConfig

client = ModelClient(
    ModelConfig(provider="openai", name="gpt-image-1", api_key="sk-...")
)

result = await client.generate_image(
    "画一只在博丽神社喝茶的猫",
    size="1024x1024",
    n=1,
)

print(result.images[0].url or result.images[0].data)
```

返回值会统一为 `ImageGenerationResult`，每张图为 `GeneratedImage`，支持 URL、base64 data、mime_type、revised_prompt 和 metadata。

### 视觉输入

聊天消息的 `content` 可以继续使用字符串，也可以使用统一多模态片段：

```python
from GensokyoAI.core.agent.types import ImageInput, MessageContentPart

messages = [
    {
        "role": "user",
        "content": [
            MessageContentPart(type="text", text="请描述这张图"),
            MessageContentPart(
                type="image",
                image=ImageInput(url="https://example.com/image.png", detail="low"),
            ),
        ],
    }
]

response = await client.chat(messages)
```

Provider 会自动转换为目标服务格式：

- OpenAI Chat Completions：`text` / `image_url`
- OpenAI Responses：`input_text` / `input_image`
- Gemini：`text` / `inline_data` / `file_data`
- Claude：`text` / `image` content blocks

## Runtime RPC 能力

GensokyoAI 提供前端无关的 Runtime 服务边界，当前可通过 [`bridge_main.py`](./bridge_main.py) 使用 JSON Lines RPC，也可通过 `python -m GensokyoAI.backends.web_server` 启动 HTTP / WebSocket adapter（[`runtime_http.py`](./runtime_http.py) 仍作为兼容包装器保留）。核心服务由 [`RuntimeService`](./GensokyoAI/runtime/service.py) 提供，RPC 方法映射由 [`dispatch_rpc()`](./GensokyoAI/runtime/rpc.py) 复用。客户端可以通过 `runtime.info` 查询当前支持的方法。

当前已暴露的主要能力包括：

- `runtime.info`：返回协议版本、capabilities、methods、legacy methods、deprecated methods、外部工具状态和资源控制摘要。
- `config.validate`：校验配置文件、内联配置、model overrides 和 embedding overrides，返回结构化 diagnostics。
- `character.validate`：校验角色文件或内联角色数据，返回角色预览、错误和警告。
- `agent.init`：初始化角色、配置与会话。
- `agent.send_message`：发送非流式消息并返回最终回复。
- `agent.send_message_stream`：返回稳定 JSON 事件列表，事件包含 `content` 和最终 `finish`，并可透传 `status`、`error`、`usage`、`finish_reason` 等字段。
- `character.list`：列出可用角色配置，并为坏文件返回结构化 diagnostics。
- `character_package.validate` / `character_package.preview` / `character_package.import` / `character_package.export`：校验、预览、导入和导出 `.gensokyo-character` 角色包。
- `model.list` / `model.info`：查询当前 Provider 的模型列表和模型元信息。
- `session.create` / `session.list` / `session.current` / `session.resume`：创建、列出、查询当前和恢复会话。
- `session.delete`：按显式 `session_id` 删除会话，并附带剩余会话数量和列表。
- `session.messages` / `session.replace_messages` / `session.regenerate_from`：读取完整历史、全量替换编辑后的消息，并从指定历史位置重新生成后续助手回复。
- `session.rollback`：按显式 `session_id` 回滚会话，返回回滚前后的轮数与消息数，便于客户端刷新界面。
- `session.export`：导出完整机器可读会话包，包含格式版本、schema version、导出时间、角色、会话元信息、消息列表、消息数量和 Runtime 基本信息。
- `session.rename`：重命名会话，标题保存到会话 `metadata.title` 中，不改变旧会话文件结构。
- `initiative_timer.current` / `initiative_timer.update` / `initiative_timer.cancel` / `initiative_timer.trigger`：查看、编辑、取消或立即触发 AI 主动定时器摘要。
- `memory.list` / `memory.search` / `memory.get` / `memory.update` / `memory.delete` / `memory.graph`：管理显式 `session_id` 对应的语义记忆与话题图。
- `scene.current` / `scene.list` / `scene.get` / `scene.switch` / `scene.graph`：查询、切换当前场景，列出场景库与连通图，供前端程序化编排舞台。
- `world.init` / `world.start` / `world.send_message` / `world.send_message_stream` / `world.state` / `world.roster` / `world.transcript` / `world.move` / `world.session.*` / `world.shutdown`：多角色 World 编排——装配与恢复 World、驱动用户回合（返回各角色发言 `turns`）、查询舞台/花名册/共享剧本、移动用户场景、管理 World 存档；WebSocket 流式逐帧推送 `world.actor.*` / `world.waiting_user` / `world.finish` 事件。
- `dependency.status` / `dependency.install`：查询和安装白名单内 Provider 可选依赖；安装动作受 Runtime 资源闸门保护。
- `external_tool.status`：查询外部工具来源状态。

CLI 主入口推荐使用模块方式或安装后的脚本入口：

```bash
uv run --extra ollama -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
python -m GensokyoAI.cli.main --character characters/zh_cn/KirisameMarisa.yaml --new-session
gensokyoai --character characters/zh_cn/KirisameMarisa.yaml --new-session
```

Windows 用户也可以直接使用 `run_default_uv.cmd`，该脚本会通过 `uv run --extra ollama -m GensokyoAI.cli.main` 启动默认角色。若使用 pip / 普通 Python，请先自行安装 Python 3.14+ 并安装依赖。

自带 CLI 的对话界面支持斜杠命令和标签命令。除 `/help`、`/save`、`/new`、`/back`、`/sessions` 等基础命令外，还可以用 `/timer` 或 `<timer>...</timer>` 查看、编辑、取消和触发主动定时器摘要，用 `/history` 或 `<history>...</history>` 查看、导出、导入、插入、删除历史消息，并从指定历史位置重新生成回复。

角色校验也提供独立命令行入口：

```bash
python -m GensokyoAI.cli.character_cli characters/zh_cn/HakureiReimu.yaml --json
python -m GensokyoAI.cli.character_cli characters/zh_cn --recursive
gensokyoai-character characters/zh_cn/HakureiReimu.yaml --json
```

安装为脚本后可使用 `gensokyoai-character`，存在 error 级诊断时退出码为 `1`，仅有 warning 时退出码仍为 `0`。

HTTP / WebSocket adapter 启动示例（推荐）：

```bash
python -m GensokyoAI.backends.web_server --host 127.0.0.1 --port 8765
```

远程多用户部署应放在 TLS/OIDC 网关后，并配置网关与 Runtime 共享的 JWT 校验参数：

```bash
GENSOKYOAI_RUNTIME_JWT_SECRET=<至少32字符> \
GENSOKYOAI_RUNTIME_JWT_ISSUER=https://auth.example \
GENSOKYOAI_RUNTIME_JWT_AUDIENCE=gensokyo-runtime \
python -m GensokyoAI.backends.web_server --host 0.0.0.0 --port 8765 --allowed-origin https://app.example
```

网络入口默认关闭 `runtime.shutdown`、`dependency.install` 和角色包导入/导出等远程管理方法。仅在隔离的管理网络中设置 `GENSOKYOAI_RUNTIME_ALLOW_REMOTE_ADMIN=true` 或传入 `--allow-remote-admin` 后开放。

兼容旧命令：

```bash
python runtime_http.py --host 127.0.0.1 --port 8765
```

可用端点：

- `GET /health`：返回 Runtime 健康状态。
- `GET /ready`：返回是否接受新请求；优雅停机 drain 期间返回 `503`。
- `GET /info`：返回 Runtime 方法列表和能力信息。
- `POST /rpc`：接收 `{"id": 1, "method": "runtime.health", "params": {}}` 形式的 JSON RPC 请求；`agent.send_message_stream` 在 HTTP RPC 中会聚合为一次响应，响应内包含 `events` 列表。
- `WebSocket /ws`：接收同样的 JSON RPC 请求；普通方法返回单帧响应，`agent.send_message_stream` 会通过 `RuntimeService.iter_message_stream()` 边生成边产出事件帧，最后发送 `done: true` 的结果帧。
- `GET /events`：Runtime 事件订阅 SSE 端点，可按事件类型或类别过滤。
- `POST /media?agent_id=...` / `GET /media/{agent_id}/{media_id}`：租户媒体上传与读取。
- `POST /character-packages`：管理员角色包上传、校验和显式信任导入；需要显式启用远程管理。

说明：JSON Lines RPC 与 HTTP `POST /rpc` 仍是一请求一响应；WebSocket `/ws` 会逐帧转发 Runtime 流式事件，适合需要实时 token / 工具调用 / finish 事件的客户端。生成 token 流不能跨连接续传；断线后应使用原 `idempotency_key` 调用 `message.status`，再通过 `session.messages` 读取权威状态。RuntimeService 当前已经提供 async iterator 形式的 `iter_message_stream()`；`send_message_stream()` 保留聚合事件列表的响应形态，便于兼容现有 JSON Lines 与 HTTP 调用方。

## API 调用层能力

GensokyoAI 的模型调用层采用 Provider 抽象，统一封装不同模型服务的差异。

- Provider 会声明自身支持的能力，例如 chat、stream、tools、embeddings、reasoning、vision、image_generation、image_edit、responses_api、custom_endpoint、web_search。
- OpenAI 兼容 Provider 支持拉取 `/models`，失败时会返回当前配置模型作为 fallback；Claude 当前返回配置模型作为稳定 fallback。
- OpenAI 官方端点默认声明图片能力；第三方 OpenAI-compatible endpoint 默认不声明图片能力，需要依赖远端 metadata 或 `model_capabilities_add` 显式补充。
- 模型级能力会结合远端 metadata、模型名和 `model_capabilities_add` / `model_capabilities_remove` 配置推断；真实联网搜索由 `web_search_enabled` 和 `web_search_strategy` 显式控制。
- chat、chat_stream、embeddings、generate_image 会尽量使用统一的错误归一化、自动重试、认证准备和事件记录逻辑。
- 流式响应现在也有首包和迭代超时保护，模型服务长时间无响应时会给出明确超时错误。
- 流式响应块可携带 `status`、`error`、`usage`、`finish_reason`，便于 UI、日志和上层运行时感知重试、结束原因和 token 用量。
- Ollama、Gemini、Claude 的工具调用和结束事件会尽量按统一 `tool_call` / `finish` 形式输出。
- 流式工具调用参数解析失败时会在 `tool_info.raw_arguments` 保留原始参数文本，帮助排查工具调用问题。
- OpenAI Responses 流式失败或不完整事件会转成明确错误，避免表现为无提示中断。
- `MODEL_CALL_TIMING` 事件可用于记录 chat、chat_stream、embeddings、generate_image 的调用耗时与推理统计。
- `MODEL_AUTH` 事件可用于观察 token 刷新开始、完成和失败，事件数据会清洗密钥与 token。
- 自定义 Provider 可以通过 capabilities、supports 和 list_models 接入统一能力体系。
- Provider 控制面由 `ProviderDefinition` 集中描述，新增 Provider 时主要补充一处定义表和 Provider 实现。
- 模型元数据查询由 `ModelRegistryService` 统一处理，可在 Provider API 失败时使用缓存或内置快照 fallback。
- 工具 schema 与工具说明由 `ToolBuildService` 统一构建，`ToolRegistry` 主要负责发现和注册工具。
- 工具执行失败会产生结构化 `ToolError`；`ToolExecutor` 返回旧字段兼容的 tool message，同时在 `error` 字段和 `TOOL_CALL_FAILED` 事件中提供 error_code、用户提示、技术原因、恢复建议和 details。

## 交流讨论

**欢迎来提供功能建议、BUG 反馈以及纯粹交流ᗜᴗᗜ！**

- [QQ群: 675608356](https://qun.qq.com/universal-share/share?ac=1&authKey=2YjM%2FXyrxGTrkTDQMoxKM5QBzphCJzFxbXnKYDpF%2FVkmuNvH2%2BNaP2Z6l7d9LsB%2B&busi_data=eyJncm91cENvZGUiOiI2NzU2MDgzNTYiLCJ0b2tlbiI6IkROTnRsMVlMcWdPUzExZlp5T2RMbDI5eXBGRVNRcDV1blAxY2crWGhrUjdpaWVXSXoybE5CdFRSb3Q5Z3dCa0giLCJ1aW4iOiIyMjI2OTU2NTc5In0%3D&data=UBToZl_UF-gj5B9gKcj0YXcw7qCwC5DKmrw0Sh2-XNjTejEA31jAi1BONVOvh9v5PB98Y0f_Hz-MDvXiFrwnLA&svctype=4&tempid=h5_group_info)

## 贡献指南

欢迎提交 Issue 和 Pull Request！详细规则见 [DCO](./DCO.md) 与 [CODE_OF_CONDUCT](./CODE_OF_CONDUCT.md)。

如果你：

- 写了新的角色配置文件，欢迎分享到 `characters/` 目录。
- 开发了新的 Provider、工具、记忆能力或 Runtime 适配器，欢迎 PR。
- 发现了 bug 或有功能建议，请提交 Issue，或者加入 [**Q群**](https://qun.qq.com/universal-share/share?ac=1&authKey=2YjM%2FXyrxGTrkTDQMoxKM5QBzphCJzFxbXnKYDpF%2FVkmuNvH2%2BNaP2Z6l7d9LsB%2B&busi_data=eyJncm91cENvZGUiOiI2NzU2MDgzNTYiLCJ0b2tlbiI6IkROTnRsMVlMcWdPUzExZlp5T2RMbDI5eXBGRVNRcDV1blAxY2crWGhrUjdpaWVXSXoybE5CdFRSb3Q5Z3dCa0giLCJ1aW4iOiIyMjI2OTU2NTc5In0%3D&data=UBToZl_UF-gj5B9gKcj0YXcw7qCwC5DKmrw0Sh2-XNjTejEA31jAi1BONVOvh9v5PB98Y0f_Hz-MDvXiFrwnLA&svctype=4&tempid=h5_group_info)

## 待办事项

- [ ] 多角色同时对话
- [ ] 语音输入 / 输出
- [ ] 更多内置工具

## 许可证

Apache License 2.0 - 详见 [LICENSE](LICENSE) 文件。

## 🙏 致谢

- [Ollama](https://ollama.ai/) - 本地模型运行
- [OpenAI](https://openai.com/) - OpenAI API 及兼容生态
- [UV](https://github.com/astral-sh/uv) - UV包管理器
- [Rich](https://github.com/Textualize/rich) - 终端美化
- [msgspec](https://github.com/jcrist/msgspec) - 高性能序列化
- [ayafileio](https://github.com/Patchouli-CN/ayafileio) - 高性能异步文件 I/O
- [上海爱丽丝幻乐团](http://www16.big.or.jp/~zun/) - 创造了幻想乡
- [灵眸AI](https://api.lmuai.com) - 感谢其国内靠谱的AI中转服务
- [芙兰蠢兔](https://github.com/AlfreScarlet) - 感谢其提供的Kimi Code账户，用于本项目开发。

## 🌟 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Patchouli-CN/GensokyoAI&type=Date)](https://star-history.com/#Patchouli-CN/GensokyoAI&Date)

---

**Made with ❤️ and 🍵 in Gensokyo**

*“只有华丽并不是魔法，弹幕最重要的是火力 DA⭐ZE！” —— 雾雨魔理沙*
