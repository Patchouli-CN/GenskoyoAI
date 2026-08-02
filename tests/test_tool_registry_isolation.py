"""ToolRegistry 实例隔离（register() 不污染进程级全局表）定向测试

回归：register() 曾先写全局 _TOOL_REGISTRY 再回取——适配器/租户闭包
（如 set_reminder 捕获 agent_id）会被之后新建注册表的 _load_builtin
吸进无关 Agent（多角色/多租户串台），且同名全局互踩。
"""

import unittest

from GensokyoAI.tools.base import get_tool, tool
from GensokyoAI.tools.registry import ToolRegistry


class RegisterIsolationTests(unittest.TestCase):
    def test_register_does_not_pollute_global_registry(self):
        def tenant_tool(agent_id: str) -> str:
            """捕获租户 id 的闭包。"""
            return agent_id

        registry_a = ToolRegistry()
        registry_a.register(tenant_tool, name="__leak_probe")
        # 全局表未被写入
        self.assertIsNone(get_tool("__leak_probe"))
        # 之后新建的注册表不会吸进它
        registry_b = ToolRegistry()
        self.assertIsNone(registry_b.get("__leak_probe"))
        # 本实例正常可用
        self.assertIsNotNone(registry_a.get("__leak_probe"))

    def test_same_name_closures_stay_instance_scoped(self):
        def closure_a() -> str:
            return "a"

        def closure_b() -> str:
            return "b"

        registry_a = ToolRegistry()
        registry_b = ToolRegistry()
        registry_a.register(closure_a, name="__same_name")
        registry_b.register(closure_b, name="__same_name")
        # 同名不互踩：各实例持有各自闭包
        self.assertIs(registry_a.get("__same_name").func, closure_a)
        self.assertIs(registry_b.get("__same_name").func, closure_b)

    def test_register_reuses_decorated_builtin_definition(self):
        @tool(name="__decorated_probe", description="装饰器给的描述", parallel_safe=False)
        def decorated() -> str:
            return "x"

        try:
            registry = ToolRegistry()
            registry.register(decorated, name="__decorated_probe")
            definition = registry.get("__decorated_probe")
            # 复用装饰器定义（描述与 parallel_safe 保留），而非按默认重建
            self.assertEqual(definition.description, "装饰器给的描述")
            self.assertFalse(definition.parallel_safe)
        finally:
            from GensokyoAI.tools.base import _TOOL_REGISTRY

            _TOOL_REGISTRY.pop("__decorated_probe", None)


if __name__ == "__main__":
    unittest.main()
