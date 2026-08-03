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

## 8.65 @ 由因果判定驱动：reply_focus 注意力种类（2026-08-03，用户定稿语义）

- 起因一（实机翻车）：§8.64 的「批次全员都 @」+ 模型自己写的文本 @
  叠加，QQ 端显示成「@帕秋莉 @帕秋莉」双 at。
- 起因二（用户拍板）：@ 目标选择是**因果关系判断**（这轮消息谁冲着
  bot 来：提问/委托/等回应），代码启发式判断不了，该交给
  AttentionThings；且「不要经常 at 人」。
- 定稿机制：
  - 新注意力种类 `reply_focus`（plugin 注册，prompt 在框架
    `build_reply_focus_prompt`）：LLM 判定本轮因果焦点名单，
    无焦点（普通闲聊）不出 verdict 就不 @；
  - `AttentionThings.inspect` 支持 `only=` 种类过滤——私聊只跑
    reminder 不跑 reply_focus（@ 无意义，省一次判定调用）；
  - QQ 端真 at 只拼焦点名单（`_resolve_focus_targets`：本批发言人
    映射优先，群名片缓存兜底）；模型自己开头写的文本 @焦点由
    `_strip_leading_mentions` 剥掉，只留真 at 段（治双 at）；
  - 「批次全员都 @」的 `batch_at_targets` 启发式已删除。
- 记忆侧（§8.64 的 `@昵称` 前缀）不变：那是给模型看的归因提示，
  不 @ 人。
- 测试：test_nb2_reply_focus.py 新文件 12 例（解析/剥除/解析目标），
  test_attention.py 补 only= 过滤例；相关 113 例全绿，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`feat(nb2): @ 由注意力因果判定驱动——新增 reply_focus 种类（谁冲着 bot 来才 @，闲聊不 @），inspect 支持 only 种类过滤；真 at 与模型文本 @ 去重（修双 at），废弃批次全员 @ 启发式`

## 8.64 回复对象标记：@ 关联治张冠李戴（2026-08-03，用户实机发现 + 拍板 @ 方案）

- 起因：群聊实机里幽幽子把 11 分钟前「随便你了，多打一个不在话下」
  （语境是转告八云紫）缝到婚事话题上答「这不就是答应了嘛」——多人
  快节奏对话中，用户消息带【昵称】前缀但 bot 自己的回复在工作记忆里
  是裸文本，模型全靠推理归因「那句话是回谁的」，kimi-k2.5 串错。
- 第一版教训（（对 某人）前缀，已实机翻车）：模型看到记忆里助手消息
  全带该前缀后学着自产，口吃成 `（对 枫叶）×4` 漏进投递文本——
  **生造标记格式会被模型模仿泄漏**。用户拍板：关联直接用 at。
- 定稿机制（@ 原生约定）：
  - QQ 投递：`_process_batch` 群聊回复首条分段拼**真 at 段**
    （`batch_at_targets` 取批次发言人 QQ 去重保序），回的是谁一目了然
    且能提醒到人；私聊不 @。
  - 工作记忆：`on_message_sent` 把助手消息存为 `@帕秋莉 @赤色杀人魔 …`
    （触发消息的【昵称】行首标记 → `reply_to`，与入站 at 渲染的
    `@昵称` 同款）；模型自己开头写了 @目标 时不重复加——模仿 @ 是
    良性、可读的，无需剥除。
  - 私聊/控制台/World 无标记自然为空，行为不变。
- 顺带厘清另两类「幻觉」不治本但定性：设定错误（觉妖怪=亡灵）靠
  角色卡常识锚点；装傻卖萌是特性。
- 测试：test_reply_to_tagging.py（提取 5 例 + 记忆写入 3 例）+
  test_nb2_pending.py 的 batch_at_targets 3 例；相关 79 例全绿，
  ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`feat(nb2): 回复对象 @ 关联——群聊回复首段拼真 at 段（批次发言人去重），助手消息在工作记忆带 @昵称 前缀，修多人快节奏对话的张冠李戴（取代会模仿泄漏的（对 某人）方案）`

## 8.63 蒸馏永不触发根因：resume_session 幂等修复（2026-08-03，用户实机发现）

- 现象：nb2 跑了 29 分钟、27 次内心思考，`/status` 记忆仍是
  0 话题 / 0 珍贵记忆；配置 `distill_enabled: true`、`distill_turns: 10` 全正常。
- 根因链：nb2 每条消息带 session_id → `service.send_message:1426`
  `_activate_tenant_session` **每条消息都调** → `Agent.resume_session`
  不短路，当前会话也照走 `_reset_session_scoped_state` →
  `update_semantic_memory` 把 `_distill_pending_turns` 清零——
  计数永远到不了 10，蒸馏在 nb2 路径上**从未触发过**。
- 连带受害（同根同愈）：`_half_completion` 每条消息被清空（半截续说
  在 nb2 也是坏的）；语义记忆管理器每条消息重建（日志满屏
  「语义记忆初始化」「会话已恢复」的来源）。
- 修复（一处框架级）：`resume_session` 对「已是当前会话」幂等返回，
  不重置、不重发 SESSION_RESUMED；真正切换会话照旧走重置路径。
- 测试：`test_resume_current_session_is_idempotent`（重复 resume 保留
  记忆实例缓存 + 半截状态；切换会话照旧重置）。相关 107 例全绿，
  ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`fix(agent): resume_session 对当前会话幂等——修掉 Runtime 每条消息重置蒸馏计数导致蒸馏永不触发（连带修复半截续说被每条消息清空、语义记忆每消息重建）`

## 8.62 守护 QR 登录回退：QQ 未知不再 not_ready 告警（2026-08-02，用户拍板）

- 起因：启动引导触发守护时若 bot QQ 未知（二级密码登录会撞腾讯验证码，
  用户选择手动扫码），直接 not_ready 告警——用户：「没有这个还是走重启
  （因为终端会弹出扫码登录），不要报错」。
- `_trigger_inner` 的 not_ready 闸收窄到只剩「NapCat 目录未知」；
  QQ 未知照常走 清理→启动 流程。
- `_windows_launch_napcat` 的 bot_qq 改 Optional：None 时不带账号位置
  参数、不 set ACCOUNT / 密码回退变量——NapCat 终端弹扫码登录，
  扫码回连后 `notify_connected` 照常记住 QQ 号。
- 重启日志分两种口吻：带 QQ（快速登录）/ 未配 QQ（提示去终端扫码）。
- 测试：not_ready 例收窄为仅缺目录；新增「QQ 未知 → launch(q=None) →
  扫码回连 → restarted 且无哨兵文件」状态机例 + 「None 时不含
  ACCOUNT/密码变量」命令行例。22 passed，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`fix(nb2): 守护 QQ 未知不再 not_ready 告警——照常启动 NapCat 走终端扫码登录（bot_qq 可选），仅目录未知才停手`

## 8.61 提醒全量重写：判定全权 LLM（ThinkEngine 范式）+ 取消机制（2026-08-02，用户砍令）

- 起因（用户血压时刻）：parse_when 只认阿拉伯数字，「一分钟后」「十点钟」
  等中文数字全挂——用户：「你这个 reminders 仍然全用 re，都说了全权交给
  注意力类了……把这功能给我砍掉，全部重写，全用 AttentionThings 类来记，
  和 ThinkEngine 如出一辙，就是为了解决 tool call 调用问题，全靠主模型会
  受到上下文注意力的影响」。
- **全砍**：`parse_when` 及全部时间正则（含我刚补的中文数字补丁——方向
  就错了）、`set_reminder` 工具（_build_reminder_tool + 按租户注入 +
  能力提示行 + register_tenant_tool 调用）。
- **新形态**：判定 prompt 给当前时间，LLM 输出三态意图——`reminder`
  （附 **ISO 8601 绝对到点时间**）/ `cancel`（用户补刀「不要提醒了」，
  附 all/latest 范围）/ `none`；代码只做 ISO 解析与范围校验（30s~30d），
  **判定层零代码判断**。时间太远/太近/缺失 → 「待确认」反问。
- **取消机制**：ReminderStore 新增 pending / cancel_latest / cancel_all；
  取消同样走代办 + 口吻转告（含「没有待办」如实回答）。
- 测试重写：26 例（存储 CRUD/取消/持久化、三态判定解析、代办登记/取消/
  待确认、投递全谱）。全套 122 passed，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`feat(attention): 提醒全量重写——判定全权 LLM（三态 intent + ISO 绝对时间，砍 parse_when 正则与 set_reminder 工具），新增取消机制（cancel_latest/cancel_all 代办）`

## 8.60 注意力判定去代码化：全量 LLM 判定 + 时间待确认补洞（2026-08-02，用户定稿）

- 起因：22:25 代办已成功一次，但 22:34 花式说法（「两分钟后…」截断形态）
  仍漏——用户：「别代码里判断了，多个 llm 自主判断又不会死」。
- **预筛退役**：`_ReminderAttentionKind.candidate` 恒真——每条消息都过
  LLM 判定（candidate 钩子保留给未来种类）；判定 prompt 加「口语/缩写/
  半截话都算，像就抓别保守」的抗漏指令。
- **补最后一洞**：判定为提醒请求但 parse_when 看不懂时间时，不再静默
  放过（= 又一次口头答应没下文），改注入「待确认」上下文让角色用口吻
  问清具体时间。
- 测试：28 例（预筛恒真/待确认注入/既有全谱）。ruff / pyright 全绿。
- 成本说明：每轮回复多一次短 JSON 判定调用（用户明确接受）。

建议 commit message（用户已授权直接提交）：
`feat(attention): 判定去代码化——预筛退役全量 LLM 判定（prompt 抗漏指令），提醒时间看不懂时注入待确认上下文补最后漏洞`

## 8.59 AttentionThings 注意力事务管线（2026-08-02，用户定稿「足够通用」）

- 起因：群聊提醒工具连续漏调（私聊灵、群聊六连「口头答应但不登记」），
  提示行强化也救不回来——用户拍板做注意力机制帮助模型注意。
- 设计（用户定稿通用性）：`core/agent/attention.py` 的 `AttentionThings`
  管线——**候选预筛（正则零成本）→ 命中才花一次 LLM 判定（一次性脱稿
  OneShotGenerator，不进会话）→ 输出 AttentionVerdict**；事务种类
  `AttentionKind`（candidate/judge_prompt/parse 三段契约）可注册扩展，
  提醒是首个。判定失败静默降级，绝不拖垮主回复。
- 关键决策：**代办式处置**（我提出并获认可的方向）——判定命中后管线
  直接 `_register_reminder` 登记进存储并注入「已代办」上下文，模型只
  用口吻转告「记下了」，不再依赖它的工具纪律。`set_reminder` 工具与
  代办共用同一登记函数（结构化 `_ReminderOutcome`）。
- nb2 接线：`_process_batch` 合并后跑 `_inspect_attention`；
  `GSK_NB2_ATTENTION`（默认 true）；启动日志加「注意力事务=开/关」。
- 测试：core 管线 6 例（预筛免调用/产出 verdict/解析 None/判定失败静默/
  停用与空白/重名忽略）+ nb2 种类与代办 5 例（预筛词/解析合法非法/代办
  登记入库+指令注入/坏时间降级）。合计 28+passed 定向，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`feat(attention): AttentionThings 注意力事务管线——候选预筛+脱稿判定+代办式处置，reminder 首个种类（判定命中直接登记，不依赖主模型工具纪律）`

## 8.58 快速登录形式修正：位置参数 + 密码回退显式注入（2026-08-02，端到端实测定案）

- 用户报：配置了 NAPCAT_QUICK_PASSWORD_MD5 但 NapCat 仍说「未配置回退
  密码」。端到端实测（本机真启动两轮）：①`-q 3779163297` 形式 NapCat
  4.18.13 直接打回「**没有 -q 指令指定快速登录**」——§8.53 内化的
  `-q` 是错误形式，main.bat 的位置参数才是被验证的形态（昨天 3 秒恢复
  用的就是它）；②改正为位置参数后日志逐行证明链路全通：「正在快速登录
  → 登录态已失效 → **正在尝试密码回退登录**（MD5 生效）→ 正在密码登录
  → 需要验证码」。密码登录触发腾讯验证码属预期（此前已提示），需人工
  完成一次 proofWater 验证。
- 定稿：watchdog 启动命令 = `chcp 65001 >nul && set ACCOUNT=<QQ> &&
  set NAPCAT_QUICK_PASSWORD_MD5=<已加载的 dotenv 值> && call
  launcher-win10-user.bat <QQ>`（未配密码则省略对应 set）。
- env 模板补 ACCOUNT / NAPCAT_QUICK_PASSWORD_MD5 / NAPCAT_QUICK_PASSWORD
  三个注释键；测试 20 例（位置参数/无 -q/密码 set 显式注入/未配省略）。
  ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`fix(nb2): 快速登录改位置参数形式（-q 形式 NapCat 4.18.13 不识别），密码回退变量显式 set 进启动命令`

## 8.57 适配器基类统一：RuntimeAdapter 升格 ABC，BaseBackend 删除（2026-08-02，用户点单）

- 问题：架构上有 `BaseBackend`（ABC，start/stop 无参）与 `RuntimeAdapter`
  （Protocol，name/start(host)/stop）两套并行概念，且派生类大半不继承
  （nb2 裸奔、web_server 全是模块级函数没有类）。用户拍板：「干脆全用
  新的概念（RuntimeAdapter）」。
- **RuntimeAdapter 升格 ABC**（`GensokyoAI/adapters/__init__.py`）：
  `name` + `start(host: RuntimeHost | None = None)` + `stop()`；
  run_adapters/serve_adapters 签名不变。`backends/base.py`（BaseBackend）
  删除；`GensokyoAI/__init__.py`、`backends/__init__.py`、
  `commands/context.py`（TypeVar bound）全部换挂。
- **派生全部继承**：ConsoleAdapter / WorldConsoleBackend / Nonebot2Adapter
  改挂 RuntimeAdapter（console 的 start 加 host=None 兼容参数，nb2 无
  host 时明确报错）；web_server 新增 `WebAdapter`（封装 create_app +
  AppRunner/TCPSite 生命周期，独立入口自建 service、组装入口复用宿主
  service），main.py 改走 WebAdapter（行为不变）。
- 连带修 §8.48 getattr 清理的两处假桩失形（test_runtime_multi_user 的
  fake agent/client 补 semantic_memory/_cost_samples 同形字段）。
- 测试：test_web_adapter.py（继承三角言 + /health 200 生命周期 + stop
  可重入）、test_abstract_contracts 改 RuntimeAdapter 不可实例化。
  增量 89 passed，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`refactor(adapters): 适配器基类统一——RuntimeAdapter 升格 ABC（name/start(host=None)/stop），BaseBackend 删除，console/nb2 继承补齐，web_server 新增 WebAdapter 封装 runner 生命周期`

## 8.56 日志摘除改官方姿势：logger.remove(logger_id)（2026-08-02，用户给文档）

- 用户贴 NoneBot 官方文档：默认日志处理器应从 `nonebot.log` 导
  `logger_id` 精确 `logger.remove(logger_id)`——「不用设置什么环境
  变量，不用把 level 设置 critical 级别」。
- 改法：adapter.start 的日志处理从「LOG_LEVEL=CRITICAL 压制 +
  全量 logger.remove() ×2」改为 init 前后各 `logger.remove(logger_id)`
  （suppress ValueError）——只摘 nonebot 默认 sink，不动我们自己的
  sink；LOG_LEVEL 环境变量 hack 删除。setup_logging 幂等（只摘自己
  追踪的 handler），init 后调用天然安全。本机实测 logger_id=1 可用。
- 顺带：上一轮用户手改 import 的 typo（OneBotV11Adapterf）已修回。
- 验证：ruff / 定向 56 passed。

建议 commit message（用户已授权直接提交）：
`refactor(nb2): 日志处理改官方姿势——logger.remove(logger_id) 精确摘除 nonebot 默认 sink，删除 LOG_LEVEL=CRITICAL 环境变量 hack`

## 8.55 首次运行播种（新人体验）+ nb2 日志格式去重（2026-08-02，用户点单）

- 起因：新人直接 `uv run python -m GensokyoAI.backends.nb2` 没有配置会
  死在半路（nonebot 缺 DRIVER 抛 server_app 错）——用户「新人体验要做
  好」。调查发现 CLI 早有播种（cli/main.py 从模板生成 config/local.yaml）
  但适配器入口没有，nb2 env 也没有。
- **播种统一**：`core/config_dirs.ensure_local_config`（CLI 原实现上收，
  返回 (path, created) 供调用方自行展示）；`serve_adapters` 入口播种——
  CLI 与全部适配器入口的 `config/local.yaml` 首次运行都会自动生成
  （只播种一次，用户改过的绝不覆盖）。nb2 侧 `seed_local_env` 从
  `tmp/nb2.env.example` 生成 `config/nb2/local.env`；解析优先级
  `local.env` → `.env` → 根 `.env` 兜底（local.* 与 local.yaml 同风格，
  用户点名的命名）。
- **日志格式去重**（用户实机反馈）：`adapter.start` 曾在 nonebot.init
  前就打日志——nonebot sink 与项目 sink 并存，一条消息两种格式各打
  一遍。改为：加载阶段静默 → init 前后各 `logger.remove()` 一次
  （suppress ValueError）→ setup_logging（失败保底加回 stderr sink，
  顺带消化 CODE_INF 01#6）→ 之后统一单一格式发声。
- 测试：6 例（播种一次/不覆盖/模板缺失不炸/local.env 优先/播种后
  resolve 命中/CLI 既有用例回归）。增量 91 passed，ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`feat(config): 首次运行播种——config/local.yaml 与 config/nb2/local.env 缺失时从模板自动生成（只播种一次）；nb2 日志格式去重（init 前后清 sink，延迟到 setup 后发声）`

## 8.54 config/{adapter}/ 私有配置目录约定（2026-08-02，用户定稿）

- 设计（用户两轮纠偏后定稿）：`config/{adapter_name}/` 只是各适配器的
  **私有配置目录**——格式（env/yaml/json/toml）与加载器完全归适配器
  自己（有自有加载器框架让渡），框架只做路径约定、零格式耦合。
  （第一版我曾提议框架统一 yaml schema，被用户点破「那还是耦合」。）
- **框架侧**：`core/config_dirs.py` 一行 helper `adapter_config_dir`
  （经 core/config.py 导出），职责到此为止。
- **nb2 侧**：`resolve_env_file`（config/nb2/.env 优先、根 .env 兜底并
  打迁移提示）；adapter.start 按解析结果 `load_dotenv` +
  `nonebot.init(_env_file=...)`——**NoneBot 自己的 DRIVER/HOST/PORT/
  ONEBOT_ACCESS_TOKEN 也住进 config/nb2/.env**，根 .env 彻底退役；
  显式 env_file 参数向后兼容。`Nb2Config.from_env` 一行未动。
- gitignore 的 `.env` 规则天然覆盖 config/nb2/.env；模板
  tmp/nb2.env.example 头部改为「复制到 config/nb2/.env」。
- 测试：4 例（目录解析/私有优先/兜底标记/缺失 None/显式透传）。
  增量 71 passed，ruff / pyright 全绿。
- 用户迁移：把根 `.env` 内容挪进 `config/nb2/.env` 即可（一行 mv）。

建议 commit message（用户已授权直接提交）：
`feat(config): config/{adapter}/ 私有配置目录约定——框架只给目录不管格式，nb2 配置（含 NoneBot 自身键）迁入 config/nb2/.env，根 .env 兜底带迁移提示`

## 8.53 watchdog 启动内化：硬编码 launcher 内容 + GSK_NB2_BOT_QQ（2026-08-02，用户点单）

- 用户：main.bat 是自建的别人没有——把内容硬编码进去、QQ 号从配置注入。
  （中途我又复刻了一版 env/注册表/loadNapCat.js，被用户二次拍醒：
  「直接 `launcher.bat -q {qq}` 不就好了」——bat 是 NapCat.Shell 发行
  自带，一行 call 就够。）
- 定稿：`_windows_launch_napcat` = `cmd /c "chcp 65001 >nul && call
  launcher-win10-user.bat -q <QQ号>"`（cwd=NapCat.Shell，独立控制台）；
  QQ 号新增 `GSK_NB2_BOT_QQ` 配置（未配由首次连接 self_id 兜底）；
  清理残留：winreg/_resolve_qq_path/env 复制/loadNapCat.js 全删，
  tmp/ 下三个诊断 ps1 一并删除。
- 测试：19 例（命令断言含 `-q 3779163297`、配置注入免首连即可恢复、
  既有状态机）。ruff / pyright 全绿。

建议 commit message（用户已授权直接提交）：
`feat(nb2): watchdog 启动内化——硬编码 launcher-win10-user.bat -q 快速登录（不依赖自建 main.bat），bot QQ 号走 GSK_NB2_BOT_QQ 配置注入`

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

