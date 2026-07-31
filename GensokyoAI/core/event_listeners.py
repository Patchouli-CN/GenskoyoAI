"""事件监听器 - 响应系统事件"""

import asyncio
from typing import TYPE_CHECKING, Any

from ..utils.logger import logger
from .events import Event, EventBus, SystemEvent

if TYPE_CHECKING:
    from .agent import Agent


class CoreListeners:
    """核心事件监听器"""

    def __init__(self, agent: Agent, event_bus: EventBus):
        self.agent = agent
        self.event_bus = event_bus
        self._register()

    def _register(self) -> None:
        bus = self.event_bus

        # 会话事件
        bus.subscribe(SystemEvent.SESSION_CREATED, self.on_session_created)
        bus.subscribe(SystemEvent.SESSION_RESUMED, self.on_session_resumed)

        # 思考事件
        bus.subscribe(SystemEvent.THINK_ENGINE_THOUGHT, self.on_thought_record)

        # 对话事件
        bus.subscribe(SystemEvent.MESSAGE_RECEIVED, self.on_message_received)
        bus.subscribe(SystemEvent.MESSAGE_SENT, self.on_message_sent)

        # 记忆事件
        bus.subscribe(SystemEvent.MEMORY_EPISODIC_COMPRESSED, self.on_episodic_compressed)

        # 工具事件
        bus.subscribe(SystemEvent.TOOL_CALL_STARTED, self.on_tool_call_started)
        bus.subscribe(SystemEvent.TOOL_CALL_COMPLETED, self.on_tool_call_completed)

        # 持久化事件
        bus.subscribe(SystemEvent.PERSISTENCE_SAVE_COMPLETED, self.on_persistence_saved)

        # 错误事件
        bus.subscribe(SystemEvent.ERROR_OCCURRED, self.on_error)

        logger.debug("核心监听器已注册")

    # ==================== 会话事件 ====================

    async def on_session_created(self, event: Event) -> None:
        session = event.data.get("session")
        if session:
            logger.info(f"会话已创建: {session.session_id[:8]}...")

    async def on_session_resumed(self, event: Event) -> None:
        session = event.data.get("session")
        if session:
            logger.info(f"会话已恢复: {session.session_id[:8]}...")

    # ==================== 思考事件 ====================
    async def on_thought_record(self, event: Event) -> None:
        """当角色静默思考时，把思考内容存入语义/情景记忆"""
        thought = event.data.get("thought", "")
        if not thought:
            return

        # 1. 存入语义记忆 (TopicAwareStore)
        # 让角色的长期记忆里，有这段“内心戏”
        await self.agent.semantic_memory.add_async(
            content=f"【内心思考】{thought}",
            importance=0.6,  # 思考的重要性中等偏高
            emotional_valence=event.data.get("emotional_valence", 0.0),
        )

        # 2. (可选) 也可以记录到情景记忆，作为一段“心理事件”
        # await self.agent.episodic_memory.add_message(...)

        logger.debug(f"🧠 已记录静默思考到语义记忆: {thought[:30]}...")

    # ==================== 对话事件 ====================

    async def on_message_received(self, event: Event) -> None:
        """记录用户消息到工作记忆"""
        user_input = event.data.get("content", "")
        if isinstance(user_input, list):
            preview = f"[多模态消息，{len(user_input)} 个 parts]"
        else:
            preview = str(user_input)[:50]
        logger.debug(f"收到消息: {preview}...")

        extra: dict[str, Any] = {}
        if event.data.get("prompt_injection_suspected"):
            extra["prompt_injection_suspected"] = True
            extra["prompt_injection_report"] = event.data.get("prompt_injection_report")

        # World 回合：共享触发文本属于舞台，不写入 Actor 私有工作记忆，
        # 避免同一段舞台上下文被复制进角色私历造成串台；
        # Actor 自己生成的回复仍由 on_message_sent 正常写入。
        if event.data.get("record_in_working_memory") is False:
            logger.debug("World 回合触发文本跳过写入私有工作记忆")
            return

        self.agent.working_memory.add_message("user", user_input, **extra)

    async def on_message_sent(self, event: Event) -> None:
        """记录助手消息到工作记忆 - 统一入口"""
        response = event.data.get("content", "")
        reasoning_content = event.data.get("reasoning_content")

        self.agent.working_memory.add_message(
            "assistant",
            response,
            reasoning_content=reasoning_content,
        )
        if event.source == "initiative_timer" or event.data.get("initiative"):
            logger.info(f"🤖 记录主动发送的助手消息: {response[:60]}...")
        else:
            logger.debug(f"记录并发送响应: {response[:60]}...")

    # ==================== 记忆事件 ====================

    async def on_episodic_compressed(self, event: Event) -> None:
        episode = event.data.get("episode")
        if episode:
            logger.info(f"情景记忆已压缩: {episode.summary[:50]}...")

    # ==================== 工具事件 ====================

    async def on_tool_call_started(self, event: Event) -> None:
        tool_name = event.data.get("name")
        logger.debug(f"工具调用开始: {tool_name}")

    async def on_tool_call_completed(self, event: Event) -> None:
        tool_name = event.data.get("name")
        result = event.data.get("result", "")
        logger.debug(f"工具调用完成: {tool_name} -> {result[:50] if result else ''}...")

    # ==================== 持久化事件 ====================

    async def on_persistence_saved(self, event: Event) -> None:
        session_id = event.data.get("session_id", "")
        logger.debug(f"会话已持久化: {session_id[:8] if session_id else ''}...")

    # ==================== 错误事件 ====================

    async def on_error(self, event: Event) -> None:
        error = event.data.get("error")
        original_event = event.data.get("original_event", {})
        logger.error(f"事件处理错误 [{original_event.get('type', 'unknown')}]: {error}")


class MemoryServiceListeners:
    """记忆服务监听器 - 响应工具的记忆请求"""

    def __init__(self, agent: Agent, event_bus: EventBus):
        self.agent = agent
        self.event_bus = event_bus
        self._register()

    def _register(self) -> None:
        self.event_bus.subscribe(
            SystemEvent.MEMORY_SEMANTIC_ADDED,
            self.on_memory_add_request,
        )
        self.event_bus.subscribe(
            SystemEvent.MEMORY_SEMANTIC_RECALLED,
            self.on_memory_recall_request,
        )
        self.event_bus.subscribe(
            SystemEvent.MEMORY_SEMANTIC_UPDATED,
            self.on_memory_update_request,
        )

    async def on_memory_add_request(self, event: Event) -> None:
        """处理记忆添加请求"""
        if not event.source.startswith("tool."):
            return

        data = event.data
        content = data.get("content", "")

        if not content:
            self.event_bus.respond(event, None)
            return

        # 立即响应，不等待存储完成，同时返回审核/追踪元数据
        self.event_bus.respond(
            event,
            {
                "status": "processing",
                "topic_name": data.get("topic_name", "记忆"),
                "importance": data.get("importance", 0.5),
                "source": event.source,
                "requires_review": bool(data.get("requires_review", False)),
                "metadata": dict(data.get("metadata", {}) or {}),
            },
        )

        # 后台执行存储
        asyncio.create_task(self._do_memory_add(event))

    async def _do_memory_add(self, event: Event) -> None:
        """后台执行记忆存储"""
        data = event.data
        content = data.get("content", "")
        importance = data.get("importance", 0.5)
        topic_name = data.get("topic_name")
        emotional_valence = data.get("emotional_valence", 0.0)

        try:
            if hasattr(self.agent, "semantic_memory"):
                topic = await self.agent.semantic_memory.add_async(
                    content=content,
                    importance=importance,
                    emotional_valence=emotional_valence,
                    topic_name=topic_name,
                )
                if topic:
                    memory_id = topic.message_ids[-1] if topic.message_ids else None
                    logger.debug(
                        f"记忆服务: 已存储 -> 话题「{topic.name}」(情感: {topic.emotional_valence:.2f}, 记忆: {memory_id})"
                    )
        except Exception as e:
            logger.error(f"记忆服务: 存储失败 - {e}")

    async def on_memory_recall_request(self, event: Event) -> None:
        """处理记忆检索请求"""
        if not event.source.startswith("tool."):
            return

        data = event.data
        keyword = data.get("keyword", "")
        category = data.get("category")
        page = data.get("page", 1)

        if not keyword:
            self.event_bus.respond(event, None)
            return

        try:
            if hasattr(self.agent, "semantic_memory"):
                store = self.agent.semantic_memory.store
                results = store.search(query_text=keyword, top_k=20)

                if category:
                    results = [r for r in results if category in r.get("tags", [])]

                if not results:
                    self.event_bus.respond(event, f"「关于 '{keyword}' …我好像没什么印象…」")
                    return

                page_size = 5
                total = len(results)
                total_pages = (total + page_size - 1) // page_size
                page = max(1, min(page, total_pages))
                start = (page - 1) * page_size
                page_data = results[start : start + page_size]

                lines = [f"「关于 '{keyword}' 的记忆：」"]
                for i, item in enumerate(page_data, start=start + 1):
                    content = item.get("content", "")
                    tags = item.get("tags", [])
                    tag_str = f"[{','.join(tags)}]" if tags else ""
                    lines.append(f"  {i}. {tag_str} {content}")

                if total_pages > 1:
                    lines.append(f"  --- 第 {page}/{total_pages} 页 ---")

                self.event_bus.respond(event, "\n".join(lines))
                return

        except Exception as e:
            logger.error(f"记忆服务: 检索失败 - {e}")

        self.event_bus.respond(event, None)

    async def on_memory_update_request(self, event: Event) -> None:
        """处理记忆更新请求"""
        if not event.source.startswith("tool."):
            return

        data = event.data
        topic_name = data.get("topic_name", "")
        new_content = data.get("new_content", "")
        reason = data.get("reason", "信息更新")

        if not topic_name or not new_content:
            self.event_bus.respond(event, None)
            return

        try:
            if hasattr(self.agent, "semantic_memory"):
                store = self.agent.semantic_memory.store
                topic = await store.update_topic_memory(topic_name, new_content)

                if topic:
                    memory_id = topic.message_ids[-1] if topic.message_ids else None
                    logger.info(f"记忆服务: 已更新话题「{topic_name}」，原因: {reason}")
                    self.event_bus.respond(
                        event,
                        {
                            "topic_name": topic.name,
                            "topic_id": topic.id,
                            "memory_id": memory_id,
                            "importance": data.get("importance", 0.7),
                            "reason": reason,
                            "source": event.source,
                            "requires_review": bool(data.get("requires_review", False)),
                            "metadata": dict(data.get("metadata", {}) or {}),
                            "updated": True,
                        },
                    )
                    return

        except Exception as e:
            logger.error(f"记忆服务: 更新失败 - {e}")

        self.event_bus.respond(event, None)


class SceneServiceListeners:
    """场景服务监听器 - 响应场景工具请求，并在会话事件时同步当前场景。"""

    def __init__(self, agent: Agent, event_bus: EventBus):
        self.agent = agent
        self.event_bus = event_bus
        self._register()

    def _register(self) -> None:
        bus = self.event_bus
        bus.subscribe(SystemEvent.SCENE_SWITCH_REQUESTED, self.on_scene_switch_request)
        bus.subscribe(SystemEvent.SCENE_QUERY_CURRENT, self.on_scene_query_current)
        bus.subscribe(SystemEvent.SESSION_CREATED, self.on_session_scene_sync)
        bus.subscribe(SystemEvent.SESSION_RESUMED, self.on_session_scene_sync)
        logger.debug("场景服务监听器已注册")

    def _scene_manager(self):
        manager = getattr(self.agent, "scene_manager", None)
        if manager is None or not getattr(manager, "enabled", False):
            return None
        return manager

    _SCENE_REQUEST_SOURCES = ("tool.", "runtime.")

    def _is_scene_request(self, event: Event) -> bool:
        """场景请求来源白名单：模型工具（tool.*）或 Runtime RPC（runtime.*）。"""
        return event.source.startswith(self._SCENE_REQUEST_SOURCES)

    async def on_scene_switch_request(self, event: Event) -> None:
        """处理场景切换请求：切换 + 持久化到会话 + 广播 SCENE_SWITCHED。"""
        if not self._is_scene_request(event):
            return

        manager = self._scene_manager()
        if manager is None:
            self.event_bus.respond(event, {"ok": False, "error": "场景功能未启用"})
            return

        scene_id = (event.data or {}).get("scene_id", "")
        from_scene_id = manager.current_scene_id
        try:
            scene = await manager.switch_scene(scene_id)
        except Exception as e:
            self.event_bus.respond(event, {"ok": False, "error": str(e)})
            return

        self._persist_current_scene(scene.id)
        self.event_bus.respond(event, {"ok": True, "scene_id": scene.id, "name": scene.name})

        # 广播场景切换，供前端提示当前场景；from_scene_id/actor_id 供
        # World 更新 WorldStage（谁在移动、从哪移动到哪）。
        self.event_bus.publish(
            Event(
                type=SystemEvent.SCENE_SWITCHED,
                source="scene_service",
                data={
                    "scene_id": scene.id,
                    "from_scene_id": from_scene_id,
                    "actor_id": getattr(self.agent, "actor_id", None),
                    "name": scene.name,
                    "description": scene.description,
                    "render": scene.render(),
                },
            )
        )

    async def on_scene_query_current(self, event: Event) -> None:
        """处理当前场景查询。"""
        if not self._is_scene_request(event):
            return

        manager = self._scene_manager()
        if manager is None:
            self.event_bus.respond(event, None)
            return

        try:
            scene = await manager.get_current_scene()
        except Exception as e:
            logger.error(f"场景服务: 查询当前场景失败 - {e}")
            self.event_bus.respond(event, None)
            return

        if scene is None:
            self.event_bus.respond(event, None)
            return

        description = await manager.render_scene_with_options(scene)
        self.event_bus.respond(
            event,
            {"scene_id": scene.id, "name": scene.name, "description": description},
        )

    async def on_session_scene_sync(self, event: Event) -> None:
        """会话创建/恢复时，从会话 metadata 恢复当前场景（或落地默认场景）。"""
        manager = self._scene_manager()
        if manager is None:
            return

        session = (event.data or {}).get("session")
        session_scene_id = None
        if session is not None:
            session_scene_id = session.metadata.get("current_scene_id")

        try:
            resolved = await manager.resolve_initial_scene(session_scene_id)
        except Exception as e:
            logger.error(f"场景服务: 恢复初始场景失败 - {e}")
            return

        manager.reset_for_session(resolved)
        # 若解析出的场景与会话记录不一致（例如首次落地默认场景），写回会话
        if resolved and resolved != session_scene_id:
            self._persist_current_scene(resolved)

    def _persist_current_scene(self, scene_id: str) -> None:
        """把当前场景 id 写入当前会话 metadata 并落盘。"""
        try:
            session = self.agent.session_manager.get_current_session()
            if session is None:
                return
            session.metadata["current_scene_id"] = scene_id
            self.agent.session_manager.persistence.save_session(session)
        except Exception as e:
            logger.error(f"场景服务: 持久化当前场景失败 - {e}")


class ErrorListeners:
    """错误事件监听器"""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._error_counts: dict[str, int] = {}
        self._last_errors: list[dict] = []
        self._register()

    def _register(self) -> None:
        self.event_bus.subscribe(SystemEvent.MODEL_ERROR, self._async_update_model_error)
        self.event_bus.subscribe(SystemEvent.TOOL_ERROR, self._async_update_tool_error)
        self.event_bus.subscribe(SystemEvent.ERROR_OCCURRED, self._async_update_general_error)

    async def _async_update_model_error(self, event: Event) -> None:
        data = event.data
        context = data.get("context", "unknown")
        key = f"model:{context}"
        self._error_counts[key] = self._error_counts.get(key, 0) + 1

        error_entry = {
            "timestamp": event.timestamp,
            "type": "model_error",
            "model": data.get("model", "unknown"),
            "context": context,
            "error": data.get("error", "未知错误"),
            "status_code": data.get("status_code"),
        }
        self._last_errors.append(error_entry)
        if len(self._last_errors) > 10:
            self._last_errors.pop(0)

        status_code = data.get("status_code")
        model = data.get("model", "unknown")
        error_msg = data.get("error", "未知错误")

        if status_code == "502":
            logger.warning(
                f"🔌 [ErrorListener] 502 连接错误 (模型: {model}, 上下文: {context})\n"
                f"   建议检查: 1) Ollama 是否运行 2) 代理设置 3) 防火墙"
            )
        else:
            logger.error(f"🤖 [ErrorListener] 模型错误: {model} - {error_msg}")

    async def _async_update_tool_error(self, event: Event) -> None:
        data = event.data
        tool_name = data.get("tool_name", "unknown")
        key = f"tool:{tool_name}"
        self._error_counts[key] = self._error_counts.get(key, 0) + 1

        error_entry = {
            "timestamp": event.timestamp,
            "type": "tool_error",
            "tool": tool_name,
            "error": data.get("error", "未知错误"),
        }
        self._last_errors.append(error_entry)
        if len(self._last_errors) > 10:
            self._last_errors.pop(0)

        logger.error(f"🔧 [ErrorListener] 工具错误: {tool_name} - {data.get('error', '未知错误')}")

    async def _async_update_general_error(self, event: Event) -> None:
        key = "general"
        self._error_counts[key] = self._error_counts.get(key, 0) + 1

        error_entry = {
            "timestamp": event.timestamp,
            "type": "general_error",
            "error": event.data.get("error", "未知错误"),
        }
        self._last_errors.append(error_entry)
        if len(self._last_errors) > 10:
            self._last_errors.pop(0)

        data = event.data
        error_msg = data.get("error", "未知错误")
        original_event = data.get("original_event", {})
        logger.error(
            f"❌ [ErrorListener] 通用错误: {error_msg}\n"
            f"   原始事件: {original_event.get('type', 'unknown')}"
        )

    def get_error_stats(self) -> dict:
        return {
            "counts": self._error_counts.copy(),
            "recent": self._last_errors.copy(),
            "total": sum(self._error_counts.values()),
        }


class PersistenceListeners:
    """持久化监听器 - 响应需要保存的事件"""

    def __init__(self, agent: Agent, event_bus: EventBus):
        self.agent = agent
        self.event_bus = event_bus
        self._session = self.agent.session_manager.get_current_session()
        self._register()

    def _register(self) -> None:
        # 订阅消息发送事件，触发保存
        self.event_bus.subscribe(SystemEvent.MESSAGE_SENT, self._on_message_sent_for_persistence)

        # 订阅主动消息事件
        self.event_bus.subscribe(
            SystemEvent.THINK_ENGINE_INITIATIVE, self._on_initiative_for_persistence
        )

        # 订阅工具调用完成事件（可选）
        self.event_bus.subscribe(
            SystemEvent.TOOL_CALL_COMPLETED, self._on_tool_completed_for_persistence
        )

        logger.debug("💾 [PersistenceListeners] 持久化监听器已注册")

    async def _on_message_sent_for_persistence(self, event: Event) -> None:
        """消息发送后触发保存"""
        await self._trigger_save(event, "message_sent")

    async def _on_initiative_for_persistence(self, event: Event) -> None:
        """主动消息后触发保存"""
        await self._trigger_save(event, "initiative")

    async def _on_tool_completed_for_persistence(self, event: Event) -> None:
        """工具调用完成后触发保存"""
        await self._trigger_save(event, "tool_completed")

    async def _trigger_save(self, event: Event, source: str) -> None:
        """触发保存操作"""
        if not hasattr(self.agent, "save_coordinator"):
            return

        # 发布保存开始事件
        self.event_bus.publish(
            Event(
                type=SystemEvent.PERSISTENCE_SAVE_STARTED,
                source=f"persistence_listeners.{source}",
                data={"trigger_event_id": event.id},
            )
        )

        try:
            # 执行保存
            success = await self.agent.save_coordinator.save_async(
                self.agent.working_memory,
                force=False,  # 让 save_coordinator 自己判断
            )

            # 发布保存完成事件
            self.event_bus.publish(
                Event(
                    type=SystemEvent.PERSISTENCE_SAVE_COMPLETED
                    if success
                    else SystemEvent.PERSISTENCE_SAVE_FAILED,
                    source=f"persistence_listeners.{source}",
                    data={
                        "trigger_event_id": event.id,
                        "success": success,
                        "session_id": self._session.session_id if self._session else None,
                    },
                )
            )

        except Exception as e:
            logger.error(f"保存失败: {e}")
            self.event_bus.publish(
                Event(
                    type=SystemEvent.PERSISTENCE_SAVE_FAILED,
                    source=f"persistence_listeners.{source}",
                    data={"error": str(e)},
                )
            )
