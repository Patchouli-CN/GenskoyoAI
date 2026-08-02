# 项目设计

## 功能设计

### 角色配置与角色一致性

- **YAML 角色配置**：用简单配置文件定义角色名称、人设、问候语和示例对话。
- **系统提示词模板**：支持长提示词和示例对话，快速塑造角色性格。
- **场景开场（begin_scene）**：可选的主动开场描述。开启后，模型会基于场景描述生成角色当前正在做的事，而不是输出静态问候语，让角色更像"正在过自己的日子"。
- **角色一致性维护**：通过工作记忆和语义记忆，让角色在长对话中保持上下文和性格一致。

### 两层记忆系统

| 记忆类型 | 作用 | 实现方式 |
|---------|------|---------|
| **工作记忆** | 当前会话的完整对话 | 滑动窗口，保留最近 N 轮 |
| **语义记忆** | 长期知识存储和检索 | 话题感知存储 + 定期蒸馏 + 遗忘曲线，默认不依赖向量数据库 |

### 记忆管理

> **设计哲学**：长期记忆应当由系统定期沉淀，而不是依赖模型自觉去记。

- **定期记忆蒸馏**：每 `memory.distill_turns` 轮对话后，ThinkEngine 自动从近期工作记忆提炼「珍贵记忆」（事实/偏好/关系变化/有情感重量的事件）写入语义记忆——确定性周期触发，一次短 JSON 调用，可用 `memory.distill_enabled` 关闭。
- **自动检索注入**：生成回复前按关键词检索相关记忆注入提示词，角色自然想起过去，无需主动调用工具。
- **话题感知存储**：自动将记忆归类到话题，建立关联图谱，供长期思考的随机游走使用。
- **遗忘曲线**：基于重要性、情感效价和访问频率的记忆权重调整机制。
- **话题热度淘汰**：读取时按半衰期（默认 72h，`memory.topic_half_life_hours`）现算话题热度，冷话题对静默思考游走隐藏而非删除，被重新谈起时自然复活；重要性达 `memory.topic_pin_importance` 的话题免疫衰减，可用 `memory.topic_decay_enabled` 关闭。写入侧另有话题数上限（`memory.semantic_max_topics`，默认 50）：达到上限新增话题时，淘汰回忆权重最低的非 pin 话题。
- **World 记忆投影**：多角色模式下，每段剧情结束自动为每个在场角色各写一份「自己视角」的记忆。

### 静默思考引擎（ThinkEngine）

> **设计哲学**：角色应当拥有自然思考的能力而非只有回应。

让 AI 拥有自己的“心理时间”：

- **自然思考**：AI 在空闲且已有可回顾话题时主动回顾过往话题。
- **随机话题路线**：模拟人类联想式思维。
- **情感驱动优先**：优先思考高情感值的话题。
- **自主决策对话时间**：通过行动规划判断是否主动发起对话；不保证每次思考都会开口。

### 行动规划系统

| 行动类型 | 说明 |
|---------|------|
| **SPEAK** | 回应用户消息 |
| **INITIATIVE_SPEAK** | 主动发起对话 |
| **WAIT** | 什么都不做（沉默也是一种行动） |

### 会话管理

- 创建、保存、恢复、列出会话。
- 支持自动持久化，后台保存流程使用异步 I/O。
- 会话回滚，说错话可以撤回。
- 会话按角色保存；启动时选择不同角色即可维护各自的会话记录。

### 工具调用

内置工具让角色拥有“超能力”：

- `get_current_time`：获取当前时间。
- `get_current_dateinfo`：获取日期和曜日（七曜日！）。
- `get_moon_phase`：获取月相。
- `get_system_info`：获取系统信息。
- `web_search`：联网搜索（需在 `tool.builtin_tools` 中加入 `web_search` 并启用 `tool.web_search`）。
- `scene_switch` / `get_current_scene`：场景切换与查询（需启用场景系统）。

工具调用已统一适配多 Provider：OpenAI / DeepSeek / OpenAI Responses / Ollama / Claude / Gemini 会转换为各自官方要求的工具调用格式。DeepSeek 使用独立 Provider 处理 thinking mode 下工具调用所需的 `reasoning_content` 回传；Claude 使用官方 Messages API 的 `tool_use` / `tool_result` content block，不使用 OpenAI 风格的 `role: tool`。

### 特殊标签

| 命令类型 | 示例 | 说明 |
|---------|------|------|
| **提示词标签** | `<know>内容</know>` | 动态注入参考资料 |
| | `<meta>内容</meta>` | 设定场景 / 元数据 |
| | `<attention>内容</attention>` | 提醒或纠正 AI |
| **系统命令** | `/help`, `/save`, `/new` | 控制程序行为 |
| **聊天命令** | `<think>`, `<whisper>` | 本地显示，不发给 AI |

### 多 LLM Provider 支持

| Provider | 对话 | 工具调用 | Embeddings | 说明 |
|----------|------|----------|------------|------|
| **Ollama** | ✅ | ✅ | ✅ | 本地模型，默认 Provider |
| **OpenAI** | ✅ | ✅ | ✅ | Chat Completions API，兼容 SiliconFlow / vLLM / Groq 等第三方服务 |
| **DeepSeek** | ✅ | ✅ | ❌ | DeepSeek 官方 OpenAI 兼容 API，支持 thinking mode 与 `reasoning_content` 回传 |
| **OpenAI Responses** | ✅ | ✅ | ✅ | OpenAI 官方 Responses API |
| **Claude** | ✅ | ✅ | ❌ | Anthropic Claude 系列；官方不提供自家 embedding 模型 |
| **Gemini** | ✅ | ✅ 基础 | ✅ | Google Gemini 系列；工具结果当前以文本形式回传 |

> 支持自定义 Provider 注册，可以扩展到其他 LLM API，具体见[此处](#高级用法)。

### 事件驱动架构

- 全异步设计，基于 `asyncio`。
- 事件总线解耦 Agent、后端、工具、记忆和持久化组件。
- 后台任务队列处理异步持久化。
- 支持流式输出和打字机效果。
- 优雅的信号处理和关闭流程，Ctrl+C 安全退出，尽量保证数据不丢失。

### 可扩展后端

- 适配器抽象基类 `RuntimeAdapter`（`GensokyoAI.adapters`；console/nb2/web_server 统一继承）。
- 内置 Rich 美化的控制台后端。
- 命令系统与后端解耦，易于扩展为 WebUI、QQ 机器人、Discord Bot 等。

## 文件结构

```text
GensokyoAI/
├── GensokyoAI/                 # 主包目录
│   ├── backends/               # 后端抽象与实现
│   │   ├── web_server/         # HTTP / WebSocket Runtime 适配器
│   │   │   ├── http_adapter.py # aiohttp HTTP / WebSocket 入口
│   │   │   ├── main.py         # 命令行入口与 web.run_app
│   │   │   └── __main__.py     # 支持 python -m GensokyoAI.backends.web_server
│   ├── background/             # 后台任务系统
│   ├── commands/               # 命令系统
│   ├── core/                   # 核心模块
│   │   ├── agent/              # Agent、模型客户端、Provider、响应处理
│   │   │   ├── providers/      # Ollama / OpenAI / DeepSeek / OpenAI Responses / Claude / Gemini 等 Provider
│   │   │   ├── _impl.py        # Agent 主类
│   │   │   ├── model_client.py # LLM 客户端 Facade
│   │   │   └── types.py        # 统一响应、消息、工具调用类型
│   │   ├── config.py           # 配置管理（YAML + 环境变量）
│   │   ├── events.py           # 事件总线
│   │   └── exceptions.py       # 自定义异常
│   ├── memory/                 # 工作记忆、语义记忆
│   ├── session/                # 会话管理与持久化
│   ├── tools/                  # 工具注册、工具执行、内置工具
│   └── utils/                  # 工具函数
├── characters/                 # 角色配置文件
│   ├── example.yaml            # 角色模板
│   └── zh_cn/                  # 中文内置角色
├── config/
│   └── default.yaml            # 默认配置
├── tests/                      # 回归测试
├── bridge_main.py              # JSON Lines Runtime RPC 入口
├── runtime_http.py             # HTTP / WebSocket Runtime 入口兼容包装器（指向 GensokyoAI/backends/web_server）
├── pyproject.toml              # 项目配置（UV / 打包脚本入口）
├── requirements.txt            # pip 依赖列表
├── run_default_uv.cmd          # Windows UV 快速启动脚本
├── run_default_pip.cmd         # Windows pip 快速启动脚本
└── README.md
```