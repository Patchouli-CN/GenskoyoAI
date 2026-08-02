# NoneBot2 QQ 适配器部署指南

通过 NoneBot2 + OneBot 11 协议，把 QQ 群 / 私聊消息桥接到 GensokyoAI 角色，
让角色以 QQ 机器人的形式在群里陪聊。

## 架构

```text
QQ 用户 ↔ QQ 服务器 ↔ NapCat（协议端，OneBot 11）
                          │ 反向 WebSocket
                          ▼
            GensokyoAI nb2 适配器（NoneBot2 进程）
              └── RuntimeHost ── 进程内直接调用 ──► RuntimeService
                  （多租户：agent_id = qq-group-<群号> / qq-user-<QQ号>）
```

- **单进程**：适配器与 Runtime 同仓库同进程，经 `RuntimeHost` 以网络主体上下文
  直接调用 `RuntimeService`（与 `tests/test_runtime_multi_user.py` 相同的驱动方式）。
  没有 HTTP/WS 绕路、没有鉴权配置、没有第二个进程——租户隔离、资源闸、幂等账本、
  revision 乐观锁全部来自 RuntimeService 本身。
- **协议端**推荐 NapCat（go-cqhttp 已停止维护）；反向 WebSocket 由 NoneBot 起服务、
  NapCat 主动连入，自动重连最稳。
- **主动消息**：适配器通过 `create_event_subscription` 拿到租户事件的 `asyncio.Queue`
  （进程内「推送」），角色主动开口（对话欲四维评估 → INITIATIVE_SPEAK）时实时投递到
  对应群 / 私聊。

## 安装

适配器依赖是可选项（不装不影响 CLI / Runtime 正常使用）：

```bash
uv sync --extra nb2
# 或
pip install -e ".[nb2]"
```

## 配置

**适配器私有配置目录**（`config/{adapter_name}/` 约定）：nb2 的全部配置
（含 NoneBot 自己的 `DRIVER`/`HOST`/`PORT`/`ONEBOT_ACCESS_TOKEN`）都住在
`config/nb2/`——框架只约定目录，格式与加载器归适配器自己
（nb2 现行为 dotenv；以后换 yaml/toml 是适配器内部的事，核心零感知）。

```bash
mkdir -p config/nb2 && cp tmp/nb2.env.example config/nb2/local.env
```

- **首次运行自动播种**：`config/nb2/local.env` 不存在时启动会从
  `tmp/nb2.env.example` 生成一份（只播种一次，绝不覆盖）；框架侧
  `config/local.yaml` 同理（CLI 与适配器入口都会播种）。
- 解析优先级：`config/nb2/local.env` → `config/nb2/.env` → 项目根 `.env`
  （兜底，启动打迁移提示）；`config/nb2/*.env` 已在 .gitignore 的
  `.env` 规则覆盖下，不会入库。
- 日志统一走 GensokyoAI 体系：nonebot/默认 sink 在 nonebot.init 前后
  各清一次，我们自己的日志一律单一格式（不再有双格式重复行）。

关键配置项（完整注释见 `tmp/nb2.env.example`）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `DRIVER` / `HOST` / `PORT` | fastapi / 127.0.0.1 / 8080 | NoneBot 自身监听（反向 WS 服务） |
| `GSK_NB2_CHARACTER` | `KirisameMarisa` | 所有群 / 私聊共用的角色 |
| `GSK_NB2_DATA_DIR` | `nb2_data` | 适配器数据目录（群→会话映射表） |
| `GSK_NB2_ROOT_DIR` | 当前工作目录 | GensokyoAI 项目根（characters/config 解析基准） |
| `GSK_NB2_INITIATIVE` | `true` | 角色主动发言；`false` 则停用各租户主动定时器 |
| `GSK_NB2_SPLIT_REPLY` | `true` | 回复按行拆成多条短消息发送（QQ 群聊风格） |
| `GSK_NB2_STRIP_RP_STYLE` | `true` | 发送前清洗 RP 标记（`*动作*`、`「」`），确定性生效 |
| `GSK_NB2_SENDER_LABEL` | `true` | 群聊消息注入 `【昵称】` 说话人标记（私聊不标） |
| `GSK_NB2_MEMBER_MEMORY` | `true` | 群友印象：首轮交谈后生成第一印象，之后随消息注入 |
| `GSK_NB2_QUOTE_CONTEXT` | `true` | 引用回复时取原消息拼成 `（引用 昵称：…）`，让角色看到被引用的内容 |
| `GSK_NB2_OWNER_QQ` | 空 | OWNER 级指令白名单（逗号分隔 QQ 号）；仅影响 OWNER 级指令 |
| `GSK_NB2_EXTRA_PROMPT` | 内置群聊风格要求 | 随每条回复注入的附加要求；留空用默认，可改写成自己的约束 |
| `GSK_NB2_GROUP_WHITELIST` | 空（不限） | 逗号分隔的群号白名单 |
| `GSK_NB2_WATCHDOG` | `true` | NapCat 掉线守护：被踢下线/断连后杀进程树并快速登录重连 |
| `GSK_NB2_NAPCAT_DIR` | `ignore/NapCat.Shell` | NapCat.Shell 目录（相对 root_dir/cwd 或绝对路径） |
| `GSK_NB2_WATCHDOG_COOLDOWN` | `600` | 两次自动重启的最小间隔（秒） |
| `GSK_NB2_WATCHDOG_MAX_RESTARTS` | `5` | 24h 内自动重启上限，超限告警停手 |
| `GSK_NB2_WATCHDOG_RECOVER_TIMEOUT` | `900` | 重启后等待回连的超时（秒），超时告警 |
| `GSK_NB2_WATCHDOG_DISCONNECT_GRACE` | `60` | WS 断开的回连宽限期（秒），NapCat 自己连上就不动 |
| `GSK_NB2_BOT_QQ` | 空（首连 self_id 兜底） | 掉线守护快速登录的 QQ 号（`launcher-win10-user.bat -q <QQ号>`） |
| `GSK_NB2_REMINDERS` | `true` | 到点提醒：角色经 `set_reminder` 工具接活，到点 @ 人用自己的口吻说出 |
| `GSK_NB2_REMINDER_MAX_PER_TENANT` | `20` | 每个群/私聊的待办提醒上限（防滥用烧 token） |
| `GSK_NB2_ATTENTION` | `true` | 注意力事务：候选消息经一次性 LLM 判定，待办直接代办登记 |

## 注意力事务（AttentionThings）

工具调用救不了主模型的上下文注意力（群聊噪声下「口头答应但不登记」）。
注意力事务是独立管线（`core/agent/attention.py`）：**每条消息都经一次性
LLM 判定（脱稿不进会话）→ 命中待办直接代办**。`AttentionKind`
（预筛 + 判定 prompt + 输出解析）可注册扩展，与具体事务零耦合；
判定失败一律静默降级为普通回复。成本：每轮回复多一次短 JSON 判定调用
（用户接受换取零漏判）。

> `/status` 的额度健康判定阈值不走 env——统一在 yaml `health:` 节
> （`quota_warn_yuan` / `quota_crit_yuan`，框架 HealthCenter 消费，静态阈值
> 重启不漂移）；`/status` 同时展示的日耗是计费计量（单价 × usage），仅观测、
> 不参与判定。

## 到点提醒（Reminder）

对 bot 说「10 分钟后提醒我吃饭」「明天早上 8 点喊栗子起床」「不要提醒了」
即可。**唯一登记通道是 AttentionThings 注意力管线**（判定全权 LLM，
ThinkEngine 范式——没有 set_reminder 工具、没有正则解析，主模型上下文
注意力影响不到登记）。链路：

1. **判定**：每条消息经一次性 LLM 判定（脱稿，给当前时间）输出三态意图
   ——`reminder`（请求提醒，附 ISO 8601 绝对到点时间）/ `cancel`（取消，
   附 all/latest 范围）/ `none`。
2. **代办**：命中请求 → 直接登记进 `nb2_data/reminders.json`（重启不丢），
   注入「已代办」上下文，角色用口吻转告；命中取消 → 按范围取消最近一条/
   全部待办并转告；时间太远/太近/缺失 → 注入「待确认」上下文反问。
3. **投递**：30s tick 扫到点项 → 走该租户会话让角色**用自己的口吻**生成
   提醒文本，群聊 @ 目标分段发送、私聊直发；失败重试约 20 分钟放弃。

## 掉线守护（NapCat Watchdog）

账号被腾讯风控踢下线（NapCat 日志 `[KickedOffLine] 你的账号当前登录已失效`）时，
NapCat 会经 OneBot 推送 `bot_offline` 通知事件；适配器收到后自动恢复：

1. **触发**：`bot_offline` 事件立即触发；WS 断开则等
   `GSK_NB2_WATCHDOG_DISCONNECT_GRACE` 宽限期（NapCat 自己重连上就不动）。
2. **安全清理**：按镜像名清 NapCatWinBootMain 引导器树 + 旧实例卡在
   `pause` 的 bat 窗口。**绝不盲杀 QQ.exe**——QQNT 多开进程互相收养，
   枚举无法区分 bot 与你的个人 QQ；新旧登录冲突由 QQ 服务端裁决
   （新登录踢旧登录）。
3. **启动**：`call launcher-win10-user.bat <QQ号>`（**位置参数**——`-q`
   形式 NapCat 4.18.13 不识别；launcher bat 是 NapCat.Shell 发行自带）；
   QQ 号取 `GSK_NB2_BOT_QQ`（未配用首次连接的 self_id）。命令行同步显式
   `set ACCOUNT` + `set NAPCAT_QUICK_PASSWORD_MD5`（在 `config/nb2/.env`
   里配置后，登录态被风控作废时 NapCat 自动密码回退重登——密码登录可能
   触发腾讯验证码，需人工完成一次），独立控制台、UTF-8 不乱码。
4. **确认**：只信 WS 回连（默认 `GSK_NB2_WATCHDOG_RECOVER_TIMEOUT` 900s，
   冷启动实测 6+ 分钟），超时告警（哨兵 + ERROR）。
   ※ 不做基于 pid 的存活探测：引导器拉起 QQ 即退，这类探测必然误判。
5. **节制**：冷却 `GSK_NB2_WATCHDOG_COOLDOWN`（冷却期吞掉的触发会**排到期
   重试**，回连/关停自动取消，不会丢弃）+ 24h 上限
   `GSK_NB2_WATCHDOG_MAX_RESTARTS` 次；超限/超时写
   `nb2_data/napcat_offline_alert.json` 哨兵停手等人（防无限重启激怒
   风控）；回连成功自动清除哨兵。仅 Windows 生效，其他平台只告警；
   适配器正常关停不会误触发。

内置的默认附加要求（`GSK_NB2_EXTRA_PROMPT` 留空时生效）：

> 你在 QQ 群聊/私聊里聊天，不是在写角色扮演小说：回复要简短、口语化、像真人发消息；
> 每句话单独占一行（系统会按行拆成多条消息依次发送）；
> 不要用 *星号* 或括号描写动作、表情、心理活动；不要用「」等台词引号，直接写说的话；
> 一次回复最多三句话。

`.env` 与 `nb2_data/` 已加入 `.gitignore`，不会入库。
模型与 API key 沿用项目根的 `config/local.yaml`（与 CLI 相同），无需重复配置。
日志统一走 GensokyoAI 体系（格式 + 第三方噪音过滤）：启动时会移除 nonebot 自带的
loguru sink，nonebot 的 WARNING+ 仍经我们的 sink 显示。是否显示到控制台与级别沿用
`local.yaml` 的 `log_console` / `log_level`（控制台盯梢时保持 `log_console: true` 即可），
也可用 `GENSOKYOAI_LOG_CONSOLE` / `GENSOKYOAI_LOG_LEVEL` 环境变量临时覆盖。

## 启动

```bash
# 1. 启动适配器（内含 Runtime，一个进程搞定）
python -m GensokyoAI.backends.nb2

# 2. 启动 NapCat，配置反向 WebSocket：
#    ws://127.0.0.1:8080/onebot/v11/ws
```

### 自定义组装（适配器约定）

适配器实现 `GensokyoAI.adapters.RuntimeAdapter` 协议（`start(host)` / `stop()`），
由 `run_adapters` 统一创建进程内 `RuntimeHost` 并托管生命周期；nb2 是首个实现者。
等价手写入口：

```python
from GensokyoAI.adapters import run_adapters
from GensokyoAI.backends.nb2.adapter import Nonebot2Adapter

run_adapters(Nonebot2Adapter())  # 想挂几个适配器就传几个
```

第三方适配器包（如 `gskai-nb2`）只需依赖 GensokyoAI 并实现同一协议即可接入；
`RuntimeHost`（`GensokyoAI/runtime/host.py`）的方法签名属于公开契约。
适配器还可以用 `host.register_adapter_tool(func)` 把工具函数注入所有租户
Agent（schema 由函数签名+文档串生成），让 AI 回调适配器能力——nb2 的
`update_member_impression` 就是范例。

## 行为说明

- **触发**：群聊需 @bot（或回复 bot 的消息）；私聊全部响应。纯表情 / 图片不回；
  `/` 前缀为 bot 指令（不进会话）。指令采用四级权限模型
  `VISITOR < USER < ADMIN < OWNER`：`OWNER`= `GSK_NB2_OWNER_QQ` 名单、
  `ADMIN`= QQ 群管理/群主、`USER`= 普通群成员/私聊、`VISITOR`= 身份无法核实的
  最低信任级；当前指令：`/quota`（`/额度`，USER 级，查询 Provider 余额）、
  `/status`（`/状态`，USER 级，查看负载水位/开户数/处理中/闸门用量/内心思考延迟）、
  `/help`（`/帮助`，VISITOR 级，按调用者权限列出可用指令）。
- **说话人归属**：群聊多对单场景下，消息正文自动带 `【群名片/昵称】` 前缀
  （注入前经 `sanitize_display_name` 净化：去换行/括号、限长 24，防群名片伪造指令），
  角色因此能分清每轮是谁在说、并用昵称称呼对方；私聊不加标记。
  消息中 @ 其他人的段会转译为 `@昵称` 文本（@bot 自身的段丢弃、`@全体成员` 特判），
  昵称优先取缓存、未命中调 `get_group_member_info`，兜底为 QQ 号——
  角色能看懂「你认识 @某某 吗」这类指代。
- **引用回复原文**：消息里的引用段（reply）会经 `get_msg` 取回被引用消息，
  以 `（引用 昵称：…）`（截断 120 字）拼进文本——A 引用 B 的话 @bot 问「你认识她吗」
  时角色看得到 B 说过什么；取不到（如 NapCat 缓存没有）则静默跳过。
  可用 `GSK_NB2_QUOTE_CONTEXT=false` 关闭。
- **复读防护（烦躁模型）**：同一用户连续复读/刷屏时角色会烦——连击 3 次注入厌烦
  （回复转冷淡），5 次让角色当面表态后进入 10 分钟「不理」冷却；冷却期复读直接丢弃
  （零 token），有新意的内容则由 LLM 以角色性格裁决：消气原谅 / 破例回一句 / 继续不理。
  阈值与冷却在全局配置 `repeat_guard` 节（`warn_streak` / `mute_streak` / `mute_minutes` /
  `similarity` / `history_size` / `llm_break`），私聊同样生效。
- **群友印象**（fake db）：与新群友的首轮交谈完成后，适配器用一个隔离的
  `nb2-meta` 元租户让角色脱稿写一段第一人称「第一印象」，存入
  `nb2_data/known_members.json`（key 为 `{昵称}_{QQ号}`，同名靠 QQ 号后缀区分、
  改名自动迁移）；之后该群友再说话时，印象以 `【你对 某某 的印象】` 注入当轮
  上下文，角色就能「认识」老熟人。生成是后台任务，不阻塞回复，失败自动跳过。
  角色还可以通过 `update_member_impression` 工具**自行更新**印象
  （觉得了解加深或印象过时了自己改写），该工具随宿主注入所有租户。
- **回复**：默认按 QQ 群聊风格——`GSK_NB2_EXTRA_PROMPT` 注入当轮要求
  （简短、口语、每句一行、不写动作、不用「」），发送前再经 `strip_rp_style`
  确定性清洗残留的 `*动作*` 与「」（提示词压不住角色卡的 RP 惯性时兜底），
  最后按行拆成多条短消息依次发送（最多 5 条，超出合并进最后一条；间隔 0.8s 防抖）。
  附加要求只影响当轮回复，不写入会话历史；清洗与分段对主动消息同样生效，
  但主动消息的**生成风格**不受 `extra_prompt` 影响（生成在 Runtime 侧）。
- **主动发言**：与 CLI 同一条对话欲链路（ThinkEngine 四维心情评估 → 阈值判断 →
  INITIATIVE_SPEAK），间隔由 `config/local.yaml` 的 `initiative_timer` 段决定。
  每次群回复后每个租户会产生一次对话欲评估调用（短 JSON），想说时再一次生成调用——
  群多时这笔 token 成本是 `群数 × (1 + 评估)` 量级，开白名单前请估算。
- **重试安全**：每条消息以 `nb2:<botQQ>:<message_id>` 为幂等键；会话失效时
  适配器自动重建租户并同键重试一次，不会产生重复发言。
- **资源闸**：Runtime 过载（`resource.limit_exceeded`）时 bot 回复「稍后再叫」，
  不会排队阻塞。
- **会话持久化**：群↔会话映射存于 `nb2_data/sessions.json`；租户会话数据在
  `runtime_data/users/<hash>/agents/<hash>/`。进程重启后会话自动恢复。
- **优雅退出**：Ctrl+C 时保存所有租户会话后退出。
- **错误兜底**：调用失败时 bot 会回复提示并在日志（loguru）记录细节。

## 公网部署注意

- 适配器不监听任何 GensokyoAI 端口（Runtime 在进程内），对外只有 NoneBot 的
  8080（给 NapCat 连）——攻击面比「Runtime web_server + 远程 bot」小一整块。
- 建议配置 `GSK_NB2_GROUP_WHITELIST`，避免 bot 被拉进陌生群后无差别响应（烧 token）。
- 主动发言叠加群数量会放大 API 调用频次；`resource_control` 并发闸默认开启，
  超出承载的请求会被直接拒绝——这是防止 API 额度被打爆的最后一道闸。
- QQ 侧风控（新号 / 频繁发言容易被限制）属于平台规则：用老号、控制发言频率；
  主动发言频繁的群建议调高 `initiative_timer.min_delay_seconds`。

## 当前限制（v1）

- 全 bot 共用一个角色（`GSK_NB2_CHARACTER`），暂无按群切换命令。
- 不解析图片 / 语音 / 合并转发，只处理纯文本。
- World 多角色模式未接入（适配器面向单角色扮演场景）。
- 单适配器进程部署：多进程同时跑会导致主动消息重复投递。
