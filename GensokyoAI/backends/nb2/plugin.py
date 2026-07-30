"""NoneBot2 插件：QQ 群/私聊 ↔ GensokyoAI Runtime 桥接。

触发规则：群聊 @bot / 回复 bot（to_me），私聊全部响应。
适配器与 Runtime 同进程：经 RuntimeHost 进程内驱动 RuntimeService 多租户路径，
每个 QQ 群、每个私聊用户映射为独立租户（agent_id），会话、记忆与资源闸彼此隔离；
主动消息经事件订阅队列进程内实时投递。依赖可选组件（pip extra: nb2），
仅在 `python -m GensokyoAI.backends.nb2` 启动时加载。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nonebot import get_bots, get_driver, on_message
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.matcher import Matcher
from nonebot.rule import Rule, to_me

from ...utils.helpers import split_reply_segments
from ...utils.logger import logger
from .config import Nb2Config
from .runtime_host import RuntimeHost, RuntimeRpcError
from .store import SessionStore

_config = Nb2Config.from_env()
_host = RuntimeHost(root_dir=_config.root_dir)
_store = SessionStore(_config.data_dir / "sessions.json")
_locks: dict[str, asyncio.Lock] = {}
_initialized: set[str] = set()  # 本进程内已成功 ensure_agent 的 agent_id
_targets: dict[str, tuple[str, int]] = {}  # agent_id -> ("group" | "user", QQ号)

# 随每条回复注入的附加要求（GSK_NB2_EXTRA_PROMPT），只影响当轮回复、不写入会话
_EXTRA_CONTEXTS = [f"【QQ 聊天场景附加要求】\n{_config.extra_prompt}"] if _config.extra_prompt else []

_REPLY_SEGMENT_DELAY_SECONDS = 0.8  # 分段发送间隔，避免消息刷屏抖动

SendCallable = Callable[[Message], Awaitable[None]]


async def _send_segmented(send: SendCallable, text: str) -> None:
    """分段发送：QQ 聊天习惯是多条短消息，而非一大段墙。"""
    segments = split_reply_segments(text) if _config.split_reply else [text]
    for index, segment in enumerate(segments):
        if index:
            await asyncio.sleep(_REPLY_SEGMENT_DELAY_SECONDS)
        await send(Message(MessageSegment.text(segment)))  # text 段转义 CQ 码


async def _deliver_initiative(agent_id: str, payload: dict[str, Any]) -> None:
    """Runtime 主动消息事件 → QQ 投递（普通回复已由 RPC 响应投递，这里只发主动的）。"""
    if payload.get("type") != "message.sent":
        return
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("initiative"):
        return
    content = str(data.get("content") or "").strip()
    target = _targets.get(agent_id)
    if not content or target is None:
        return
    bots = get_bots()
    if not bots:
        logger.warning(f"[nb2] 协议端未连接，{agent_id} 的主动消息暂缓投递")
        return
    bot = next(iter(bots.values()))
    kind, target_id = target

    async def _send(message: Message) -> None:
        if kind == "group":
            await bot.send_group_msg(group_id=target_id, message=message)
        else:
            await bot.send_private_msg(user_id=target_id, message=message)

    try:
        await _send_segmented(_send, content)
        logger.info(f"[nb2] 主动消息已投递到 {kind}:{target_id}（{len(content)} 字）")
    except Exception:
        logger.exception(f"[nb2] 主动消息投递失败（{agent_id}）")


_driver = get_driver()


@_driver.on_startup
async def _on_startup() -> None:
    logger.info(
        f"[nb2] 适配器已加载: 角色={_config.character}, "
        f"主动发言={'开' if _config.initiative else '关'}, "
        f"群白名单={sorted(_config.group_whitelist) or '不限'}, "
        f"分段回复={'开' if _config.split_reply else '关'}, "
        f"附加要求={_config.extra_prompt[:30] or '无'}, root={_config.root_dir or 'cwd'}"
    )


@_driver.on_shutdown
async def _on_shutdown() -> None:
    await _host.close()


def _is_private_message(event: MessageEvent) -> bool:
    return isinstance(event, PrivateMessageEvent)


group_chat = on_message(rule=to_me(), priority=90, block=True)
private_chat = on_message(rule=Rule(_is_private_message), priority=90, block=True)


@group_chat.handle()
async def _handle_group(event: GroupMessageEvent) -> None:
    if _config.group_whitelist and event.group_id not in _config.group_whitelist:
        return
    agent_id = f"qq-group-{event.group_id}"
    _targets[agent_id] = ("group", event.group_id)
    await _chat(event, group_chat, key=f"group:{event.group_id}", agent_id=agent_id)


@private_chat.handle()
async def _handle_private(event: PrivateMessageEvent) -> None:
    agent_id = f"qq-user-{event.user_id}"
    _targets[agent_id] = ("user", event.user_id)
    await _chat(event, private_chat, key=f"user:{event.user_id}", agent_id=agent_id)


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


async def _ensure_agent(agent_id: str, entry: dict[str, Any] | None) -> tuple[str, int]:
    stored_session = str(entry["session_id"]) if entry and entry.get("session_id") else None
    try:
        session_id, revision = await _host.ensure_agent(
            agent_id,
            _config.character,
            stored_session,
            disable_initiative=not _config.initiative,
        )
    except RuntimeRpcError:
        if stored_session is None:
            raise
        # Runtime 侧该会话已被删除：退化为恢复最新会话 / 新建会话
        logger.warning(f"[nb2] 会话 {stored_session} 恢复失败，改为恢复最新会话")
        session_id, revision = await _host.ensure_agent(
            agent_id, _config.character, None, disable_initiative=not _config.initiative
        )
    if _config.initiative:
        try:
            await _host.subscribe_events(agent_id, _deliver_initiative)
        except Exception as error:
            # 订阅失败只影响主动投递，不阻塞正常问答
            logger.warning(f"[nb2] 订阅主动消息事件失败（{agent_id}），主动投递暂不可用: {error}")
    _initialized.add(agent_id)
    return session_id, revision


async def _chat(event: MessageEvent, matcher: type[Matcher], *, key: str, agent_id: str) -> None:
    text = event.get_message().extract_plain_text().strip()
    if not text or text.startswith("/"):
        return  # 忽略纯表情/图片消息与保留的 "/" 命令前缀
    reply = ""
    async with _lock_for(key):
        try:
            entry = _store.get(key)
            if entry is None or agent_id not in _initialized:
                session_id, revision = await _ensure_agent(agent_id, entry)
                _store.put(key, agent_id=agent_id, session_id=session_id, revision=revision)
            else:
                session_id = str(entry["session_id"])
                revision = int(entry["revision"])
            idempotency_key = f"nb2:{event.self_id}:{event.message_id}"
            try:
                reply, new_revision = await _host.send_message(
                    agent_id,
                    session_id,
                    revision,
                    text,
                    idempotency_key=idempotency_key,
                    system_contexts=_EXTRA_CONTEXTS,
                )
            except RuntimeRpcError as error:
                if error.code == "resource.limit_exceeded":
                    await matcher.send(MessageSegment.text("幻想乡现在有点忙，稍后再叫我吧。"))
                    return
                # 租户丢失 / 会话失效：重建租户后同键重试一次（幂等安全）
                logger.warning(f"[nb2] 发送失败 [{error.code}]，重建租户会话后重试: {error}")
                session_id, revision = await _ensure_agent(agent_id, None)
                _store.put(key, agent_id=agent_id, session_id=session_id, revision=revision)
                reply, new_revision = await _host.send_message(
                    agent_id,
                    session_id,
                    revision,
                    text,
                    idempotency_key=idempotency_key,
                    system_contexts=_EXTRA_CONTEXTS,
                )
            _store.update_revision(key, new_revision)
        except RuntimeRpcError as error:
            logger.error(f"[nb2] Runtime 调用失败 [{error.code}]: {error}")
            await matcher.send(MessageSegment.text("呜……出了点问题，请稍后再试。"))
            return
        except Exception:
            logger.exception("[nb2] 处理消息时出现未预期错误")
            await matcher.send(MessageSegment.text("呜……出了点问题，请稍后再试。"))
            return
    if not reply.strip():
        logger.warning(f"[nb2] {agent_id} 返回了空回复")
        reply = "……（好像一下子不知道说什么了，再说一次试试？）"
    await _send_segmented(matcher.send, reply)
