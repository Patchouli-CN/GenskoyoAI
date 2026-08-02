# GensokyoAI — 交接文档

> 给接手的 AI：本文件是**唯一入口**。更新日志仅保留**当日**条目；
> 历史档案（多角色项目全程记录 + 2026-07-30~08-01 更新日志）在 `ignore/MEMORY.md`（本地留存，不入库）。
> 多角色完整实施计划见 `docs/gsk-ai-multi-character.md`。

## 1. 硬性约束（务必遵守）

1. **不要 `git commit` / `git push`**。完成后只报告改动文件、验证结果、建议 commit message，由用户亲自提交。
2. 动手前先 `git status` 确认工作树；工作树干净时 `git pull --ff-only` 检查上游，有更新则先快进并重新核对接线点。无法 ff / 有冲突 / 有用户改动 → 停止并报告，不擅自 merge/reset/stash。
3. **逐阶段推进**：一次做一个可独立验证的阶段，跑完测试再进下一个。用户在意 token 成本，别一口气堆完多个阶段。
4. 每阶段完成后更新 `docs/gsk-ai-multi-character.md` 里对应小节的状态标注。

---


## 8. 更新日志（仅保留当日；更早见 ignore/MEMORY.md）

## 8.46 nb2 到点提醒：set_reminder 工具 + 角色口吻 @ 投递（2026-08-02，用户点单）

- 玩法（用户原话）：「到点提醒……用里面的工具注入来实现……到点了 at
  一下被提醒人，用角色的口吻」。
- **接活**：每个租户经 `host.register_tenant_tool`（host 新 API，闭包
  捕获租户 id）注入 `set_reminder(when, content, target_name)`，角色
  自己决定调用；租户驱逐重建时 ensure 流程再注册。target_name 经群名片
  缓存反查 QQ（找不到则不 @、只带名字）。
- **reminders.py（新）**：`parse_when` 确定性解析（相对"10分钟后"/
  时刻"15:30·明天 08:00"/绝对"2026-08-03 15:30"，非法时刻返回 None 不
  炸）；`ReminderStore`（reminders.json 原子写持久化，重启恢复待办、
  逾期 24h 作废、attempts 重置）。时间全链路时区感知本地 datetime
  （ISO 落盘）——用户纠偏「datetime 是好用的工具，干嘛 time.time」，
  顺手对齐项目时区感知约定。
- **到点投递**：30s tick 扫到点项 → 走该租户会话生成（角色记得答应过，
  注入 build_reminder_trigger_context）→ 群聊 @ 拼首条分段发送/私聊
  直发；未连接/生成失败/投递失败计入 attempts 下轮再试，40 次（约 20
  分钟）放弃；每租户待办上限 20（GSK_NB2_REMINDER_MAX_PER_TENANT）。
- 连带重构：`_process_batch` 的 session/revision 舞蹈抽成
  `_generate_for_tenant`（提醒路径复用；resource.limit_exceeded 不重建
  直接上抛，行为与旧实现等价）。配置：`GSK_NB2_REMINDERS`（默认 true）。
- 测试：tests/test_nb2_reminders.py 17 例（时间解析全格式+非法输入/
  存储 CRUD 持久化过期重置/工具成功·坏时间·无目标·无名回退/群投递带
  @·私聊无 @·无连接重试·屡败放弃）。增量验证 63 passed，
  ruff / pyright 全绿（本轮起改增量验证，用户指示）。

建议 commit message（用户已授权提交）：
`feat(nb2): 到点提醒——set_reminder 工具按租户注入，30s tick 到点后角色口吻生成并 @ 投递（持久化重启不丢，失败重试上限放弃）`

## 8.45 关闭日志租户标签 + 守护重启控制台乱码修复（2026-08-02，用户点单）

- 问题：多租户关停时刷屏的同款关闭日志（思考引擎/后台管理器/工作器/
  最终保存/Agent 已关闭）没有租户身份，分不清哪行是哪户。
- **label 透传**：Agent 新增可选 `label` 参数（`_log_label` +
  `_tenant_suffix`），构造 ThinkEngine（`log_label`）/ BackgroundManager
  （`label`）/ SaveCoordinator（`label`）时透传；RuntimeService 租户创建
  Agent 时传 `self._tenant_key[1]`（agent_id，如 qq-group-263402786）；
  本地/CLI（tenant_key=None）不传，日志与此前完全一致。
- 生效行：Agent 初始化/已关闭、思考引擎启动/停止（`, 租户: x` 嵌进原有
  括号内）、后台管理器已停止与工作器取消/停止/异常、最终保存已完成。
- **乱码修复**（§8.44 守护连带）：launcher bat 里有 `chcp 65001` 把
  控制台切 UTF-8，守护直接拉起 exe 时新控制台是默认 GBK → 中文日志
  全乱码。`_windows_launch_napcat` 改经 `cmd /c "chcp 65001 >nul && …"`
  启动（CREATE_NEW_CONSOLE 不变）。
- 测试：tests/test_tenant_log_labels.py 4 例（四组件后缀有/无 label）+
  test_nb2_watchdog.py 增 LaunchCommandTests（cmd 包装/chcp 65001/QQ 号/
  loadNapCat.js 落盘）。基线：826 passed, 3 subtests passed，
  ruff / pyright 全绿。

建议 commit message（待用户授权后提交）：
`feat(runtime): 关闭日志带租户标签——Agent/ThinkEngine/BackgroundManager/SaveCoordinator 透传 label，租户创建传 agent_id；watchdog 启动命令补 chcp 65001 修控制台乱码`

## 8.44 NapCat 掉线守护：bot_offline 事件 + 自动快速登录恢复（2026-08-02，用户定稿）

- 起因：账号被风控踢下线（`[KickedOffLine] 你的账号当前登录已失效，
  请重新登录`）。NapCat 会把它包装成 OneBot `bot_offline` 通知事件推送
  （NapCat 事件文档 BotOfflineEvent：tag + message）；onebot-adapter
  2.4.6 无内置模型，但 json_to_event fallback 退化为基础 NoticeEvent
  照常分发——适配器挂 `on_notice` 即可收到。
- 关键事实（用户纠正）：NapCat.Shell 的 `main.bat` 走
  `launcher-win10-user.bat <QQ号>` → `NapCatWinBootMain.exe` 快速登录，
  本地缓存凭证静默重登、**无需扫码**——所以「登录已失效」也能全自动
  恢复（推翻「重启必停在扫码界面」的判断）。
- **watchdog.py（新）**：`NapCatWatchdog` 状态机——触发（bot_offline
  事件立即 / WS 断开 60s 宽限期后未回连）→ 杀 NapCat 进程树
  （PowerShell CIM 按父子关系只杀 NapCatWinBootMain 及子孙，**不学
  KillQQ.bat 误伤个人 QQ**）→ 按 launcher 同等环境变量带 QQ 号重启
  （独立控制台窗口，记 PID）→ 等 on_bot_connect 确认回连。节制：单
  flight + 600s 冷却 + 24h 上限 5 次；超限/回连超时/重启失败写
  `nb2_data/napcat_offline_alert.json` 哨兵 + ERROR 告警停手（防无限
  重启激怒风控），回连成功清哨兵。trigger 入口即置 restarting（亲手
  杀出的断开不误触发）；适配器 on_shutdown close() 防止退出时反而把
  NapCat 拉起来。仅 win32 动手，其他平台只告警；winreg 条件导入。
- 用户补刀：WS 断开时先 log warning（进入宽限期的提示）。
- 配置（env，Nb2Config）：`GSK_NB2_WATCHDOG`（默认 true）/
  `GSK_NB2_NAPCAT_DIR`（默认 ignore/NapCat.Shell）/
  `WATCHDOG_COOLDOWN`（600）/`WATCHDOG_MAX_RESTARTS`（5）/
  `WATCHDOG_RECOVER_TIMEOUT`（300）/`WATCHDOG_DISCONNECT_GRACE`（60）。
- 测试：tests/test_nb2_watchdog.py 14 例（成功恢复/超时告警/冷却/
  每日上限/非 Windows/未就绪/重启失败/关停停用/宽限期重连跳过/宽限期
  后触发/重启中断连忽略/事件单 flight/配置解析）。基线：821 passed,
  3 subtests passed，ruff / pyright 全绿。
- 未做：Windows toast 等推送通道（哨兵文件 + ERROR 日志兜底）；
  真实被踢后的端到端验证（需等一次风控自然发生）。

建议 commit message（用户已授权提交）：
`feat(nb2): NapCat 掉线守护——bot_offline 事件/断连宽限触发，定向杀进程树后快速登录自动恢复（冷却+每日上限+哨兵告警，不碰个人 QQ）`

## 8.43 额度健康改速率型：按全局单位时间总消耗算阈值（2026-08-02，用户定稿）

- 设计变更（用户原话）：「按单位时间的总消耗来，消耗快警告阈值升高、
  消耗慢降低，临界阈值也一样，不要每个租户分开计算，要看全局」——
  取代 §8.40 的「中位单次成本 × 调用次数」按次模型。
- **样本带时间戳**：ModelClient `_cost_samples` 改存 `(wall_ts, 元)`
  （`_finish_timing` 收口处 `time.time()`），滚动窗口仍 100 条/客户端。
- **框架中心统一算法** `quota_health.compute_burn_rate`：全租户样本
  合并 → 近 24h 窗口求和 ÷ 实际跨度折算日耗（元/天）；跨度下限 1 小时
  摊薄（启动初期宁可低估也不爆表）；未来/超窗样本剔除；无样本
  `{"count": 0}` 回落约定不变。host `_collect_cost_stats` 与
  ModelClient `cost_stats()` 共用此函数（全局/单客户端同一把尺）。
- **阈值改天级**：warn = 日耗 × 7 天、crit = 日耗 × 1 天、紫 = 日耗 / 24
  （撑不过 1 小时）；指数 = 余额/warn 封顶 100；报告字段 median_cost /
  remaining_calls → burn_per_day / remaining_days。
- **快升慢降警戒时间**（用户补刀定稿：「突然慢下来也不要瞬间降低阈值，
  不然会突然健康或突然恶化」）：`BurnRateSmoother`——上升立即生效
  （烧得快必须马上告警），下降按 6h 半衰期指数回落；host 持单例对全局
  日耗逐次平滑，另记 `raw_burn_per_day` 原始值观测。连带修掉两个瞬时
  跳变口：全天静默样本滑出 24h 窗口时不再回落静态阈值（见过样本后
  raw=0 沿半衰期衰减），稀疏样本逐个出窗的台阶下跌也被平滑吸收。
- **/status 展示**：`额度：🟡 健康指数 71（余额 ¥16.60，日耗 ¥1.63，
  约可再撑 10.2 天）`；不足 1 天显示小时（`约可再撑 12 小时`）。
- 测试：test_quota_health.py 重写为 32 例（天级四级边界/1 小时耗尽线/
  快慢烧阈值升降性质/速率窗口过滤与跨度下限/平滑器快升慢降与半衰期/
  host 全租户合并/静默衰减无悬崖/动态优先/静态回落）。
  基线：807 passed, 3 subtests passed，ruff / pyright 全绿。

建议 commit message（待用户授权后提交）：
`refactor(core): 额度健康改速率型——成本样本带时间戳，quota_health 按全租户 24h 日耗折算动态阈值（快烧升/慢烧降），耗尽线改为撑不过 1 小时，快升慢降平滑器兜底警戒时间`

