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
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from nonebot import get_bots, get_driver, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
    PrivateMessageEvent,
)
from nonebot.matcher import Matcher
from nonebot.rule import Rule, to_me

from ...commands import CommandContext, CommandExecutor
from ...core.agent.attention import AttentionThings
from ...core.agent.prompts import (
    build_member_impression_prompt,
    build_multi_speaker_context,
    build_mute_break_context,
    build_mute_break_judge_prompt,
    build_mute_forgive_context,
    build_reminder_attention_prompt,
    build_reminder_cancelled_context,
    build_reminder_clarify_context,
    build_reminder_none_pending_context,
    build_reminder_preregistered_context,
    build_reminder_trigger_context,
    build_repeat_annoyance_context,
    build_repeat_farewell_context,
    build_reply_focus_prompt,
)
from ...core.health import HealthCenter
from ...runtime.host import RuntimeHost, RuntimeRpcError
from ...utils.helpers import sanitize_display_name, split_reply_segments, strip_rp_style
from ...utils.logger import logger
from .commands import NB2_COMMANDS, resolve_level
from .config import Nb2Config
from .pending import PendingChat, PendingChatQueue, merge_batch
from .reminders import (
    REMINDER_MAX_ATTEMPTS,
    REMINDER_TICK_SECONDS,
    Reminder,
    ReminderStore,
    local_now,
)
from .repeat_guard import RepeatGuard, RepeatVerdict
from .store import MemberStore, SessionStore
from .watchdog import NapCatWatchdog

_config = Nb2Config.from_env()
_host: RuntimeHost | None = None  # 由 Nonebot2Adapter.start() 经 bind_host 注入
_store = SessionStore(_config.data_dir / "sessions.json")
_members = MemberStore(_config.data_dir / "known_members.json")
_pending = PendingChatQueue()  # 多人同时发言的待发合并（替代旧的按会话锁串行）
_initialized: set[str] = set()  # 本进程内已成功 ensure_agent 的 agent_id
_targets: dict[str, tuple[str, int]] = {}  # agent_id -> ("group" | "user", QQ号)
_member_names: dict[tuple[int, int], str] = {}  # (群号, QQ号) -> 净化后的群名片/昵称
_impression_inflight: set[int] = set()  # 正在后台生成印象的 QQ 号（防并发重复生成）
_repeat_guard: RepeatGuard | None = None  # 复读烦躁模型（_on_startup 按全局配置构建）
_health_center: HealthCenter | None = None  # 框架健康中心（_on_startup 按 yaml health: 节构建）
_attention: AttentionThings | None = None  # 注意力事务管线（_on_startup 装配，reminder 为首个种类）

# NapCat 掉线守护（bot_offline 事件 / WS 断开 → 杀进程树 → 快速登录 → 确认回连）
_napcat_dir = _config.napcat_dir
if not _napcat_dir.is_absolute():
    _napcat_dir = (_config.root_dir or Path.cwd()) / _napcat_dir
_watchdog = NapCatWatchdog(
    enabled=_config.watchdog_enabled,
    cooldown_seconds=_config.watchdog_cooldown_seconds,
    max_restarts_per_day=_config.watchdog_max_restarts,
    recover_timeout_seconds=_config.watchdog_recover_timeout,
    disconnect_grace_seconds=_config.watchdog_disconnect_grace,
    alert_path=_config.data_dir / "napcat_offline_alert.json",
    bot_qq=_config.bot_qq,
)
_watchdog.configure(napcat_dir=_napcat_dir)

# 到点提醒：角色经 set_reminder 工具接活，tick 循环扫到点项后生成并 @ 投递
_reminders = ReminderStore(_config.data_dir / "reminders.json")
_reminder_task: asyncio.Task[None] | None = None
# 后台任务强引用集（_learn_impression 等 fire-and-forget 任务防 GC 提前回收）
_background_tasks: set[asyncio.Task[Any]] = set()

# 随每条回复注入的附加要求（GSK_NB2_EXTRA_PROMPT），只影响当轮回复、不写入会话
_EXTRA_CONTEXTS = (
    [f"【QQ 聊天场景附加要求】\n{_config.extra_prompt}"] if _config.extra_prompt else []
)

# 指令执行器（框架 commands 体系）：本地注册表，解析/权限/执行/日志统一
_command_executor = CommandExecutor(mode="smart", registry=NB2_COMMANDS)

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


async def _send_segmented(
    send: SendCallable, text: str, mention_map: dict[str, int] | None = None
) -> None:
    """分段发送：清洗 RP 标记后按行拆成多条短消息（QQ 聊天习惯，而非一大段墙）。

    mention_map（本群 昵称→QQ）给定时，文本里的 `@昵称` 转成**真 at 段**
    （QQ 显示并提醒）——模型自己写的 @ 也是程序 at，不再是哑文本。
    """
    cleaned = strip_rp_style(text) if _config.strip_rp_style else text
    if not cleaned.strip():
        logger.warning(f"[nb2] 清洗后无文本内容（原文 {len(text)} 字），跳过发送: {text[:60]!r}")
        return
    segments = split_reply_segments(cleaned) if _config.split_reply else [cleaned]
    for index, segment in enumerate(segments):
        if index:
            await asyncio.sleep(_REPLY_SEGMENT_DELAY_SECONDS)
        await send(_at_text_to_message(segment, mention_map or {}))


def _group_mention_map(group_id: int) -> dict[str, int]:
    """群名片缓存反查：该群 昵称→QQ 映射（模型文本里的 @昵称 转真 at 用）。"""
    return {
        name: qq for (gid, qq), name in _member_names.items() if gid == group_id and name
    }


def _at_text_to_message(text: str, mention_map: dict[str, int]) -> Message:
    """把文本里的 `@昵称` 转成真 at 段（按名字最长优先匹配），其余保持文本。"""
    if not mention_map:
        return Message(MessageSegment.text(text))
    names = sorted(mention_map, key=len, reverse=True)
    pattern = re.compile("@(" + "|".join(re.escape(name) for name in names) + ")")
    segments: list[MessageSegment] = []
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            segments.append(MessageSegment.text(text[pos : match.start()]))
        segments.append(MessageSegment.at(mention_map[match.group(1)]))
        pos = match.end()
    if pos < len(text):
        segments.append(MessageSegment.text(text[pos:]))
    return Message(segments)


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


_QUOTED_TEXT_MAX_CHARS = 120  # 引用原文截断长度（防长文灌进上下文烧 token）


async def _fetch_quoted_text(bot: Bot, message_id: str) -> str | None:
    """取引用消息的「发送者：纯文本」（截断）；查不到/为空返回 None。

    与 to_me() 的回复检查查的是同一个 get_msg 接口（NapCat 侧有缓存，代价低）；
    1200「消息为空」等失败静默跳过——引用上下文是增强而非必需。
    """
    try:
        info = await bot.get_msg(message_id=int(message_id))
    except Exception as error:
        logger.debug(f"[nb2] 引用消息查询失败（{message_id}）: {error}")
        return None
    sender = info.get("sender") or {}
    name = sanitize_display_name(str(sender.get("card") or sender.get("nickname") or "某人"))
    raw = info.get("message")
    text = Message(raw).extract_plain_text() if isinstance(raw, list) else str(raw or "")
    text = " ".join(text.split())[:_QUOTED_TEXT_MAX_CHARS].strip()
    if not text:
        return None
    return f"{name}：{text}"


async def _extract_group_text(bot: Bot, event: GroupMessageEvent) -> str:
    """逐段提取群消息文本：at 段转译为 @昵称（@bot 自身的段丢弃），
    reply 段取引用原文拼成（引用 昵称：…），其余非文本段忽略。"""
    parts: list[str] = []
    for segment in event.get_message():
        if segment.type == "text":
            parts.append(str(segment.data.get("text", "")))
        elif segment.type == "reply" and _config.quote_context:
            quoted = await _fetch_quoted_text(bot, str(segment.data.get("id", "")))
            if quoted:
                parts.append(f"（引用 {quoted}）")
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


_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


async def _judge_mute_break(member_label: str, text: str) -> str:
    """冷却期破例判定：LLM 以角色性格裁决 forgive/respond；失败一律 ignore（fail-closed）。

    只处理「有新意」的内容（纯复读已被 RepeatGuard 在判定前拦截，不烧 token）；
    经元租户一次性脱稿调用，短 JSON 输出。
    """
    host = _require_host()
    try:
        raw = await host.generate_meta_text(
            _config.character,
            build_mute_break_judge_prompt(_config.character, member_label, text),
        )
        match = _JSON_OBJECT_PATTERN.search(raw)
        data = json.loads(match.group(0) if match else raw)
        if isinstance(data, dict):
            if data.get("forgive") is True:
                return "forgive"
            if data.get("respond") is True:
                return "respond"
    except Exception as error:
        logger.debug(f"[nb2] 破例判定失败（{member_label}）: {error}")
    return "ignore"


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
        await _send_segmented(
            _send,
            content,
            mention_map=_group_mention_map(target_id) if kind == "group" else None,
        )
        logger.info(f"[nb2] 主动消息已投递到 {kind}:{target_id}（{len(content)} 字）")
    except Exception:
        logger.exception(f"[nb2] 主动消息投递失败（{agent_id}）")


# ==================== 到点提醒 ====================


@dataclass
class _ReminderOutcome:
    ok: bool
    message: str  # 给模型/用户看的登记结果
    due_text: str = ""
    remind_name: str = ""
    content: str = ""


def _register_reminder(
    agent_id: str, due: datetime, content: str, target_name: str
) -> _ReminderOutcome:
    """登记一条到点提醒（唯一登记通道：AttentionThings 代办）。

    due 必须是判定 LLM 输出的绝对时间（ISO 8601 解析后的 datetime）——
    本函数只做范围校验，不做任何形式判断（正则解析已被用户砍掉）。
    """
    now = local_now()
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)  # 裸 ISO 按本地时区
    if due - now < timedelta(seconds=REMINDER_TICK_SECONDS):
        return _ReminderOutcome(False, "太近或已过")
    if due - now > timedelta(days=30):
        return _ReminderOutcome(False, "太远（超过 30 天）")
    content_clean = content.strip()[:200]
    if not content_clean:
        return _ReminderOutcome(False, "提醒的事是空的")
    if _reminders.pending_count(agent_id) >= _config.reminder_max_per_tenant:
        return _ReminderOutcome(False, f"这里已经攒了 {_config.reminder_max_per_tenant} 条提醒了")
    target = _targets.get(agent_id)
    if target is None:
        return _ReminderOutcome(False, "投递目标未知")
    kind, target_id = target
    remind_qq, remind_name = _resolve_remind_target(kind, target_id, target_name)
    reminder = Reminder(
        id=uuid4().hex[:12],
        agent_id=agent_id,
        key=f"{kind}:{target_id}",
        kind=kind,
        target_id=target_id,
        remind_qq=remind_qq,
        remind_name=remind_name,
        content=content_clean,
        due=due,
        created_at=now,
    )
    _reminders.add(reminder)
    due_text = due.strftime("%m月%d日 %H:%M")
    logger.info(
        f"[nb2] {agent_id} 新增提醒：{due_text} 提醒 {remind_name}"
        f"「{content_clean[:30]}」（{reminder.id}）"
    )
    return _ReminderOutcome(
        True,
        f"记下啦：{due_text} 提醒 {remind_name}「{content_clean}」，到点我会来说。",
        due_text=due_text,
        remind_name=remind_name,
        content=content_clean,
    )


def _resolve_remind_target(kind: str, target_id: int, target_name: str) -> tuple[int | None, str]:
    """把 LLM 给的昵称解析成要 @ 的 QQ（群名片缓存反查）；找不到则不 @、只带名字。"""
    name = target_name.strip().replace("【", "").replace("】", "")
    if kind == "user":
        return target_id, name or "你"
    if not name:
        return None, "大家"
    for (group_id, qq), cached in _member_names.items():
        if group_id == target_id and cached == name:
            return qq, name
    return None, name


# ==================== 注意力事务（AttentionThings） ====================


class _ReminderAttentionKind:
    """AttentionThings 的第一个种类：到点提醒（请求/取消）判定。

    预筛恒真（用户 2026-08-02 定稿：「别代码里判断了，多个 LLM 自主判断
    又不会死」——代码关键词预筛会拦掉花式说法，全部交给 LLM 判定；
    candidate 钩子保留供未来种类使用）。
    """

    name = "reminder"

    def candidate(self, text: str) -> bool:
        return True

    def judge_prompt(self, text: str) -> str:
        return build_reminder_attention_prompt(text, local_now())

    def parse(self, raw: str) -> dict[str, Any] | None:
        try:
            match = _JSON_OBJECT_PATTERN.search(raw)
            data = json.loads(match.group(0) if match else raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        intent = data.get("intent")
        if intent == "cancel":
            return {"intent": "cancel", "scope": data.get("scope") or "latest"}
        if intent != "reminder":
            return None
        content = str(data.get("content") or "").strip()
        if not content:
            return None
        due: datetime | None = None
        due_at = str(data.get("due_at") or "").strip()
        if due_at:
            try:
                due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            except ValueError:
                due = None
        return {
            "intent": "reminder",
            "due": due,
            "content": content,
            "target_name": str(data.get("target_name") or "").strip(),
        }


class _ReplyFocusAttentionKind:
    """AttentionThings 的第二个种类：回应焦点（因果关系）判定。

    判定本轮消息里谁是冲着 bot 来的（提问/委托/等回应）；焦点名单驱动
    QQ 端真实 @——有焦点才 @，普通闲聊无焦点不 @（「批次全员都 @」的
    代码启发式已废弃，@ 是否必要属于因果判断，交给 LLM）。
    """

    name = "reply_focus"

    def candidate(self, text: str) -> bool:
        return True

    def judge_prompt(self, text: str) -> str:
        return build_reply_focus_prompt(text)

    def parse(self, raw: str) -> dict[str, Any] | None:
        try:
            match = _JSON_OBJECT_PATTERN.search(raw)
            data = json.loads(match.group(0) if match else raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        focus = data.get("focus")
        if not isinstance(focus, list):
            return None
        names = [str(name).strip() for name in focus if str(name).strip()]
        if not names:
            return None
        return {"focus": names[:2]}  # prompt 约定最多两个，截断防模型失控


async def _dispatch_attention(
    verdict: Any, agent_id: str, sender_name: str | None = None
) -> str | None:
    """把 AttentionVerdict 处置为注入上下文（代办式：直接登记/取消，不求模型调工具）。

    sender_name：本轮发言者名（群聊【昵称】）——judge 没给目标人时兜底，
    否则提醒会落成「大家」无 @（2026-08-02 实机问题）。
    """
    if verdict.kind != "reminder":
        return None
    intent = verdict.data.get("intent")
    if intent == "cancel":
        return await _dispatch_cancel(verdict, agent_id)
    if intent != "reminder":
        return None
    due = verdict.data.get("due")
    if due is None:
        logger.info(f"[nb2] {agent_id} 注意力事务：提醒时间待确认")
        return build_reminder_clarify_context("", verdict.data["content"])
    target_name = verdict.data.get("target_name") or (sender_name or "")
    outcome = _register_reminder(
        agent_id, due, verdict.data["content"], target_name
    )
    if outcome.ok:
        logger.info(
            f"[nb2] {agent_id} 注意力事务已代办：{outcome.due_text} 提醒 "
            f"{outcome.remind_name}「{outcome.content[:30]}」"
        )
        return build_reminder_preregistered_context(
            outcome.due_text, outcome.remind_name, outcome.content
        )
    # 时间太近/太远：让角色用口吻问清，而不是干瞪眼装没听见
    logger.info(f"[nb2] {agent_id} 注意力事务：提醒时间待确认（{outcome.message}）")
    return build_reminder_clarify_context(outcome.message, verdict.data["content"])


async def _dispatch_cancel(verdict: Any, agent_id: str) -> str:
    """取消代办：「不要提醒了」类请求命中时执行（用户点单的取消机制）。"""
    if verdict.data.get("scope") == "all":
        items = _reminders.cancel_all(agent_id)
    else:
        latest = _reminders.cancel_latest(agent_id)
        items = [latest] if latest else []
    if not items:
        logger.info(f"[nb2] {agent_id} 注意力事务：取消命中但无待办提醒")
        return build_reminder_none_pending_context()
    contents = [item.content for item in items]
    logger.info(f"[nb2] {agent_id} 注意力事务已代办取消 {len(items)} 条: {contents}")
    return build_reminder_cancelled_context(contents)


async def _inspect_attention(
    text: str, agent_id: str, sender_name: str | None = None, *, group: bool = False
) -> tuple[list[str], list[str]]:
    """对本轮文本跑注意力管线，返回 (代办上下文, 回应焦点名单)。

    私聊不跑 reply_focus（@ 无意义，省一次判定调用）。
    """
    if _attention is None:
        return [], []
    try:
        verdicts = await _attention.inspect(text, only=None if group else {"reminder"})
    except Exception as error:
        logger.debug(f"[nb2] 注意力判定失败（忽略）: {error}")
        return [], []
    notes: list[str] = []
    focus: list[str] = []
    for verdict in verdicts:
        if verdict.kind == "reply_focus":
            focus.extend(verdict.data.get("focus", []))
            continue
        note = await _dispatch_attention(verdict, agent_id, sender_name)
        if note:
            notes.append(note)
    return notes, focus


def _resolve_focus_targets(key: str, batch: list[PendingChat], names: list[str]) -> list[int]:
    """注意力判定的焦点名单 → 要 @ 的 QQ：先查本批发言人，再反查群名片缓存。"""
    batch_map = {
        item.member_name: item.member_qq
        for item in batch
        if item.member_name and item.member_qq is not None
    }
    group_id = int(key.split(":", 1)[1])
    targets: list[int] = []
    for name in names:
        qq = batch_map.get(name)
        if qq is None:
            qq, _ = _resolve_remind_target("group", group_id, name)
        if qq is not None and qq not in targets:
            targets.append(qq)
    return targets


# 回复开头的文本 @ 串（模型自己写的纯文本 @，QQ 不显示成 at）
_LEADING_AT_RUN_PATTERN = re.compile(r"^(?:@[^\s@]+\s*)+")


def _strip_leading_at_mentions(reply: str) -> str:
    """剥掉回复开头的文本 @ 串：模型自己写的 @ 是纯文本（QQ 不会显示成
    at），与代码拼的真 at 段重复——只保留程序 at（剥空了则保留原文，
    防误伤）。按形态剥（不精确匹配名字）：模型可能把昵称写错成变体。"""
    stripped = _LEADING_AT_RUN_PATTERN.sub("", reply, count=1)
    return stripped or reply


async def _generate_for_tenant(
    agent_id: str, key: str, text: str, contexts: list[str], idempotency_key: str
) -> str:
    """租户会话生成（session/revision 舞蹈）：未初始化先 ensure，RPC 失败重建
    租户同键重试一次（幂等安全）；resource.limit_exceeded 直接上抛不重建。"""
    entry = _store.get(key)
    if entry is None or agent_id not in _initialized:
        session_id, revision = await _ensure_agent(agent_id, entry)
        _store.put(key, agent_id=agent_id, session_id=session_id, revision=revision)
    else:
        session_id = str(entry["session_id"])
        revision = int(entry["revision"])
    try:
        reply, new_revision = await _host_send(
            agent_id, session_id, revision, text, idempotency_key, contexts
        )
    except RuntimeRpcError as error:
        if error.code == "resource.limit_exceeded":
            raise
        logger.warning(f"[nb2] 发送失败 [{error.code}]，重建租户会话后重试: {error}")
        session_id, revision = await _ensure_agent(agent_id, None)
        _store.put(key, agent_id=agent_id, session_id=session_id, revision=revision)
        reply, new_revision = await _host_send(
            agent_id, session_id, revision, text, idempotency_key, contexts
        )
    _store.update_revision(key, new_revision)
    return reply


async def _fire_reminder(reminder: Reminder) -> None:
    """到点投递：让角色用自己的口吻生成提醒文本（走租户会话，角色记得答应过），
    群聊 @ 目标分段发送；失败计入重试（下轮 tick 再来），超上限放弃。"""
    bots = get_bots()
    if not bots:
        attempts = _reminders.bump_attempts(reminder.id)
        if attempts >= REMINDER_MAX_ATTEMPTS:
            logger.warning(f"[nb2] 提醒 {reminder.id} 协议端长期未连接，放弃投递")
            _reminders.mark_done(reminder.id)
        return
    bot = next(iter(bots.values()))
    target_label = f"【{reminder.remind_name}】" if reminder.remind_name else "对方"
    contexts = [
        *_EXTRA_CONTEXTS,
        build_reminder_trigger_context(target_label, reminder.content),
    ]
    try:
        reply = await _generate_for_tenant(
            reminder.agent_id,
            reminder.key,
            f"【提醒触发】时间到了，该提醒 {target_label}：{reminder.content}",
            contexts,
            f"nb2-reminder:{reminder.id}",
        )
    except Exception as error:
        attempts = _reminders.bump_attempts(reminder.id)
        logger.warning(f"[nb2] 提醒 {reminder.id} 生成失败（第 {attempts} 次）: {error}")
        if attempts >= REMINDER_MAX_ATTEMPTS:
            logger.warning(f"[nb2] 提醒 {reminder.id} 生成屡败，放弃投递")
            _reminders.mark_done(reminder.id)
        return
    if not reply.strip():
        reply = f"喂——{reminder.remind_name}，{reminder.content}！"

    async def _send(message: Message) -> None:
        if reminder.kind == "group":
            await bot.send_group_msg(group_id=reminder.target_id, message=message)
        else:
            await bot.send_private_msg(user_id=reminder.target_id, message=message)

    send = _send
    if reminder.kind == "group" and reminder.remind_qq is not None:
        # @ 拼进首条分段（@ 后补个空格，避免和文字粘在一起）；模型自己开头
        # 写的文本 @ 是纯文本且与真 at 重复，剥掉只留程序 at
        reply = _strip_leading_at_mentions(reply)
        at_prefix = MessageSegment.at(reminder.remind_qq)
        first = True

        async def _send_with_at(message: Message) -> None:
            nonlocal first
            if first:
                message = Message([at_prefix, MessageSegment.text(" ")]) + message
                first = False
            await _send(message)

        send = _send_with_at
    try:
        await _send_segmented(
            send,
            reply,
            mention_map=(
                _group_mention_map(reminder.target_id) if reminder.kind == "group" else None
            ),
        )
    except Exception as error:
        attempts = _reminders.bump_attempts(reminder.id)
        logger.warning(f"[nb2] 提醒 {reminder.id} 投递失败（第 {attempts} 次）: {error}")
        if attempts >= REMINDER_MAX_ATTEMPTS:
            logger.warning(f"[nb2] 提醒 {reminder.id} 投递屡败，放弃")
            _reminders.mark_done(reminder.id)
        return
    _reminders.mark_done(reminder.id)
    logger.info(
        f"[nb2] 提醒已投递（{reminder.agent_id} → {reminder.remind_name}"
        f"「{reminder.content[:30]}」）"
    )


async def _reminder_loop() -> None:
    """30s tick 扫到点提醒（到点精度 ±30s，对聊天提醒足够）。"""
    while True:
        await asyncio.sleep(REMINDER_TICK_SECONDS)
        try:
            for reminder in _reminders.due(local_now()):
                await _fire_reminder(reminder)
        except Exception:
            logger.exception("[nb2] 提醒调度循环出错")


_driver = get_driver()


@_driver.on_startup
async def _on_startup() -> None:
    host = _require_host()
    if _config.member_memory:
        # 让角色可以自行更新群友印象（注入当前及后续租户的工具注册表）
        await host.register_adapter_tool(update_member_impression)
    global _reminder_task
    if _config.reminders_enabled:
        _reminder_task = asyncio.create_task(_reminder_loop())
    global _repeat_guard
    guard_config = host.get_app_config().repeat_guard
    if guard_config.enabled:
        _repeat_guard = RepeatGuard.from_config(guard_config)
    global _health_center
    _health_center = HealthCenter(host.get_app_config().health)
    global _attention
    if _config.attention_enabled:
        # 注意力事务管线：判定走一次性脱稿生成（不进任何会话），reminder 为首个种类
        _attention = AttentionThings(
            lambda prompt: host.generate_meta_text(_config.character, prompt)
        )
        _attention.register(_ReminderAttentionKind())
        _attention.register(_ReplyFocusAttentionKind())
    if _config.watchdog_enabled:
        # 启动期引导：10 秒宽限内协议端没连上，就直接复用掉线恢复流程拉起 NapCat
        # （不用手动先启动 NapCat；守护单 flight，与其他触发路径天然去重）
        async def _bootstrap_watchdog() -> None:
            await asyncio.sleep(10.0)
            if not _watchdog._connected.is_set():
                logger.info("[nb2] 启动宽限内协议端未连接，守护自动拉起 NapCat")
                await _watchdog.trigger("startup_bootstrap")

        task = asyncio.create_task(_bootstrap_watchdog())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    repeat_guard_desc = (
        f"开（厌烦 {_repeat_guard.warn_streak}/不理 {_repeat_guard.mute_streak} 连击）"
        if _repeat_guard
        else "关"
    )
    logger.info(
        f"[nb2] 适配器已加载: 角色={_config.character}, "
        f"主动发言={'开' if _config.initiative else '关'}, "
        f"群白名单={sorted(_config.group_whitelist) or '不限'}, "
        f"分段回复={'开' if _config.split_reply else '关'}, "
        f"说话人标记={'开' if _config.sender_label else '关'}, "
        f"群友印象={'开' if _config.member_memory else '关'}, "
        f"引用上下文={'开' if _config.quote_context else '关'}, "
        f"复读防护={repeat_guard_desc}, "
        f"掉线守护={'开' if _config.watchdog_enabled else '关'}, "
        f"到点提醒={'开' if _config.reminders_enabled else '关'}, "
        f"注意力事务={'开' if _attention else '关'}, "
        f"附加要求={_config.extra_prompt[:30] or '无'}, root={_config.root_dir or 'cwd'}"
    )


@_driver.on_bot_connect
async def _on_bot_connect(bot: Bot) -> None:
    _watchdog.notify_connected(int(bot.self_id))


@_driver.on_bot_disconnect
async def _on_bot_disconnect(bot: Bot) -> None:
    _watchdog.notify_disconnected()


def _is_bot_offline(event: NoticeEvent) -> bool:
    # NapCat 扩展通知（onebot-adapter 无内置模型，退化为基础 NoticeEvent，
    # 额外字段经 pydantic extra 保留）：账号被踢下线/登录态失效
    return getattr(event, "notice_type", "") == "bot_offline"


bot_offline_notice = on_notice(rule=Rule(_is_bot_offline), priority=1)


@bot_offline_notice.handle()
async def _handle_bot_offline(event: NoticeEvent) -> None:
    _watchdog.notify_bot_offline(
        str(getattr(event, "tag", "") or ""), str(getattr(event, "message", "") or "")
    )


@_driver.on_shutdown
async def _on_shutdown() -> None:
    _watchdog.close()  # 正常关停不触发自动恢复（防止退出时反而把 NapCat 拉起来）
    if _reminder_task is not None and not _reminder_task.done():
        _reminder_task.cancel()
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
    if text.strip().startswith("/"):
        await _dispatch_command(text.strip(), event, bot, group_chat)
        return  # 指令消息不进会话
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
async def _handle_private(event: PrivateMessageEvent, bot: Bot) -> None:
    agent_id = f"qq-user-{event.user_id}"
    _targets[agent_id] = ("user", event.user_id)
    member_name = sanitize_display_name(event.sender.nickname or str(event.user_id))
    text = event.get_message().extract_plain_text()
    if text.strip().startswith("/"):
        await _dispatch_command(text.strip(), event, bot, private_chat)
        return  # 指令消息不进会话
    await _chat(
        matcher=private_chat,
        key=f"user:{event.user_id}",
        agent_id=agent_id,
        text=text,
        sender_name=None,
        member_name=member_name,
        member_qq=event.user_id,
        self_id=event.self_id,
        message_id=event.message_id,
    )


async def _fetch_member_role(bot: Bot, event: MessageEvent) -> str | None:
    """取群成员角色用于权限判定；私聊按普通成员，查询失败返回 None（→ VISITOR）。"""
    if not isinstance(event, GroupMessageEvent):
        return "member"
    try:
        info = await bot.get_group_member_info(group_id=event.group_id, user_id=event.user_id)
        return str(info.get("role") or "member")
    except Exception as error:
        logger.debug(f"[nb2] 群成员角色查询失败（{event.group_id}/{event.user_id}）: {error}")
        return None


async def _dispatch_command(
    text: str, event: MessageEvent, bot: Bot, matcher: type[Matcher]
) -> None:
    """bot 指令分发：框架 CommandExecutor 统一解析/权限闸门/执行（自带执行日志）。

    权限不足与未注册指令对用户静默（不提示指令存在），执行细节全部在日志里。
    """
    role = await _fetch_member_role(bot, event)
    level = resolve_level(event.user_id, _config.owner_qq, role)
    if isinstance(event, GroupMessageEvent):
        raw_name = event.sender.card or event.sender.nickname or str(event.user_id)
    else:
        raw_name = event.sender.nickname or str(event.user_id)
    sender = sanitize_display_name(raw_name)

    async def send(content: str) -> None:
        await matcher.send(MessageSegment.text(content))

    ctx = CommandContext(
        source="nb2",
        issuer=f"{sender}({event.user_id})",
        permission=level,
        metadata={
            "host": _require_host(),
            "config": _config,
            "member_qq": event.user_id,
            "send": send,
            # /status 的复读防护行（None 时该行不显示）
            "repeat_guard": _repeat_guard,
            # /status 的额度健康判定（框架健康中心；启动前懒构建兜底）
            "health_center": _health_center
            or HealthCenter(_require_host().get_app_config().health),
        },
    )
    await _command_executor.execute(text, ctx)


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
        return  # 忽略纯表情/图片消息与未登记的指令文本（指令已在 handler 层分发）
    verdict = None
    break_action: str | None = None
    member_label = member_name or str(member_qq or "?")
    if _repeat_guard is not None and member_qq is not None:
        verdict = _repeat_guard.check(key, member_qq, text)
        if verdict.verdict is RepeatVerdict.MUTED:
            # 「不理」冷却中且仍是复读：静默丢弃，不进 Runtime（零 token）
            logger.info(
                f"[nb2] {agent_id} 的 {member_label} 处于「不理」冷却"
                f"（剩 {verdict.remaining_seconds:.0f} 秒），复读消息已忽略"
            )
            return
        if verdict.verdict is RepeatVerdict.MUTED_NOVEL:
            # 冷却中但内容有新意：由 LLM 以角色性格裁决要不要破例
            if _repeat_guard.llm_break:
                break_action = await _judge_mute_break(member_label, text)
            if break_action == "forgive":
                _repeat_guard.forgive(key, member_qq)
                logger.info(f"[nb2] {agent_id} 的 {member_label} 获得原谅，「不理」解除")
            elif break_action == "respond":
                logger.info(f"[nb2] {agent_id} 的 {member_label} 触发破例回应（尚未消气）")
            else:
                logger.info(
                    f"[nb2] {agent_id} 的 {member_label} 冷却中"
                    f"（剩 {verdict.remaining_seconds:.0f} 秒），新内容未获破例，消息已忽略"
                )
                return
        elif verdict.verdict is RepeatVerdict.ANNOYED:
            logger.info(
                f"[nb2] {agent_id} 的 {member_label} 复读连击 {verdict.streak} 次，"
                "已注入厌烦情绪（回复将转冷淡）"
            )
        elif verdict.verdict is RepeatVerdict.FAREWELL:
            logger.info(
                f"[nb2] {agent_id} 的 {member_label} 复读连击 {verdict.streak} 次，"
                f"角色将当面表态「不理他」并进入 {_repeat_guard.mute_seconds / 60:.0f} 分钟冷却"
            )
        elif verdict.streak:
            logger.trace(f"[nb2] {agent_id} 的 {member_label} 复读连击 {verdict.streak} 次")
    if sender_name:
        # 群聊多对单：注入说话人标记，让角色在历史里分清每轮是谁说的
        text = f"【{sender_name}】{text}"
    contexts = list(_EXTRA_CONTEXTS)
    if _config.member_memory and member_qq is not None and member_name:
        impression = _members.get(member_qq)
        if impression:
            contexts.append(f"【你对 {member_name} 的印象】\n{impression}")
    if verdict is not None:
        if verdict.verdict is RepeatVerdict.ANNOYED:
            # 厌烦区：角色回复自然转冷淡（由性格决定怎么表达）
            contexts.append(build_repeat_annoyance_context(member_label, verdict.streak))
        elif verdict.verdict is RepeatVerdict.FAREWELL:
            # 最后一句话：本轮表态后进入「不理」冷却
            contexts.append(build_repeat_farewell_context(member_label))
        elif verdict.verdict is RepeatVerdict.MUTED_NOVEL and break_action is not None:
            # 冷却期破例：消气原谅 / 偷偷回一句（「不理」状态仍继续）
            if break_action == "forgive":
                contexts.append(build_mute_forgive_context(member_label))
            else:
                contexts.append(build_mute_break_context(member_label))
    item = PendingChat(
        text=text,
        contexts=contexts,
        member_name=member_name,
        member_qq=member_qq,
        self_id=self_id,
        message_id=message_id,
    )
    if not _pending.add(key, item):
        # 该会话正在处理中：消息并入待发，由处理循环合并成一轮一起回应
        logger.info(f"[nb2] {agent_id} 正在处理中，{member_label} 的消息已并入待发")
        return
    try:
        # 合并窗口：等一等接连到达的发言，窗口内攒下的消息合成一轮回复，
        # 避免两个人同时 @ 时交替回（0 = 不等待直接处理）
        if _config.merge_window_seconds > 0:
            await asyncio.sleep(_config.merge_window_seconds)
        while batch := _pending.take_batch(key):
            await _process_batch(matcher, key=key, agent_id=agent_id, batch=batch)
    finally:
        _pending.finish(key)


async def _process_batch(
    matcher: type[Matcher],
    *,
    key: str,
    agent_id: str,
    batch: list[PendingChat],
) -> None:
    """把一批待发消息合并成一轮处理：一次生成、一条回复同时回应所有人。"""
    text, contexts, idempotency_key = merge_batch(batch)
    if len(batch) > 1:
        contexts.append(build_multi_speaker_context(len(batch)))
        logger.info(f"[nb2] {agent_id} 合并 {len(batch)} 条待发消息为一轮处理")
    # 注意力事务管线：命中待办（如提醒请求）直接代办登记并注入告知上下文
    # （目标人兜底取本轮发言者，防「大家」无 @）
    attention_notes, focus_names = await _inspect_attention(
        text, agent_id, batch[-1].member_name, group=key.startswith("group:")
    )
    contexts.extend(attention_notes)
    try:
        reply = await _generate_for_tenant(agent_id, key, text, contexts, idempotency_key)
    except RuntimeRpcError as error:
        if error.code == "resource.limit_exceeded":
            await matcher.send(MessageSegment.text("幻想乡现在有点忙，稍后再叫我吧。"))
        else:
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
    if _config.member_memory:
        # 新群友：首轮交谈完成后后台生成第一印象（不阻塞回复；按批去重。
        # 任务持强引用——事件循环对 task 只持弱引用，不存引用可能被 GC
        # 提前回收，成员将在 _impression_inflight 里永久占位不再生成）
        seen: set[int] = set()
        for item in batch:
            if (
                item.member_qq is not None
                and item.member_name
                and item.member_qq not in seen
                and _members.get(item.member_qq) is None
                and item.member_qq not in _impression_inflight
            ):
                seen.add(item.member_qq)
                _impression_inflight.add(item.member_qq)
                task = asyncio.create_task(
                    _learn_impression(item.member_name, item.member_qq, f"{text}\n{reply}")
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
    send = matcher.send
    if key.startswith("group:") and focus_names and (
        at_targets := _resolve_focus_targets(key, batch, focus_names)
    ):
        # 回应焦点（AttentionThings 因果判定：谁冲着 bot 来）才 @——真 at 段
        # 拼进首条分段；普通闲聊无焦点不 @。模型自己开头写的文本 @ 是纯文本
        # （QQ 不显示成 at）且与真 at 重复，剥掉只留程序 at
        reply = _strip_leading_at_mentions(reply)
        at_message = Message(
            [
                segment
                for qq in at_targets
                for segment in (MessageSegment.at(qq), MessageSegment.text(" "))
            ]
        )
        first = True

        async def _send_with_at(message: Message) -> None:
            nonlocal first
            if first:
                message = at_message + message
                first = False
            await matcher.send(message)

        send = _send_with_at
    await _send_segmented(
        send,
        reply,
        mention_map=(
            _group_mention_map(int(key.split(":", 1)[1])) if key.startswith("group:") else None
        ),
    )


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
