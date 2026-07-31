<div align="center">
  <h1>🌸 GensokyoAI · 新手上手指南</h1>
  <p><i>「欢迎来到幻想乡。这里没有 README 三万字的压迫感，只有一杯红茶的功夫，就能和角色说上话。」</i></p>
</div>

---

> 这份指南是给**第一次来幻想乡**的你写的。不需要你懂什么是 Runtime、事件总线、schema version——那些等你玩熟了再翻 [README](./README.md) 也不迟。
>
> 目标很简单：**十分钟内，让灵梦或魔理沙在你的终端里活过来。**

## 你需要准备什么

就三样，缺一不可：

- **Python 3.14+**。是的，就是要这么新——这是项目有意选的底线，别降级。用 `python --version` 确认一下。
- **一个能跑的大模型**。新手最省事的选择是 [**Ollama**](https://ollama.com/)（本地、免费、不用 API Key，隐私也不出门）。当然你也可以接 OpenAI / DeepSeek / Claude / Gemini，那些需要钥匙（API Key）。
- **[uv](https://github.com/astral-sh/uv)**（强烈推荐）。它会自动帮你准备好合适的 Python 和依赖，省去一堆环境的破事。装它：`pip install uv`。

> 🧙‍♀️ 魔理沙温馨提示：「没有 uv 也能用 pip，不过 uv 快得像我的魔炮，DA☆ZE。」

---

## 第一步：把幻想乡搬回家

```bash
git clone <你的仓库地址> GensokyoAI
cd GensokyoAI
```

## 第二步：请一位常驻居民（装个模型）

如果你选了 Ollama，先把它装上并拉一个模型。中文角色扮演推荐用 `qwen`（千问）系列，中文味儿正：

```bash
# 装好 Ollama 后，拉一个模型（第一次会下载，耐心等等）
ollama pull qwen3:8b
```

> 💡 模型越大越聪明，但也越吃显存。显卡一般的话，8b 左右是甜点区。等你用上了**场景系统**（后面会讲），小模型也能有不错的临场感。

## 第三步：一句话，让魔理沙开口

项目自带了全套东方角色卡（`characters/zh_cn/` 里有几十位），直接点名就行：

```bash
# 用 uv（推荐）
uv run --extra ollama -m GensokyoAI.cli.main --character "characters/zh_cn/KirisameMarisa.yaml" --new-session

# 或者用 pip（先 pip install -e ".[ollama]"）
python -m GensokyoAI.cli.main --character "characters/zh_cn/KirisameMarisa.yaml" --new-session
```

Windows 用户更省事，仓库根目录已经放好了脚本，双击或命令行跑：

```bash
run_default_uv.cmd      # 用 uv
run_default_pip.cmd     # 用 pip
```

顺利的话，你会看到一个红白配色的欢迎面板，然后魔理沙就**自己开口了**——她不会干巴巴地说"你好我是魔理沙"，而是带着当前正在做的事自然登场（这就是 `begin_scene` 的功劳，后面讲）。

> 🎉 恭喜，你已经在和幻想乡的居民对话了。接下来的都是锦上添花。

---

## 换个角色 / 换个模型

**换角色**：把 `--character` 后面的路径换成 `characters/zh_cn/` 里任意一位。灵梦、芙兰、蕾米、咲夜、幽幽子……几十位随你挑。

**换模型**：不想用本地 Ollama，想接云端大模型？编辑配置文件（推荐新建 `config/local.yaml`，不动默认配置），改 `model` 节。以 DeepSeek 为例：

```yaml
model:
  provider: "deepseek"
  name: "deepseek-chat"
  # api_key 不要写在这里！用环境变量更安全：
```

然后设置钥匙（**别把 Key 写进文件提交上去**）：

```bash
# Linux / macOS
export GENSOKYOAI_API_KEY=你的钥匙
# Windows
set GENSOKYOAI_API_KEY=你的钥匙
```

启动时带上你的配置：

```bash
python -m GensokyoAI.cli.main -c "characters/zh_cn/HakureiReimu.yaml" --config "config/local.yaml" --new-session
```

支持的 Provider：Ollama / OpenAI / OpenRouter / DeepSeek / OpenAI Responses / Claude / Gemini。细节看 [配置模板](./tmp/template-conf.yaml) 和 [README 的「快速配置 Provider」](./README.md#快速配置-provider)。

---

## 🗺️ 进阶玩法：让角色真正"身处幻想乡"（场景系统）

这是 GensokyoAI 最有味道的功能之一。默认关闭，开启后角色会**真的知道自己站在博丽神社的石阶上**，而不用你在对话里反复提醒。

### 为什么要用它

没有场景系统时，你得靠 prompt 或每句话去暗示"我们现在在魔法森林"，角色一多聊就忘、就出戏。场景系统把"环境"变成一份常驻的结构化状态：

- 角色开场就置身某个地点，**注意力全花在人设和对话上**——小模型也能演得更稳。
- 剧情推进时，角色自己会切换场景；忘了自己在哪，也会自己查。
- 当前场景**随会话记住**，退出重进还在原地。

### 三步开启

**1. 在配置里打开开关**（`config/local.yaml`）：

```yaml
scene:
  enabled: true                 # 打开场景系统
  library_path: ./scenes        # 场景库目录（项目已自带示例）
  default_scene: hakurei_shrine # 没指定时的默认起始地点
  enforce_connectivity: false   # true 时角色只能去"相邻"场景，不能瞬移

tool:
  # 把 "scene" 加进来，角色才有切换/查看场景的能力
  builtin_tools: ["time", "moon", "system", "scene"]
```

**2. 场景库已经备好了。** 看看 `scenes/zh_cn/`，博丽神社、魔法森林、红魔馆、迷途竹林、人间之里都在里面。想加新地点？照着抄一份 YAML 就行：

```yaml
# scenes/zh_cn/hakurei_shrine.yaml
id: hakurei_shrine
name: 博丽神社
description: |
  你正身处博丽神社。石阶从山下蜿蜒而上，赛钱箱前几乎不见香客，庭院落满红叶。
atmosphere: 宁静中带着几分冷清
time_of_day: 黄昏
connected_scenes: [magic_forest, human_village]  # 从这里能直接走去哪
props: [破旧的赛钱箱, 随风摇动的绘马]
```

**3. 让角色开场就在场景里。** 编辑角色卡的 `begin_scene`——这是"场景 + 开场动作"的组合拳：

```yaml
begin_scene:
  scene: hakurei_shrine              # 交给场景系统，开场就站在神社
  action: "正在扫院子，红叶落了一地，嘴里念叨着又没人来上香。"  # 此刻在干什么
```

启动后，灵梦会带着**完整的神社环境**和**扫院子这个动作**自然开口。之后她想去别处，会自己调 `scene_switch`；你的 console 还会贴心地提示 `（当前场景：魔法森林）`。

> 🍵 幽幽子表示：「知道自己身在何处，才能优雅地飘去下一个地方呢～」

---

## 🎭 高阶玩法：一台戏（World 多角色）

这是场景系统的下一站：不再只演一个角色，而是**一个模型演一整台戏**。魔理沙带你去红魔馆，主人蕾米莉亚就在馆内等着——谁开口、什么时候换人，由导演根据剧情决定，不是轮流发言。

### 它和群聊有什么不同

- **一个大脑，多张面孔**：所有角色共享同一个模型（成本不翻倍），但各自的记忆和会话完全独立；蕾米莉亚不会知道馆外发生的事。
- **导演调度**：每段话结束后，导演决定"当前角色继续 / 换人上场 / 把话筒还给你"。角色之间最多连聊几轮有熔断，不会把你晾在一边看戏看到烧光 token。
- **你在戏里**：你有舞台位置，会跟着当前演员移动；表演段落结束后，每个在场角色还会把这段经历记成"自己视角"的记忆。
- **戏可以存盘**：舞台、剧本、各角色的状态一起存档，下次接着演。

### 一分钟开演

项目自带现成示例（魔理沙 × 蕾米莉亚 × 红魔馆）：

```bash
uv run -m GensokyoAI.cli.main --config tmp/temp-world.yaml
```

`world_example.yaml` 里 `world.enabled: true`，所以会自动进入 World 模式；用你自己的配置时，打开 `world.enabled` 或加 `--world` 参数即可。

想换角色阵容？编辑 `tmp/temp-world.yaml`（建议复制为 `config/local_world.yaml` 再改）里的 `world.actors`：每个角色一行 id + 角色卡路径，`characters/zh_cn/` 下几十位居民随你点。`protagonist` 决定谁主动开场（填 `__user__` 则等你先开口）。

World 模式下有几条专用命令：`/world`（世界状态）、`/roster`（登场角色）、`/stage`（谁在哪个场景）、`/transcript`（当前场景的共享剧本）。单角色会话命令（`/back`、`/history` 等）在 World 模式不适用——世界的存档管理看 `/world`。

> 📚 帕秋莉表示：「未经许可进入图书馆的人，我会记得比她自己还清楚哦。」

---

## 常用命令速查

启动参数：

| 参数 | 作用 |
| --- | --- |
| `--character` / `-c` | 指定角色卡路径 |
| `--config` | 指定配置文件（默认 `config/local.yaml`，首次启动自动生成） |
| `--new-session` | 强制开一段新对话 |
| `--resume <会话id>` | 继续之前的某段对话 |
| `--list-sessions` | 看看你有哪些历史会话 |
| `--no-stream` | 关掉流式输出（一次性出整段） |
| `--world` | 进入多角色 World 模式（`world.enabled: true` 时自动启用） |

对话中输入 `<cmd>help</cmd>` 可以查看所有对话内命令；`Ctrl+C` 安全退出（会自动保存）。

---

## 遇到麻烦？

- **`ImportError: ... ollama`**：你没装对应 Provider 的可选依赖。用 uv 的话加 `--extra ollama`，用 pip 的话 `pip install -e ".[ollama]"`（把 ollama 换成你用的 Provider）。
- **模型调用失败 / 连不上**：本地 Ollama 确认 `ollama serve` 在跑；云端 Provider 确认 `GENSOKYOAI_API_KEY` 环境变量设对了。
- **502 / 连接错误**：多半是 Ollama 没启动、代理没配好或防火墙拦了，日志里会有提示。
- **角色卡报错**：运行时会给出结构化诊断，照着提示改 YAML 字段就行。
- **Python 版本不够**：必须 3.14+，用 uv 的话它会自动帮你准备。

还是搞不定？来 [Q 群](./README.md) 问，或者翻 [使用指南](./docs/user_guide.md) 看细节。

---

<div align="center">
  <p><b>准备好了吗？</b></p>
  <p>「幻想乡的大门已经打开——去和你喜欢的角色聊聊吧。」</p>
  <p>🌸 ☯ 🌸</p>
</div>

