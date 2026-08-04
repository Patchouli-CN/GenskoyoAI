"""SaveCoordinator 去重状态机定向测试（05#6/05#13 修复）。"""

from __future__ import annotations

from GensokyoAI.core.agent.save_coordinator import SaveCoordinator
from GensokyoAI.core.config import SessionConfig
from GensokyoAI.memory.working import WorkingMemoryManager


def _coordinator() -> SaveCoordinator:
    return SaveCoordinator(
        session_manager=None,  # type: ignore[arg-type]
        session_config=SessionConfig(auto_save=True),
        label=None,
    )


def _wm() -> WorkingMemoryManager:
    wm = WorkingMemoryManager(max_turns=10)
    wm.add_message("user", "第一条")
    wm.add_message("assistant", "第一条回复")
    return wm


def test_failed_save_rolls_back_dedup_state() -> None:
    """05#13：保存失败（mark_saved success=False）后去重状态回滚，后续保存不被永久跳过。"""
    coordinator = _coordinator()
    wm = _wm()

    # 模拟一次失败的保存：mark_saving 乐观前移 → 本轮 should_save 返回 False
    coordinator.mark_saving(wm)
    assert coordinator.should_save(wm) is False

    # 保存失败：回滚后同一轮可以重试
    coordinator.mark_saved(success=False)
    assert coordinator.should_save(wm) is True


def test_successful_save_keeps_dedup_state() -> None:
    """05#13 反向：保存成功后去重状态保留，同内容不再重复保存。"""
    coordinator = _coordinator()
    wm = _wm()

    coordinator.mark_saving(wm)
    coordinator.mark_saved(success=True)

    # 内容没变 → 跳过
    assert coordinator.should_save(wm) is False


def test_stale_session_completion_ignored() -> None:
    """会话切换后到达的旧会话保存结果不得清掉新会话的 _save_pending。"""
    import asyncio

    from GensokyoAI.background.types import TaskResult

    coordinator = _coordinator()
    coordinator._save_pending = True
    coordinator._save_session_id = "sess-new"

    stale = TaskResult(
        task_id="t1",
        success=True,
        result={"operation": "save_messages", "session_id": "sess-old"},
        duration_ms=1.0,
    )
    asyncio.run(coordinator._on_save_task_complete(stale))
    assert coordinator._save_pending is True  # 旧会话结果：不动新会话状态

    current = TaskResult(
        task_id="t2",
        success=True,
        result={"operation": "save_messages", "session_id": "sess-new"},
        duration_ms=1.0,
    )
    asyncio.run(coordinator._on_save_task_complete(current))
    assert coordinator._save_pending is False
    assert coordinator._save_session_id is None


def test_reset_clears_save_session_id() -> None:
    coordinator = _coordinator()
    coordinator._save_pending = True
    coordinator._save_session_id = "sess-1"
    coordinator._dirty = True
    coordinator.reset()
    assert coordinator._save_pending is False
    assert coordinator._save_session_id is None
    assert coordinator._dirty is False
