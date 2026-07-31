# GensokyoAI/commands/permission.py
"""命令四级权限模型：数值越大权限越高，放行要求 调用方等级 >= 命令所需等级。

权限等级由调用方（后端/适配器）在构造 CommandContext 时给出：
- 本地后端（console）：本地用户即主人，默认 OWNER（不指定即全部放行，向后兼容）；
- 网络适配器（如 nb2）：按平台身份解析（主人名单/群管理/普通成员/无法核实）。

命令声明权限：`@command(..., permission=PermissionLevel.USER)`；
**未指定的命令默认 OWNER**——命令默认只给主人用，需要开放再显式降级。
"""

from enum import IntEnum


class PermissionLevel(IntEnum):
    """四级权限：VISITOR < USER < ADMIN < OWNER。"""

    VISITOR = 0  # 无法核实身份——最低信任级
    USER = 1  # 普通用户（群成员 / 私聊用户）
    ADMIN = 2  # 平台管理员（QQ 群管理 / 群主）
    OWNER = 3  # bot 主人（默认级：未声明权限的命令只有主人可用）
