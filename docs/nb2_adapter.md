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

复制示例配置并按需修改：

```bash
cp tmp/nb2.env.example .env
```

关键配置项（完整注释见 `tmp/nb2.env.example`）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `DRIVER` / `HOST` / `PORT` | fastapi / 127.0.0.1 / 8080 | NoneBot 自身监听（反向 WS 服务） |
| `LOG_LEVEL` | `WARNING` | nonebot 自身日志级别（它自己的 sink）；适配器日志走 GensokyoAI 体系 |
| `GSK_NB2_CHARACTER` | `KirisameMarisa` | 所有群 / 私聊共用的角色 |
| `GSK_NB2_DATA_DIR` | `nb2_data` | 适配器数据目录（群→会话映射表） |
| `GSK_NB2_ROOT_DIR` | 当前工作目录 | GensokyoAI 项目根（characters/config 解析基准） |
| `GSK_NB2_INITIATIVE` | `true` | 角色主动发言；`false` 则停用各租户主动定时器 |
| `GSK_NB2_SPLIT_REPLY` | `true` | 回复按行拆成多条短消息发送（QQ 群聊风格） |
| `GSK_NB2_STRIP_RP_STYLE` | `true` | 发送前清洗 RP 标记（`*动作*`、`「」`），确定性生效 |
| `GSK_NB2_EXTRA_PROMPT` | 内置群聊风格要求 | 随每条回复注入的附加要求；留空用默认，可改写成自己的约束 |
| `GSK_NB2_GROUP_WHITELIST` | 空（不限） | 逗号分隔的群号白名单 |

内置的默认附加要求（`GSK_NB2_EXTRA_PROMPT` 留空时生效）：

> 你在 QQ 群聊/私聊里聊天，不是在写角色扮演小说：回复要简短、口语化、像真人发消息；
> 每句话单独占一行（系统会按行拆成多条消息依次发送）；
> 不要用 *星号* 或括号描写动作、表情、心理活动；不要用「」等台词引号，直接写说的话；
> 一次回复最多三句话。

`.env` 与 `nb2_data/` 已加入 `.gitignore`，不会入库。
模型与 API key 沿用项目根的 `config/local.yaml`（与 CLI 相同），无需重复配置。
日志走 GensokyoAI 体系（格式 + 第三方噪音过滤）；是否显示到控制台与级别沿用
`local.yaml` 的 `log_console` / `log_level`（控制台盯梢时保持 `log_console: true` 即可），
也可用 `GENSOKYOAI_LOG_CONSOLE` / `GENSOKYOAI_LOG_LEVEL` 环境变量临时覆盖。

## 启动

```bash
# 1. 启动适配器（内含 Runtime，一个进程搞定）
python -m GensokyoAI.backends.nb2

# 2. 启动 NapCat，配置反向 WebSocket：
#    ws://127.0.0.1:8080/onebot/v11/ws
```

## 行为说明

- **触发**：群聊需 @bot（或回复 bot 的消息）；私聊全部响应。纯表情 / 图片不回；
  `/` 前缀保留给将来的 bot 命令，当前忽略。
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
