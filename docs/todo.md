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

## 8.52 watchdog 回归极简：call main.bat + 只信 WS 回连（2026-08-02，用户拍板）

- 第三轮实机失败后的自我实锤：§8.51 的「孵化期捕获 QQ pid」是我自己
  造出来的误判——`Popen.pid` 是 cmd 的 pid，却在它的直接子进程里找
  QQ（QQ 是 NapCatWinBootMain 的子进程，隔代永远找不到）；引导器
  引导完即退、cmd 跟着退，按构造必然「秒退」，与实例冲突无关。
  用户拍板：「重启，你直接让它执行我的 main.bat 就好了，不用那么麻烦」。
- **定稿模型**：安全清理（按镜像名清 NapCatWinBootMain 引导器树 +
  pause 残留 bat 窗口，绝不盲杀 QQ.exe；新旧登录冲突由 QQ 服务端
  裁决——新登录踢旧登录）→ 沉降 3s → `cmd /c "chcp 65001 >nul &&
  call main.bat"`（用户实证路径，改 bat 自动生效）→ 只信 WS 回连
  （900s 超时告警）。
- **全部砍掉**：QQ pid 捕获/tracked 状态文件（napcat_bot.json）/
  pid_alive / find_child_qq / process_died 早判 / 秒退重试——这套进程
  模型下 pid 探测必然误判，WS 才是唯一真相。
- 测试：18 例（状态机全谱 + call main.bat 命令断言 + pause 窗口清理）。
  ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`fix(nb2): watchdog 回归极简——恢复=安全清理+call main.bat+只信 WS 回连，砍掉必然误判的 pid 捕获/存活探测`

## 8.51 元租户删除（一次性脱稿生成）+ watchdog 进程模型定稿（2026-08-02，用户指导）

- 背景（用户血压时刻）：§8.47 的 enable_think_engine 实测仍有 nb2-meta
  思考引擎日志、watchdog 两轮修复后 NapCat 重启仍秒退。用户指导：
  ①不要 meta 租户，隔离性靠一次性 LLM 调用（不进 session）；②进程
  管理用 aiosubprocess。
- **深度诊断（闪退根因坐实）**：NapCatWinBootMain 是一次性引导器
  （拉起 QQ 即退，当前进程表查无此物）；QQNT 多开时进程互相收养
  （个人 QQ 652 下挂着 QQEX 23320）——**靠进程枚举根本无法区分哪个
  QQ.exe 是 bot 的**。此前所有「杀树」在 launcher 退出后全是空操作
  （roots 找不到），旧 bot QQ 一直活着，每次重启都撞单实例锁秒退。
  旁证：cache/qrcode.png 16:04 生成——昨天那次「恢复成功」其实是
  快速登录失败落到扫码页、用户手动扫的。
- **meta 租户删除**：新 `core/agent/oneshot.py` OneShotGenerator——
  每角色缓存 ModelClient + 角色系统提示词，`generate`（system+user
  一次性脱稿）与 `get_quota`（账户额度，原借元租户客户端）都走它；
  不建租户、不进任何会话与记忆。§8.47 的 enable_think_engine 全链
  （Agent/service.init/ensure_agent）回滚删除；runtime_data 里
  nb2-meta 的残留 manifest 目录已清理（下次启动不再复活僵尸租户）。
- **watchdog 进程模型定稿**：只精确管理自己拉起的实例——孵化期捕获
  launcher 的 QQ 子进程 pid 并持久化 `nb2_data/napcat_bot.json`，杀
  = `taskkill /pid <qq_pid> /t`（等死透 ≤15s）+ 按镜像名清引导器树
  + pause 残留 bat 窗口；**绝不盲杀 QQ.exe**（个人 QQ 绝对安全）。
  无追踪记录的外来实例不做盲杀盲启；秒退（外来冲突）自动重试一次，
  仍死则告警并明确提示手动清理旧实例。全部进程探测改 asyncio 子进程
  （aiosubprocess），去掉 to_thread 线程池绕行。
- 测试：watchdog 测试 23 例（精确杀传参/追踪状态读写损坏/捕获后中途
  死亡/秒退重试/flash 一次后复活 + 既有状态机用例）；oneshot 5 例
  （系统提示词装配/缓存/zh_cn 解析/缺失报错/quota 委托）；host 层
  元租户用例全部改写为不建租户断言（临时 root 避开生产 manifest）。
  合计 130 passed，ruff / pyright 全绿。
- 遗留说明：当前 bot 处于人工关停态；升级后手动 main.bat 拉起一次
  （或扫码一次），此后守护进入「可精确管理」状态。

建议 commit message（用户已授权直接提交，两条）：
`refactor(core): 元租户删除——OneShotGenerator 一次性脱稿生成（不进会话不建租户），enable_think_engine 链路回滚`
`fix(nb2): watchdog 进程模型定稿——只精确管理守护拉起的实例（追踪 QQ pid 精确杀），外来实例不盲杀盲启，探测全改 aiosubprocess`

## 8.50 watchdog 恢复可靠性三连修（2026-08-02，实机日志驱动）

- 实机现象：bot_offline → 杀树重启 → 300s 超时误报人工介入 → 6.4 分钟才
  回连（冷启动就是慢）→ 随后 WS 又断 → 冷却吞掉触发后无人再管。
- **冷却拒绝排重试（真 bug）**：冷却期吞掉的触发此前永久丢弃——bot 还
  挂着也没人重试。现在排一个到期重试（仍离线才执行，回连/关停自动取消；
  retry 自排链修复：retry 任务自身不再被自己「未完成」状态挡住）。
- **回连确认双通道**：每 5s 轮询进程树存活（`_windows_is_tree_alive`，
  PS 按 PID 静态走父子链）+ WS 回连——闪退立刻 `process_died` 告警
  （不干等超时），回连超时默认 300→900s（冷启动实测 6+ 分钟，用户
  「间隔太短」）。顺带清理旧实例卡在 `pause` 的 main.bat/launcher cmd
  窗口（杀树脚本覆盖，防用户误以为没启动再开实例互踢）。
- **单 flight 改旗标**（修测试时连环挖出的真缺陷）：`trigger` 曾把
  `asyncio.current_task()` 记为 `_recover_task`——直接 await 的调用方
  （测试/未来的调用方）会被 `close()` 误 cancel、且存活期间堵死
  `_spawn_recovery`。改为布尔旗标 + 派发时立即占位（spawn 但任务未起跑
  的窗口期连发事件也只跑一次）。
- 测试：19 例全绿（新增冷却重试触发/回连取消/process_died 立即告警/
  PS 杀树覆盖 pause 窗口；冷却测试改可控假时钟 + `_drive_trigger` 拨钟
  辅助，消除真时钟竞态）。nb2 全套 105 passed，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`fix(nb2): watchdog 恢复可靠性——冷却拒绝排到期重试（不再丢弃触发）、回连确认进程树存活+WS 双通道（闪退即报）、超时默认 900s、杀树清理 pause 残留窗口、单 flight 改旗标防误 cancel`

## 8.49 CODE_INF 首轮四连修 + 提醒可发现性（2026-08-02，用户点单「一个个干掉」）

- 背景：多 agent 全项目大调查产出 `ignore/CODE_INF.md`（88 条可疑点），
  按优先级修掉 4 条（每条独立 commit）：
- **06#1 工具注册表全局泄漏**：`registry.register()` 曾先写进程级全局表
  再回取——适配器/租户闭包（set_reminder 捕获 agent_id）被之后新建
  ToolRegistry 的 _load_builtin 吸进无关 Agent，同名互踩。修法：base.py
  抽出纯函数 `build_tool_definition`（schema 生成单源），register 改实例级
  构建，仅 @tool 装饰过的同一函数对象才复用全局定义。回归测试
  test_tool_registry_isolation.py 3 例（`e16cea2`）。
- **08#3 僵尸提醒堵配额**：重试耗尽的提醒既不投递也不删除，永久计入
  pending_count 可堵满租户配额。修法：`ReminderStore.due()` 顺带清除并
  记日志（`31c8bb1`）。
- **02#10 印象任务弱引用**：`create_task(_learn_impression)` 未持强引用，
  可被 GC 提前回收、成员在 inflight 集合永久占位。修法：模块级
  `_background_tasks` 强引用集 + done_callback 自清（`bacc0ad`，顺带
  带上此前未提交的注册结果日志）。
- **07#1 episodic 旧键崩溃**：§5.6 删情景记忆后校验层只警告、加载层未
  pop，`MemoryConfig(**data)` 裸 TypeError。修法：loader 按
  `_REMOVED_MEMORY_EPISODIC_KEYS` 丢弃（与 initiative_timer 同一招），
  回归测试 1 例（`1570a11`）。
- **提醒可发现性**（15:39 实测模型不调 set_reminder 的收尾）：工具
  docstring 加触发词（「提醒我/喊我/到点叫我/remind me」「只登记不掐
  时间」）；`_EXTRA_CONTEXTS` 追加能力提示行（约 30 token/轮，提醒启用
  时）；register_tenant_tool 结果有 debug/warning 日志（此前静默）。
  测试 1 例断言提示行注入。
- 另：§8.47 元租户轻量化经实机复现确认生效（agent.init 直调
  enable_think_engine=False → think_engine=None），用户 15:39 日志里的
  nb2-meta 思考引擎是改动前启动的旧进程。
- 增量验证：各轮定向测试全绿（tools 46 / reminders 17 / nb2 85 / config
  55 / 汇总 89 passed），ruff / pyright 全绿。

建议 commit message（本条随末个 fix 一并入库）：
`feat(nb2): 提醒可发现性——set_reminder docstring 加触发词、每轮注入能力提示行、注册结果日志化`

## 8.48 HealthCenter 健康总监控 + 砍动态阈值判定（计费保留）+ getattr 清理（2026-08-02，用户定稿）

- 起因（用户三条）：①动态阈值「每次重启刷新就又变健康，判定错误没意义」
  ——砍；②框架核心模块加 HealthCenter 总监控类，判定收口、关键条件走
  yaml；③减少自家对象上的 getattr/setattr 防御链（直接调用才优雅）。
  补充指令：计费功能保留（单价 × usage 成本采样不删）。
- **砍判定留计费**：删 `core/agent/quota_health.py`（compute_quota_health
  动态判定 + BurnRateSmoother 平滑器 + host 平滑状态全去）；保留
  ModelClient `_estimate_call_cost`/`_cost_samples` 与四个 price_* 配置键，
  `compute_burn_rate` 迁入 `core/health.py`——计量仅观测展示（/status
  日耗后缀），不再参与任何健康等级。
- **HealthCenter（新 `core/health.py`）**：健康判定统一收口；
  `evaluate_quota` 按 yaml `health:` 节静态阈值（HealthConfig：
  quota_warn_yuan 20 / quota_crit_yuan 5）出 🟢🟡🔴🟣⚫ 五级 + 指数，
  重启不漂移；loader/validator/merge 三节全接线（未知字段校验、crit ≤
  warn 跨字段校验、合并逐字段 choose）。
- **连带修 latent bug**：`ConfigMerger.merge` 此前根本没合并 repeat_guard
  /health 两节——yaml 里的自定义被静默丢回默认（repeat_guard 因默认值
  恰好可用而从未暴露）。本轮两节都补上逐字段合并。
- **getattr 清理**：host 三个聚合器（成本/延迟/记忆）从
  `getattr(getattr(agent,...),...)` 防御链改为 `agent is None` 单检 +
  直接属性访问；cmd_status 的 `getattr(config, "quota_warn_yuan"...)`
  随 env 键删除一并消失。NapCat 事件边界（pydantic extra）与
  __new__ 测试桩的防御性 getattr 保留（那是对外部/鸭子类型的正当防御）。
- **nb2 侧**：Nb2Config 删 quota_warn_yuan/quota_crit_yuan + GSK_NB2_QUOTA_*
  env 键；/status 额度行经 ctx.metadata 注入的 HealthCenter 判定，
  日耗计量附加展示：`额度：🟢 健康指数 100（余额 ¥36.50（现金 ¥30.00，代金券 ¥6.50，日耗 ¥1.63））`。
- 测试：test_quota_health.py → test_health_center.py 重写（静态五级边界/
  自定义阈值/yaml health: 节端到端加载/日耗折算/成本估算/全租户合并）；
  test_nb2_commands.py 走 HealthCenter；删 env 额度键解析测试。增量
  137 passed，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`refactor(core): HealthCenter 健康总监控——额度判定收口 yaml health: 节静态阈值，砍动态阈值判定（计费计量保留仅观测），ConfigMerger 补 repeat_guard/health 合并，host getattr 链清理`

## 8.47 元租户轻量化：nb2-meta 不装配思考引擎（2026-08-02，用户点单）

- 问题（用户发现）：`nb2-meta` 元租户只用于脱稿生成（群友第一印象/
  破例判定），却按全功能 Agent 装配——ThinkEngine 每 30 分钟空转长期
  思考，纯烧 token。用户问「第一印象生成下沉到适配器内可以吗」。
- 结论：不必下沉——元租户的价值正是隔离（生成不进任何用户会话）；
  贵的是它白背的 ThinkEngine，关掉即可，空转成本归零。
- 实现：`Agent.__init__` 新增 `enable_think_engine`（默认 True，start
  时跳过装配）；`RuntimeService.init` 同名参数透传（RPC schema 按签名
  自动生成，网络路径免费获得）；`RuntimeHost.ensure_agent` 加
  `disable_think_engine`（与 disable_initiative 同风格，默认 False
  不影响现有调用方）；`generate_meta_text` 传 True——元租户现在
  无思考引擎、无主动定时器，只在被调用时工作。
- 测试：test_nb2_adapter.py 增 2 例（disable_think_engine 参数透传+
  默认不带键后向兼容 / generate_meta_text 元租户轻量化）。增量验证
  48+21 passed，ruff / pyright 全绿。

建议 commit message（待用户授权后提交）：
`feat(runtime): 元租户轻量化——Agent/init/ensure_agent 加 enable_think_engine 开关，nb2-meta 脱稿租户不再空转思考引擎`

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

