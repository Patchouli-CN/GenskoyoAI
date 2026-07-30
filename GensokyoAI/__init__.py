"""
GensokyoAI - 幻想乡 AI 角色扮演引擎
=====================================

一个专为角色扮演设计的异步 AI 对话框架，提供完整的三层记忆系统、
自主话题聚类、情感标记、静默思考引擎和可扩展的后端支持。

核心特性
--------

🎭 **角色扮演优化**
    - 支持 YAML 格式的角色配置文件
    - 系统提示词模板和示例对话
    - 角色一致性维护

🧠 **三层记忆系统**
    - 工作记忆 (Working Memory)：当前会话的滑动窗口上下文
    - 情景记忆 (Episodic Memory)：历史对话的自动压缩摘要
    - 语义记忆 (Semantic Memory)：基于 LLM 自主话题聚类的长期知识存储
      *零 Embedding 显存占用，由角色自主判断记忆归属*

💡 **静默思考引擎** (ThinkEngine)
    - 模拟人脑默认模式网络，在对话间隙进行记忆游走
    - 基于情感标记的话题联想
    - 可产生主动意图，让角色"有自己的想法"

❤️ **情感标记与遗忘曲线**
    - 记忆带有情感效价 (emotional_valence)，强烈情感更难遗忘
    - 基于艾宾浩斯遗忘曲线的时间衰减机制
    - 提取练习效应：每次回忆都会刷新并强化记忆

💬 **会话管理**
    - 会话创建、保存、恢复和列表
    - 自动持久化，支持异步 I/O
    - 会话回滚和切换

🔧 **工具调用** (Function Calling)
    - 内置工具：时间、月相、系统信息、记忆管理
    - 装饰器式工具注册
    - 并行工具执行

🎛️ **智能命令系统**
    - 标签模式：<know>、<meta>、<attention> 等
    - 前缀模式：/help、/save、/new 等
    - 提示词上下文累积

⚡ **事件驱动架构**
    - 完全解耦的发布/订阅事件总线
    - 全异步设计，基于 asyncio
    - 后台任务队列处理记忆和持久化
    - 流式和非流式响应支持

🔌 **可扩展后端**
    - 抽象后端基类 BaseBackend
    - 内置 Rich 美化的控制台后端
    - 可扩展为 WebUI、QQ 机器人等


快速开始
--------

1. 安装依赖（推荐 uv；也可 pip 安装可选 Provider extra）：
   uv sync --extra ollama          # 或 pip install "GensokyoAI[ollama]"

2. 确保 Ollama 运行并下载模型：
   ollama pull qwen3.5:9b

3. 创建角色配置文件 (characters/my_role.yaml)：
   name: "角色名"
   system_prompt: "你的角色设定..."
   greeting: "开场白"

4. 启动对话：
   python -m GensokyoAI.cli.main --character characters/my_role.yaml --new-session


命令行参数
----------
--character, -c    角色名称或配置文件路径
--config           配置文件路径 (默认: config/local.yaml，首次启动自动从模板生成)
--new-session      创建新会话
--resume SESSION   恢复指定会话
--list-sessions    列出所有历史会话
--no-stream        禁用流式输出


环境变量
--------
GENSOKYOAI_MODEL                覆盖模型名称
GENSOKYOAI_LOG_LEVEL            日志级别 (DEBUG/INFO/WARNING/ERROR)
GENSOKYOAI_LOG_CONSOLE          控制台日志开关 (true/false)
GENSOKYOAI_MEMORY_WORKING_TURNS 工作记忆最大轮数


示例
----
>>> from GensokyoAI import Agent, ConsoleBackendBuilder
>>>
>>> # 创建 Agent
>>> agent = Agent(character_file="characters/yuyuko.yaml")
>>>
>>> # 创建控制台后端并运行
>>> backend = ConsoleBackendBuilder(agent).build()
>>> await backend.run_interactive()


架构亮点
--------
1. LLM 自主话题聚类
   让正在角色扮演的 LLM 自己判断记忆归属哪个话题，替代传统的
   Embedding + 向量检索方案。零额外显存占用，语义理解更准确。

2. 静默思考引擎
   在对话间隙，随机游走话题图谱产生联想。可以产生主动意图，
   让角色从"被动响应"升级为"主动思考"。

3. 情感标记与遗忘
   记忆带有情感效价，强烈情感的记忆更难遗忘；未被提取的记忆
   随时间自然衰减，模拟人脑的遗忘曲线。

4. 事件驱动架构
   所有组件通过事件总线解耦，易于扩展。新增功能只需订阅相应
   事件，无需修改核心逻辑。


许可证
------
Apache License 2.0

作者
------
Patchouli-CN <3072252442@qq.com>

GitHub
------
https://github.com/Patchouli-CN/GensokyoAI

"""

from .core.version import package_version

# 版本号统一由 core.version.package_version() 解析（安装元数据优先，源码树回退 pyproject.toml），
# 与 Runtime 元数据同源，避免此处硬编码与 pyproject.toml 漂移。
__version__ = package_version()
__author__ = "Patchouli-CN"
__license__ = "Apache-2.0"
__email__ = "3072252442@qq.com"

# 导出主要类和函数
from .backends import BaseBackend, ConsoleBackend, ConsoleBackendBuilder
from .core.agent import Agent, StreamChunk
from .core.config import AppConfig, CharacterConfig, ConfigLoader
from .core.exceptions import (
    AgentError,
    ConfigError,
    GensokyoError,
    MemorySystemError,
    ModelError,
    SessionError,
    ToolError,
)

__all__ = [
    # 版本信息
    "__version__",
    "__author__",
    "__license__",
    # 核心类
    "Agent",
    "StreamChunk",
    "AppConfig",
    "ConfigLoader",
    "CharacterConfig",
    # 后端
    "BaseBackend",
    "ConsoleBackend",
    "ConsoleBackendBuilder",
    # 异常
    "GensokyoError",
    "AgentError",
    "ConfigError",
    "MemorySystemError",
    "ToolError",
    "SessionError",
    "ModelError",
]
