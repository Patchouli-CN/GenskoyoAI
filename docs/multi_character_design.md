# GensokyoWorld 多角色扮演设计文档（草案）

> 状态：✅ 已实现（2026-07，按 `docs/gsk-ai-multi-character.md` 阶段 1-10 全部落地）。
> 作者构思，Claude 整理成型。
> 目标读者：实现者本人（后续开发）。
> 一句话定位：从"演一个角色"升级到"单模型演一整台戏"——框架当导演和舞台，模型只管入戏。

---

## 0. 核心理念

角色扮演的本质是**演戏**。当前框架已经能让模型稳定地"演一个人"，多角色要做的，是在演员之上加一个**导演**和一个**舞台**：

- **演员（Actor）**：现有的 `Agent`，负责"入戏说话"。不改或极少改。
- **导演（Director）**：每段话结束后决定"这场戏接下来谁上"——`continue`（当前角色继续）还是 `switch`（换角色上场）。
- **舞台（World）**：`GensokyoWorld` 主类，持有场景、角色花名册、共享剧本，负责编排。

模型永远只做一件事：**演好当前被点到的那个角色**。谁上场、记什么、在哪，全部由框架管。

---

## 1. 设计目标与非目标

### 目标
- 单个模型（单个 ModelClient）扮演同一场景中的多个角色。
- 每个角色拥有独立的人设、记忆、当前所处场景。
- 由"导演"决定每一轮由谁发言、是否切换角色。
- 复用现有的主动对话（InitiativeTimer / ThinkEngine）、场景系统（SceneManager）、记忆系统。
- 开场可指定"主角"：主角是角色 → AI 主动开场；主角是用户 → 等用户先说。

### 非目标（第一版不做）
- 多个模型并发扮演（成本与复杂度高，第一版单模型足矣）。
- 角色之间的实时并发抢话（第一版是"回合制导演调度"，一次一个角色）。
- 跨会话的世界状态共享（第一版一个 World 一段戏）。

---

## 2. 与现有架构的关系（关键：这是生长，不是重写）

| 新概念 | 复用/扩展的现有实现 | 说明 |
| --- | --- | --- |
| 演员 Actor | `core/agent/Agent`（原样） | 每个角色一个 Agent 实例 |
| 共享大脑 | 单个 `ModelClient` | 所有 Actor 共用，成本可控 |
| 共享事件 | 单个 `EventBus` | 已验证可跨组件共享（工具/监听器都挂它） |
| 导演 Director | 仿 `ActionPlanner` 的结构化决策 | 复用"模型输出 JSON 决策"成熟模式 |
| 舞台 World | 新增 `GensokyoWorld` 主类 | 编排层，之前没有 |
| 场景/地点 | `scene/SceneManager`（扩展） | 增加"角色→场景"在场映射 |
| 每角色记忆 | 记忆路径已带 `character_name` | 天然隔离，无需改 |
| 主动对话 | `InitiativeTimer` / `ThinkEngine` | 已内置，直接为多角色服务 |
| 开场 | `begin_scene`（原样） | 主角的 begin_scene 驱动开场 |

**结论**：现有 `Agent` 几乎不动，多角色是在它"之上"加一层 World 编排 + Director 决策。

---

## 3. 顶层架构

```
                    ┌─────────────────────────────┐
                    │        GensokyoWorld         │  ← 舞台 + 编排
                    │  - roster: {id: Actor}       │
                    │  - shared_transcript         │  ← 共享剧本（大家都看到的）
                    │  - director: Director        │
                    │  - stage: WorldStage         │  ← 谁在哪个场景
                    └──────────────┬──────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
   ┌─────▼─────┐            ┌──────▼──────┐           ┌──────▼──────┐
   │ Actor:魔理沙│           │ Actor:帕秋莉 │          │ Actor:蕾米  │
   │ Agent 实例  │           │  Agent 实例  │          │  Agent 实例 │
   │ 私有人设/记忆│           │ 私有人设/记忆 │          │ 私有人设/记忆│
   └─────┬──────┘           └──────┬──────┘           └──────┬──────┘
         └────────────┬────────────┴────────────┬───────────┘
                      │                          │
              ┌───────▼────────┐        ┌────────▼────────┐
              │  单 ModelClient │        │   单 EventBus    │  ← 共享
              └────────────────┘        └─────────────────┘
```

---

## 4. 数据模型

### 4.1 消息分层（整个系统的地基，务必先想清楚）

这是最容易做错、也最决定成败的一点。消息分成**两层**：

**共享层 · 剧本 `shared_transcript`**
- 所有在场角色和用户都"看得到"的对话与动作。
- 例：魔理沙说「你看，这就是红魔馆地下大图书馆」→ 进剧本 → 帕秋莉上场时能看到这句。
- 存在 World 上，一份，全场景共享（或按场景分片，见 4.3）。

**私有层 · 人设 + 记忆**
- 每个角色的 system_prompt（人设 + 框架规则）、私有语义记忆。
- 切到某角色时，才注入这个角色的私有层。
- 例：魔理沙记"偷书被抓好丢脸"，帕秋莉记"又来偷我书"——**同一世界事件，各记各的视角**。

**给模型的最终上下文 = 当前角色私有人设/记忆 + 共享剧本 + 当前场景描述**

```python
# 伪代码：为"当前该发言的角色"构建上下文
messages = [
    {"role": "system", "content": actor.system_prompt},  # 私有人设
    {"role": "system", "content": scene.render_with_options()},  # 当前场景（复用现有）
    *actor.private_memory_context(),  # 私有记忆检索
    *world.shared_transcript.render_for(actor),  # 共享剧本（关键）
]
```

> 想清楚这层，系统就立住了；想不清，角色会"知道自己不该知道的事"（穿帮）。

### 4.2 角色在场表 WorldStage

```python
class WorldStage(Struct):
    """谁在哪个场景。Director 只能从'当前场景在场角色'里选角。"""

    # character_id -> scene_id
    locations: dict[str, str] = field(default_factory=dict)

    def characters_in(self, scene_id: str) -> list[str]:
        return [cid for cid, sid in self.locations.items() if sid == scene_id]
```

- 角色 `scene_switch` 时，同时更新 WorldStage 里它的位置。
- **Director 选角时，只能在"用户当前所在场景的在场角色"里挑**，否则角色会瞬移进对话。

### 4.3 剧本可见性（进阶，可第二版）

第一版：`shared_transcript` 全局一份，简单。
第二版：按场景分片——魔理沙在魔法森林说的话，不该被红魔馆里的帕秋莉看到。用 `transcript[scene_id]` 分片，切场景时切剧本片段。

---

## 5. 导演 Director（核心新组件）

### 5.1 职责
每当一个角色说完话，Director 决定接下来的走向。**它本身也是一次模型调用**（结构化输出），但走的是"导演视角"的 prompt。

### 5.2 决策 Schema（仿现有 InitiativeTimer 的 JSON 决策模式）

```python
DIRECTOR_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["continue", "switch", "wait_user"]},
        # continue: 当前角色继续说 / switch: 换人 / wait_user: 把话筒交给用户
        "next_character": {"type": "string"},  # action=switch 时，目标角色 id
        "reason": {"type": "string"},  # 为什么这么调度（可用于调试/日志）
    },
    "required": ["action", "reason"],
}
```

- `action=switch` 时，`next_character` **必须是当前场景在场角色**（World 校验，非法则降级为 continue 或 wait_user）。
- `action=wait_user`：戏演到该用户接话了，交还控制权。

### 5.3 性能优化：省掉独立的导演往返

朴素做法是每轮单开一次 Director 模型调用，**每轮多一次往返，延迟翻倍**。

优化（推荐，复用现有主动定时器"回复后顺带决策"的套路）：
- 让**演员在回复末尾顺带吐一个结构化尾信号**（下一步该 continue / switch / wait_user）。
- 演员生成正文（流式给用户）后，紧接着输出一小段 JSON（不展示给用户），World 解析它做调度。
- 这样一次模型调用同时完成"演出 + 导演暗示"，省一次往返。
- 兜底：尾信号解析失败时，才降级为独立 Director 调用。

---

## 6. 开场逻辑（复用 begin_scene）

```
World.start(protagonist):
    if protagonist 是某角色 id:
        # AI 主动开场：该角色的 begin_scene 驱动首句
        actor = roster[protagonist]
        актор 走现有 begin_scene 流程（场景+开场动作）
        然后进入导演循环
    elif protagonist == "__user__":
        # 用户主动：只布置舞台，等用户先开口
        设置初始场景，不生成首句
```

- 完全复用你现在的 `begin_scene`（`{scene, action}`）机制，无需新逻辑。
- 主角的 `begin_scene.scene` 决定开场时用户"站在哪个场景"，同场景的在场角色成为初始演员池。

---

## 7. 一轮完整流程（以"红魔馆偷书"为例）

```
1. 用户在 magic_forest，主角=魔理沙，AI 主动开场
   魔理沙(begin_scene): 「走，带你去个好地方，DA☆ZE！」

2. 魔理沙调 scene_switch("scarlet_devil_mansion")
   → World 更新 stage: 魔理沙 & 用户 → 红魔馆
   → 广播 SCENE_SWITCHED（前端提示"当前场景：红魔馆"）

3. 魔理沙: 「你看，这就是红魔馆地下大图书馆」
   → 进 shared_transcript
   → 演员尾信号: {action: "continue"}  或  {action: "wait_user"}

4. 用户: 「这里的书看起来好厉害……但主人呢？」
   → 进 shared_transcript

5. Director/尾信号判定: switch → 帕秋莉（她在红魔馆在场）
   → World 切换当前演员为帕秋莉
   → 注入帕秋莉私有人设/记忆 + 共享剧本（她能看到前面魔理沙和用户说的话）
   帕秋莉: 「……又是你，雾雨魔理沙。把书放下。」

6. 世界事件"魔理沙偷书被帕秋莉抓到"写入双方私有记忆（各自视角）
   魔理沙记忆: "在红魔馆偷书被帕秋莉逮个正着，尴尬"
   帕秋莉记忆: "魔理沙又来偷书，这次当场抓住了"
```

---

## 8. GensokyoWorld 主类骨架

```python
class GensokyoWorld:
    def __init__(self, config: WorldConfig):
        self.model_client = ModelClient(...)  # 共享大脑
        self.event_bus = EventBus(...)  # 共享事件
        self.scene_manager = SceneManager(...)  # 共享舞台
        self.roster: dict[str, Agent] = {}  # 角色花名册（每个是 Actor）
        self.stage = WorldStage()  # 谁在哪
        self.shared_transcript = SharedTranscript()  # 共享剧本
        self.director = Director(self.model_client, self.event_bus)
        self._current_actor_id: str | None = None

    def add_character(self, character_file: Path, scene_id: str) -> None:
        """加入一个角色，共用 model_client + event_bus。"""
        agent = Agent(config=self._actor_config(character_file))  # 复用现有 Agent
        self.roster[agent.character_name] = agent
        self.stage.locations[agent.character_name] = scene_id

    async def start(self, protagonist: str) -> None:
        """定主角、布置舞台、按需 AI 开场。"""
        ...

    async def step(self, user_input: str | None) -> AsyncIterator[Turn]:
        """推进一轮：当前演员发言 → 导演决策 → 可能切换演员。"""
        ...
```

**关键接线点**（都已在现有代码中验证存在）：
- Agent 共用 model_client：`AgentComposition` 目前每个 Agent 自建 ModelClient，需加一个"注入外部 model_client"的构造路径。
- Agent 共用 event_bus：同上，`composition.py` 增加可选注入。
- 记忆隔离：现有路径 `base/character_name/memory/session_id` 已按角色分，无需改。
- 场景在场：`SceneManager` 或 World 维护 `character_id → scene_id`。

---

## 9. 需要对现有代码的最小改动清单

1. **`AgentComposition` / `Agent.__init__`**：支持注入外部 `model_client` 和 `event_bus`（当前是内部自建）。这是让多 Actor 共享大脑的前提，改动小、纯增量。
2. **`SceneManager`**：增加"角色→当前场景"的在场映射查询（或由 World 持有）。
3. **`Scene` 数据类**：可选增加 `characters_present`（也可由 World 的 WorldStage 统一管，二选一）。
4. **新增 `world/` 包**：`GensokyoWorld`、`Director`、`WorldStage`、`SharedTranscript`。
5. **新增导演决策 schema** + 演员尾信号解析（仿 `think_engine` 的 JSON 决策解析）。
6. **新增 `WorldConfig`**：角色列表、主角、初始场景等。

现有 `Agent`、记忆、场景工具、主动定时器**基本不动**。

---

## 10. 分阶段实施建议

**阶段一 · 能跑起来（MVP）**
- 单场景、两个角色、回合制导演（独立 Director 调用，先不优化尾信号）。
- 共享 transcript 全局一份。
- 主角=角色，AI 开场。
- 目标：能看到"魔理沙说完 → 导演切帕秋莉 → 帕秋莉接话"。

**阶段二 · 演出质量**
- 演员尾信号优化（省导演往返）。
- 世界事件写入双方私有记忆（各视角）。
- 多场景 + 在场约束（导演只从在场角色选）。

**阶段三 · 舞台完整**
- transcript 按场景分片（可见性隔离）。
- 主角=用户 的开场模式。
- 主动定时器在多角色下的调度（谁该主动、会不会打架）。

---

## 11. 已知风险与岔路口（想清楚再动手）

1. **消息分层做错 = 角色穿帮**（第 4.1 节）。这是头号风险，MVP 就要做对。
2. **导演往返延迟**（第 5.3 节）。MVP 可接受独立调用，但要预留尾信号优化路径。
3. **在场约束**（第 4.2 节）。导演选角必须限定在场角色，否则瞬移穿帮。
4. **主动定时器打架**：多个角色都能主动开口时，谁先说？建议由 World 统一仲裁，同一时刻只允许一个主动发言，其余排队或丢弃。
5. **成本**：单模型省钱，但导演调用 + 多角色记忆检索会增加 token。尾信号优化 + 记忆检索按需触发可缓解。

---

## 12. 一句话收尾

你已经有**演员**（Agent）、**记忆**（分角色隔离）、**主动能力**（InitiativeTimer）、**舞台**（SceneManager）。多角色要补的，只是一个**导演**（Director）和一个把它们串起来的**世界**（GensokyoWorld）。

这是这套架构最漂亮的一次收口——从"演一个人"到"演一台戏"。地基已经在了，剩下的是编排。

到时候见，导演。🌸

