"""消息构建器 - 构建发送给模型的消息列表"""

# GensokyoAI/core/agent/message_builder.py

from typing import TYPE_CHECKING, Any

from ...tools.build_service import ToolBuildResult
from ...tools.registry import ToolRegistry

if TYPE_CHECKING:
    from ...memory.episodic import EpisodicMemoryManager
    from ...memory.semantic import SemanticMemoryManager
    from ...memory.working import WorkingMemoryManager
    from ..config import ModelConfig, WebSearchToolConfig


class MessageOperation:
    """消息流式操作器；每个操作方法返回新的实例，不修改原对象。"""

    def __init__(self, messages: list[dict]):
        self._messages = messages

    def exclude(self, **conditions) -> MessageOperation:
        """万能排除器"""

        def matches(msg):
            for key, value in conditions.items():
                if key == "has":
                    if value not in msg:
                        return False
                elif key == "has_not":
                    if value in msg:
                        return False
                else:
                    if msg.get(key) != value:
                        return False
            return True

        return MessageOperation([m for m in self._messages if not matches(m)])

    def filter(self, predicate) -> MessageOperation:
        return MessageOperation([m for m in self._messages if predicate(m)])

    def filter_role(self, *roles: str) -> MessageOperation:
        return MessageOperation([m for m in self._messages if m.get("role") in roles])

    def exclude_role(self, *roles: str) -> MessageOperation:
        return MessageOperation([m for m in self._messages if m.get("role") not in roles])

    def take(self, n: int) -> MessageOperation:
        return MessageOperation(self._messages[-n:])

    def get(self) -> list[dict]:
        return self._messages


class MessageBuilder:
    """
    消息构建器 - 构建发送给模型的消息列表

    职责：
    - 组装系统提示词
    - 添加情景记忆（历史摘要）
    - 添加语义记忆（相关知识）
    - 添加工作记忆（当前对话）
    - 构建工具调用后的继续对话消息
    """

    def __init__(
        self,
        system_prompt: str,
        working_memory: WorkingMemoryManager,
        episodic_memory: EpisodicMemoryManager,
        semantic_memory: SemanticMemoryManager,
        tool_registry: ToolRegistry | None = None,
        tool_enabled: bool = False,
        character_name: str = "",
        web_search_config: WebSearchToolConfig | None = None,
        model_config: ModelConfig | None = None,
        tool_build_result: ToolBuildResult | None = None,
        metadata: dict[str, Any] | None = None,
        example_dialogue: list[dict[str, str]] | None = None,
    ):
        """
        初始化消息构建器

        Args:
            system_prompt: 基础系统提示词
            working_memory: 工作记忆管理器
            episodic_memory: 情景记忆管理器
            semantic_memory: 语义记忆管理器
            tool_registry: 工具注册中心（用于生成工具说明）
            tool_enabled: 是否启用工具
            metadata: 角色元数据，将注入系统提示词
            example_dialogue: 角色示例对话，作为 few-shot 注入
        """
        self._system_prompt = system_prompt
        self._working_memory = working_memory
        self._episodic_memory = episodic_memory
        self._semantic_memory = semantic_memory
        self._tool_registry = tool_registry
        self._tool_enabled = tool_enabled
        self._character_name = character_name
        self._web_search_config = web_search_config
        self._model_config = model_config
        self._tool_build_result = tool_build_result
        self._metadata = metadata or {}
        self._example_dialogue = example_dialogue or []

    @property
    def system_prompt(self) -> str:
        """获取系统提示词（包含角色元数据和工具说明）"""
        prompt = self._system_prompt

        # 注入角色元数据
        if self._metadata:
            metadata_lines = ["【角色元数据】"]
            for key, value in self._metadata.items():
                metadata_lines.append(f"- {key}: {value}")
            prompt += "\n\n" + "\n".join(metadata_lines)

        # 添加 ToolBuildService 统一生成的工具说明；未接入时保持旧逻辑兼容。
        if self._tool_build_result is not None:
            if self._tool_build_result.instructions:
                prompt += "\n\n" + self._tool_build_result.instructions
        elif self._tool_enabled and self._tool_registry:
            if tools := self._tool_registry.list():
                tools_desc = "\n\n【可用工具】\n"
                tools_desc += "\n".join(f"- {t.name}: {t.description}" for t in tools)
                prompt += tools_desc
                prompt += (
                    "\n当需要获取外部信息时，请调用相应的工具。调用工具后，将结果整合到回复中。"
                )

            if self._provider_builtin_web_search_enabled():
                prompt += (
                    "\n\n【联网搜索策略】当前模型已启用 Provider 内置联网搜索；"
                    "遇到需要实时信息的问题时，优先依赖模型内置搜索能力。"
                )

        return prompt

    def build(
        self,
        user_input: str,
        system_contexts: list[str] | None = None,
        *,
        ephemeral_input: bool = False,
    ) -> list[dict[str, str]]:
        """
        构建完整消息列表

        Args:
            user_input: 用户输入
            system_contexts: 可选的系统上下文列表，每一项将作为独立的 system 消息插入
            ephemeral_input: 为 True 时在末尾补一条临时 user 消息（仅本次调用可见）。
                world-turn 触发文本默认不入私有工作记忆（舞台数据而非角色私历），
                但模型必须看到本轮触发；单角色路径为 False（输入已在工作记忆末尾）。

        Returns:
            消息列表，格式为 [{"role": "...", "content": "..."}]
        """
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt}]

        # 1. 注入来自用户界面的系统级上下文（如 <attention>、<know>、<meta>）
        if system_contexts:
            for ctx in system_contexts:
                messages.append({"role": "system", "content": ctx})

        # 注入角色示例对话（few-shot），帮助模型理解角色风格
        # 限制最多 3 条，避免挤占上下文
        if self._example_dialogue:
            messages.append({"role": "system", "content": "【角色对话风格示例】"})
            for item in self._example_dialogue[:3]:
                if isinstance(item, dict):
                    user_text = item.get("user", "")
                    assistant_text = item.get("assistant", "")
                    if user_text:
                        messages.append({"role": "user", "content": user_text})
                    if assistant_text:
                        messages.append({"role": "assistant", "content": assistant_text})

        # 2. 情景记忆（历史摘要）
        if episodic_context := self._episodic_memory.get_relevant_context(user_input):
            messages.append(
                {
                    "role": "system",
                    "content": "【历史记忆摘要】\n" + "\n".join(episodic_context),
                }
            )

        # 3. 语义记忆（相关记忆）
        if semantic_context := self._semantic_memory.get_relevant_context(user_input):
            messages.append(
                {
                    "role": "system",
                    "content": "【相关记忆】\n" + "\n".join(semantic_context),
                }
            )

        # 4. 联网搜索触发策略提示（只引导模型调用工具，不在后端自动联网）
        if hint := self._build_web_search_hint(user_input, system_contexts):
            messages.append({"role": "system", "content": hint})

        # 5. 工作记忆（当前对话）
        messages.extend(self._working_memory.get_context())

        # 6. world-turn 临时触发：不入私历，但必须让模型看到本轮触发
        if ephemeral_input and user_input:
            messages.append({"role": "user", "content": user_input})

        return messages

    def _provider_builtin_web_search_enabled(self) -> bool:
        """当前模型是否配置为优先使用 Provider 内置搜索。"""
        if not self._model_config:
            return False
        return bool(
            self._model_config.web_search_enabled
            and self._model_config.web_search_strategy != "off"
        )

    def _has_web_search_tool(self) -> bool:
        if not self._tool_enabled or not self._tool_registry:
            return False
        return self._tool_registry.get("web_search") is not None

    def _build_web_search_hint(
        self,
        user_input: str,
        system_contexts: list[str] | None = None,
    ) -> str:
        """按工具配置、用户输入和场景上下文生成联网搜索引导。"""
        config = self._web_search_config
        if not config or not config.enabled or config.trigger_strategy == "off":
            return ""
        if self._provider_builtin_web_search_enabled() or not self._has_web_search_tool():
            return ""

        strategy = config.trigger_strategy
        reasons: list[str] = []
        text = user_input or ""
        lower_text = text.lower()
        matched_keyword = next(
            (
                keyword
                for keyword in config.freshness_keywords
                if keyword and keyword.lower() in lower_text
            ),
            "",
        )
        if matched_keyword:
            reasons.append(f"问题包含时效性信号“{matched_keyword}”")

        if self._character_name and self._character_name in config.prefer_for_characters:
            reasons.append(f"角色策略要求 {self._character_name} 偏好联网核验")

        combined_context = "\n".join(system_contexts or [])
        matched_scenario = next(
            (
                scenario
                for scenario in config.prefer_for_scenarios
                if scenario and scenario.lower() in combined_context.lower()
            ),
            "",
        )
        if matched_scenario:
            reasons.append(f"场景策略命中“{matched_scenario}”")

        if strategy == "explicit" and not reasons:
            return ""
        if strategy == "auto" and not reasons:
            reasons.append("自动策略允许在需要外部实时信息时主动搜索")

        reason_text = "；".join(reasons)
        return (
            "【联网搜索策略】如果回答用户问题需要最新、当前、新闻、价格、版本、事实核验等外部实时信息，"
            f"请优先调用 web_search 工具后再回答。触发依据：{reason_text}。"
        )

    def build_continuation(
        self,
        system_contexts: list[str] | None = None,
        *,
        ephemeral_input: str = "",
    ) -> list[dict[str, str]]:
        """
        构建工具调用后的继续对话消息

        用于模型调用工具后，带着工具结果继续生成回复

        Args:
            system_contexts: 本轮系统上下文（World 舞台/在场角色/共享剧本等）；
                提供时在工具调用后继续保留，避免 Actor 调完工具丢失舞台。
                单角色路径不传，行为与旧版一致。
            ephemeral_input: world-turn 本轮触发文本；非空时以临时 user 消息
                插入工作记忆之前，让模型在 continuation 中仍知道自己在回应什么。

        Returns:
            消息列表
        """
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt}]
        if system_contexts:
            for ctx in system_contexts:
                messages.append({"role": "system", "content": ctx})
        # world-turn 临时触发（单角色路径为空串，行为不变）
        if ephemeral_input:
            messages.append({"role": "user", "content": ephemeral_input})
        # 保留工作记忆中的 provider 私有协议字段，例如 DeepSeek thinking mode
        # 所需的 reasoning_content；展示层是否输出由 debug_silent_output 控制。
        messages.extend(self._working_memory.get_context())
        messages.append(
            {
                "role": "system",
                "content": "工具调用已完成，请自然地将结果信息融入你的回复中，保持角色风格。",
            }
        )
        return messages

    def update_tool_build_result(self, tool_build_result: ToolBuildResult | None) -> None:
        """更新工具构建结果。"""
        self._tool_build_result = tool_build_result

    def update_system_prompt(self, system_prompt: str) -> None:
        """
        更新系统提示词（例如切换角色时）

        Args:
            system_prompt: 新的系统提示词
        """
        self._system_prompt = system_prompt

    @staticmethod
    def operate_on(messages: list[dict]) -> MessageOperation:
        """创建一个消息操作链"""
        return MessageOperation(messages)
