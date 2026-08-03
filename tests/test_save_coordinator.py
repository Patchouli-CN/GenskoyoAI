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
