"""NoneBot2 插件：QQ 群/私聊 ↔ GensokyoAI Runtime 桥接。

触发规则：群聊 @bot / 回复 bot（to_me），私聊全部响应。
RuntimeHost 由 Nonebot2Adapter.start() 注入（bind_host），生命周期归组装入口
`run_adapters` 管；每个 QQ 群、每个私聊用户映射为独立租户（agent_id），会话、
记忆与资源闸彼此隔离；主动消息经事件订阅队列进程内实时投递；
群友印象存 known_members.json fake db。
依赖可选组件（pip extra: nb2），仅在 Nonebot2Adapter 启动时加载。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nonebot import get_bots, get_driver, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.matcher import Matcher
from nonebot.rule import Rule, to_me

from ...core.agent.prompts import build_member_impression_prompt
from ...runtime.host import RuntimeHost, RuntimeRpcError
from ...utils.helpers import sanitize_display_name, split_reply_segments, strip_rp_style
from ...utils.logger import logger
from .config import Nb2Config
from .store import MemberStore, SessionStore

_config = Nb2Config.from_env()
_host: RuntimeHost | None = None  # 由 Nonebot2Adapter.start() 经 bind_host 注入
_store = SessionStore(_config.data_dir / "sessions.json")
_members = MemberStore(_config.data_dir / "known_members.json")
_locks: dict[str, asyncio.Lock] = {}
_initialized: set[str] = set()  # 本进程内已成功 ensure_agent 的 agent_id
_targets: dict[str, tuple[str, int]] = {}  # agent_id -> ("group" | "user", QQ号)
_member_names: dict[tuple[int, int], str] = {}  # (群号, QQ号) -> 净化后的群名片/昵称
_impression_inflight: set[int] = set()  # 正在后台生成印象的 QQ 号（防并发重复生成）

# 随每条回复注入的附加要求（GSK_NB2_EXTRA_PROMPT），只影响当轮回复、不写入会话
_EXTRA_CONTEXTS = [f"【QQ 聊天场景附加要求】\n{_config.extra_prompt}"] if _config.extra_prompt else []

_REPLY_SEGMENT_DELAY_SECONDS = 0.8  # 分段发送间隔，避免消息刷屏抖动

SendCallable = Callable[[Message], Awaitable[None]]


def bind_host(host: RuntimeHost) -> None:
    """注入 RuntimeHost（由 Nonebot2Adapter.start 在 load_plugin 前调用）。"""
    global _host
    _host = host


def _require_host() -> RuntimeHost:
    if _host is None:
        raise RuntimeError("nb2 插件尚未绑定 RuntimeHost（应由 Nonebot2Adapter.start 注入）")
    return _host


async def _send_segmented(send: SendCallable, text: str) -> None:
    """分段发送：清洗 RP 标记后按行拆成多条短消息（QQ 聊天习惯，而非一大段墙）。"""
    cleaned = strip_rp_style(text) if _config.strip_rp_style else text
    if not cleaned.strip():
        logger.warning(f"[nb2] 清洗后无文本内容（原文 {len(text)} 字），跳过发送: {text[:60]!r}")
        return
    segments = split_reply_segments(cleaned) if _config.split_reply else [cleaned]
    for index, segment in enumerate(segments):
        if index:
            await asyncio.sleep(_REPLY_SEGMENT_DELAY_SECONDS)
        await send(Message(MessageSegment.text(segment)))  # text 段转义 CQ 码


async def _resolve_member_name(bot: Bot, group_id: int, qq: str) -> str:
    """解析群成员显示名：优先缓存（群消息发送者会持续填充），未命中调群成员接口。"""
    key = (group_id, int(qq))
    cached = _member_names.get(key)
    if cached:
        return cached
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=int(qq))
        name = sanitize_display_name(str(info.get("card") or info.get("nickname") or qq))
    except Exception as error:
        logger.debug(f"[nb2] 群成员名片查询失败（{group_id}/{qq}）: {error}")
        name = qq  # 兜底：至少给个 QQ 号
    _member_names[key] = name
    return name


async def _extract_group_text(bot: Bot, event: GroupMessageEvent) -> str:
    """逐段提取群消息文本：at 段转译为 @昵称（@bot 自身的段丢弃），其余非文本段忽略。"""
    parts: list[str] = []
    for segment in event.get_message():
        if segment.type == "text":
            parts.append(str(segment.data.get("text", "")))
        elif segment.type == "at":
            qq = str(segment.data.get("qq", ""))
            if not qq or qq == str(event.self_id):
                continue
            if qq == "all":
                parts.append("@全体成员")
            else:
                parts.append(f"@{await _resolve_member_name(bot, event.group_id, qq)}")
    return "".join(parts).strip()


async def update_member_impression(member_name: str, impression: str) -> str:
    """更新你对某位群友的印象备注：当你对 TA 的了解加深、或之前的印象不再准确时调用。
    member_name 是群友昵称（群聊消息里【】中的名字）；impression 是新的印象内容
    （你的第一人称、一两句话，不要动作描写）。同名群友存在多位时会更新第一位。"""
    cleaned = strip_rp_style(impression).replace("【", "").replace("】", "")[:240].strip()
    if not cleaned:
        return "印象内容为空，未更新。"
    if _members.update_by_name(member_name, cleaned):
        logger.info(f"[nb2] 角色更新了对 {member_name} 的印象")
        return f"已更新对 {member_name} 的印象。"
    return f"还没有关于 {member_name} 的印象记录——先正常交谈，第一印象会自动生成。"


async def _learn_impression(member_name: str, member_qq: int, exchange: str) -> None:
    """首轮交谈后给新群友生成角色视角的第一印象（后台任务，不阻塞回复）。"""
    host = _require_host()
    try:
        prompt = build_member_impression_prompt(_config.character, member_name, exchange)
        raw = await host.generate_meta_text(_config.character, prompt)
        impression = strip_rp_style(raw).replace("【", "").replace("】", "")[:240].strip()
        if not impression:
            return
        _members.put(member_name, member_qq, impression)
        logger.info(f"[nb2] 已记下对 {member_name} 的第一印象（{len(impression)} 字）")
    except Exception as error:
        logger.warning(f"[nb2] 生成对 {member_name} 的印象失败: {error}")
    finally:
        _impression_inflight.discard(member_qq)


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
    if _config.member_memory:
        # 让角色可以自行更新群友印象（注入当前及后续租户的工具注册表）
        await _require_host().register_adapter_tool(update_member_impression)
    logger.info(
        f"[nb2] 适配器已加载: 角色={_config.character}, "
        f"主动发言={'开' if _config.initiative else '关'}, "
        f"群白名单={sorted(_config.group_whitelist) or '不限'}, "
        f"分段回复={'开' if _config.split_reply else '关'}, "
        f"说话人标记={'开' if _config.sender_label else '关'}, "
        f"群友印象={'开' if _config.member_memory else '关'}, "
        f"附加要求={_config.extra_prompt[:30] or '无'}, root={_config.root_dir or 'cwd'}"
    )


@_driver.on_shutdown
async def _on_shutdown() -> None:
    # 宿主生命周期归 run_adapters 管（逆序 stop 后统一 host.close()），这里无需动作
    logger.debug("[nb2] nonebot 驱动已停止")


def _is_private_message(event: MessageEvent) -> bool:
    return isinstance(event, PrivateMessageEvent)


group_chat = on_message(rule=to_me(), priority=90, block=True)
private_chat = on_message(rule=Rule(_is_private_message), priority=90, block=True)


@group_chat.handle()
async def _handle_group(event: GroupMessageEvent, bot: Bot) -> None:
    if _config.group_whitelist and event.group_id not in _config.group_whitelist:
        return
    agent_id = f"qq-group-{event.group_id}"
    _targets[agent_id] = ("group", event.group_id)
    sender_name = None
    if _config.sender_label:
        raw_name = event.sender.card or event.sender.nickname or str(event.user_id)
        sender_name = sanitize_display_name(raw_name)
        _member_names[(event.group_id, event.user_id)] = sender_name  # 顺手填充名片缓存
    text = await _extract_group_text(bot, event)
    await _chat(
        matcher=group_chat,
        key=f"group:{event.group_id}",
        agent_id=agent_id,
        text=text,
        sender_name=sender_name,
        member_name=sender_name,
        member_qq=event.user_id,
        self_id=event.self_id,
        message_id=event.message_id,
    )


@private_chat.handle()
async def _handle_private(event: PrivateMessageEvent) -> None:
    agent_id = f"qq-user-{event.user_id}"
    _targets[agent_id] = ("user", event.user_id)
    member_name = sanitize_display_name(event.sender.nickname or str(event.user_id))
    await _chat(
        matcher=private_chat,
        key=f"user:{event.user_id}",
        agent_id=agent_id,
        text=event.get_message().extract_plain_text(),
        sender_name=None,
        member_name=member_name,
        member_qq=event.user_id,
        self_id=event.self_id,
        message_id=event.message_id,
    )


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


async def _ensure_agent(agent_id: str, entry: dict[str, Any] | None) -> tuple[str, int]:
    host = _require_host()
    stored_session = str(entry["session_id"]) if entry and entry.get("session_id") else None
    try:
        session_id, revision = await host.ensure_agent(
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
        session_id, revision = await host.ensure_agent(
            agent_id, _config.character, None, disable_initiative=not _config.initiative
        )
    if _config.initiative:
        try:
            await host.subscribe_events(agent_id, _deliver_initiative)
        except Exception as error:
            # 订阅失败只影响主动投递，不阻塞正常问答
            logger.warning(f"[nb2] 订阅主动消息事件失败（{agent_id}），主动投递暂不可用: {error}")
    _initialized.add(agent_id)
    return session_id, revision


async def _chat(
    matcher: type[Matcher],
    *,
    key: str,
    agent_id: str,
    text: str,
    sender_name: str | None,
    member_name: str | None,
    member_qq: int | None,
    self_id: int,
    message_id: int,
) -> None:
    text = text.strip()
    if not text or text.startswith("/"):
        return  # 忽略纯表情/图片消息与保留的 "/" 命令前缀
    if sender_name:
        # 群聊多对单：注入说话人标记，让角色在历史里分清每轮是谁说的
        text = f"【{sender_name}】{text}"
    contexts = list(_EXTRA_CONTEXTS)
    if _config.member_memory and member_qq is not None and member_name:
        impression = _members.get(member_qq)
        if impression:
            contexts.append(f"【你对 {member_name} 的印象】\n{impression}")
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
            idempotency_key = f"nb2:{self_id}:{message_id}"
            try:
                reply, new_revision = await _host_send(
                    agent_id, session_id, revision, text, idempotency_key, contexts
                )
            except RuntimeRpcError as error:
                if error.code == "resource.limit_exceeded":
                    await matcher.send(MessageSegment.text("幻想乡现在有点忙，稍后再叫我吧。"))
                    return
                # 租户丢失 / 会话失效：重建租户后同键重试一次（幂等安全）
                logger.warning(f"[nb2] 发送失败 [{error.code}]，重建租户会话后重试: {error}")
                session_id, revision = await _ensure_agent(agent_id, None)
                _store.put(key, agent_id=agent_id, session_id=session_id, revision=revision)
                reply, new_revision = await _host_send(
                    agent_id, session_id, revision, text, idempotency_key, contexts
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
    if (
        _config.member_memory
        and member_qq is not None
        and member_name
        and _members.get(member_qq) is None
        and member_qq not in _impression_inflight
    ):
        # 新群友：首轮交谈完成后后台生成第一印象（不阻塞回复）
        _impression_inflight.add(member_qq)
        asyncio.create_task(_learn_impression(member_name, member_qq, f"{text}\n{reply}"))
    await _send_segmented(matcher.send, reply)


async def _host_send(
    agent_id: str,
    session_id: str,
    revision: int,
    text: str,
    idempotency_key: str,
    contexts: list[str],
) -> tuple[str, int]:
    return await _require_host().send_message(
        agent_id,
        session_id,
        revision,
        text,
        idempotency_key=idempotency_key,
        system_contexts=contexts,
    )
