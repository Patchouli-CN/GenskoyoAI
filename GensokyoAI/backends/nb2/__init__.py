"""NoneBot2 QQ 机器人后端（可选 extra: nb2）。

通过 OneBot 11 协议把 QQ 群 / 私聊消息桥接到 GensokyoAI Runtime；
每个群、每个私聊用户映射为独立 Runtime 租户（agent_id），会话与资源闸彼此隔离。
入口：`python -m GensokyoAI.backends.nb2`，部署步骤详见 docs/nb2_adapter.md。

注意：包根故意不导出任何符号——`Nonebot2Adapter` 在 `.adapter` 子模块里，
它依赖 nonebot（可选 extra）；包根保持可导入，避免污染无 nb2 依赖的环境。
组装用法：`from GensokyoAI.backends.nb2.adapter import Nonebot2Adapter`
"""
