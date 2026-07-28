import tempfile
import unittest
from pathlib import Path

from GensokyoAI.core.agent.types import ProviderCapability
from GensokyoAI.core.config import ModelConfig, SceneConfig, ToolConfig
from GensokyoAI.scene import Scene, SceneError, SceneManager
from GensokyoAI.tools.build_service import ToolBuildContext, ToolBuildService
from GensokyoAI.tools.registry import ToolRegistry


def _write_scene(directory: Path, scene_id: str, **fields) -> None:
    import yaml

    data = {"id": scene_id, "name": fields.pop("name", scene_id), **fields}
    (directory / f"{scene_id}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


class SceneRenderTests(unittest.TestCase):
    def test_render_includes_name_and_details(self):
        scene = Scene(
            id="shrine",
            name="博丽神社",
            description="石阶蜿蜒而上。",
            atmosphere="宁静",
            time_of_day="黄昏",
            props=["赛钱箱", "绘马"],
        )
        rendered = scene.render()
        self.assertIn("博丽神社", rendered)
        self.assertIn("石阶蜿蜒而上", rendered)
        self.assertIn("黄昏", rendered)
        self.assertIn("赛钱箱", rendered)

    def test_from_dict_defaults(self):
        scene = Scene.from_dict("forest", {"description": "浓雾。"})
        self.assertEqual(scene.id, "forest")
        self.assertEqual(scene.name, "forest")
        self.assertEqual(scene.description, "浓雾。")
        self.assertEqual(scene.connected_scenes, [])


class SceneManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.library = Path(self._tmp.name)
        _write_scene(
            self.library,
            "hakurei_shrine",
            name="博丽神社",
            description="神社庭院。",
            connected_scenes=["magic_forest"],
        )
        _write_scene(
            self.library,
            "magic_forest",
            name="魔法森林",
            description="浓雾森林。",
            connected_scenes=["hakurei_shrine"],
        )
        _write_scene(self.library, "bamboo_forest", name="迷途竹林", description="翠竹。")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _manager(self, **overrides) -> SceneManager:
        config = SceneConfig(enabled=True, library_path=self.library, **overrides)
        return SceneManager(config)

    async def test_load_library(self):
        manager = self._manager()
        scenes = await manager.load_library()
        self.assertEqual(set(scenes), {"hakurei_shrine", "magic_forest", "bamboo_forest"})

    async def test_disabled_manager_loads_nothing(self):
        manager = SceneManager(SceneConfig(enabled=False, library_path=self.library))
        scenes = await manager.load_library()
        self.assertEqual(scenes, {})
        self.assertIsNone(await manager.get_current_scene())

    async def test_switch_scene_updates_current(self):
        manager = self._manager()
        scene = await manager.switch_scene("magic_forest")
        self.assertEqual(scene.id, "magic_forest")
        self.assertEqual(manager.current_scene_id, "magic_forest")
        current = await manager.get_current_scene()
        self.assertIsNotNone(current)
        self.assertEqual(current.name, "魔法森林")

    async def test_switch_invalid_scene_raises(self):
        manager = self._manager()
        with self.assertRaises(SceneError):
            await manager.switch_scene("nonexistent")

    async def test_enforce_connectivity_blocks_unconnected(self):
        manager = self._manager(enforce_connectivity=True)
        await manager.switch_scene("hakurei_shrine")
        # 神社只连通魔法森林，不能直接去竹林
        with self.assertRaises(SceneError):
            await manager.switch_scene("bamboo_forest")
        # 连通的可以走
        scene = await manager.switch_scene("magic_forest")
        self.assertEqual(scene.id, "magic_forest")

    async def test_connectivity_disabled_allows_any(self):
        manager = self._manager(enforce_connectivity=False)
        await manager.switch_scene("hakurei_shrine")
        scene = await manager.switch_scene("bamboo_forest")
        self.assertEqual(scene.id, "bamboo_forest")

    async def test_resolve_initial_scene_prefers_session(self):
        manager = self._manager(default_scene="hakurei_shrine")
        resolved = await manager.resolve_initial_scene("magic_forest")
        self.assertEqual(resolved, "magic_forest")

    async def test_resolve_initial_scene_falls_back_to_default(self):
        manager = self._manager(default_scene="hakurei_shrine")
        resolved = await manager.resolve_initial_scene(None)
        self.assertEqual(resolved, "hakurei_shrine")

    async def test_resolve_initial_scene_ignores_unknown_default(self):
        manager = self._manager(default_scene="does_not_exist")
        resolved = await manager.resolve_initial_scene(None)
        self.assertIsNone(resolved)

    async def test_injection_context_only_once_per_session(self):
        manager = self._manager(default_scene="hakurei_shrine")
        manager.reset_for_session("hakurei_shrine")

        first = await manager.build_injection_context()
        self.assertIsNotNone(first)
        self.assertIn("博丽神社", first)

        # 第二次同一会话不再注入
        second = await manager.build_injection_context()
        self.assertIsNone(second)

        # 新会话重置后又能注入一次
        manager.reset_for_session("magic_forest")
        third = await manager.build_injection_context()
        self.assertIsNotNone(third)
        self.assertIn("魔法森林", third)

    async def test_injection_context_none_without_current_scene(self):
        manager = self._manager()
        manager.reset_for_session(None)
        self.assertIsNone(await manager.build_injection_context())

    async def test_injection_lists_all_scene_ids_when_free_movement(self):
        # 默认 enforce_connectivity=False：可自由飞行，注入应列出全部场景 id
        manager = self._manager(default_scene="hakurei_shrine")
        manager.reset_for_session("hakurei_shrine")
        ctx = await manager.build_injection_context()
        self.assertIsNotNone(ctx)
        self.assertIn("可前往的场景", ctx)
        # 全部 3 个场景 id 都应出现（含不与神社相邻的竹林）
        self.assertIn("hakurei_shrine", ctx)
        self.assertIn("magic_forest", ctx)
        self.assertIn("bamboo_forest", ctx)

    async def test_injection_lists_only_connected_when_enforced(self):
        # enforce_connectivity=True：仅列相邻场景（神社只连魔法森林）
        manager = self._manager(default_scene="hakurei_shrine", enforce_connectivity=True)
        manager.reset_for_session("hakurei_shrine")
        ctx = await manager.build_injection_context()
        self.assertIsNotNone(ctx)
        self.assertIn("magic_forest", ctx)
        # 不相邻的竹林不应出现在可前往清单里
        self.assertNotIn("bamboo_forest", ctx)

    async def test_render_available_scenes_free_lists_every_scene(self):
        manager = self._manager()
        await manager.load_library()
        current = await manager.get_scene("hakurei_shrine")
        listing = await manager.render_available_scenes(current)
        for sid in ("hakurei_shrine", "magic_forest", "bamboo_forest"):
            self.assertIn(sid, listing)


class BeginSceneNormalizationTests(unittest.TestCase):
    def _normalize(self, value):
        from GensokyoAI.core.character_validator import CharacterValidator

        return CharacterValidator()._normalize_begin_scene(value)

    def test_none_returns_none(self):
        self.assertIsNone(self._normalize(None))

    def test_legacy_string_becomes_action_only(self):
        begin = self._normalize("正在扫院子")
        self.assertIsNotNone(begin)
        self.assertIsNone(begin.scene)
        self.assertEqual(begin.action, "正在扫院子")

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._normalize("   "))

    def test_structured_form(self):
        begin = self._normalize({"scene": "hakurei_shrine", "action": "正在扫院子"})
        self.assertEqual(begin.scene, "hakurei_shrine")
        self.assertEqual(begin.action, "正在扫院子")

    def test_structured_scene_only(self):
        begin = self._normalize({"scene": "hakurei_shrine"})
        self.assertEqual(begin.scene, "hakurei_shrine")
        self.assertEqual(begin.action, "")
        self.assertTrue(begin.has_content)

    def test_structured_empty_returns_none(self):
        self.assertIsNone(self._normalize({"scene": "  ", "action": ""}))

    def test_character_validator_accepts_structured_begin_scene(self):
        from GensokyoAI.core.character_validator import CharacterValidator

        validator = CharacterValidator()
        diagnostics = validator.validate_character_dict(
            {
                "name": "灵梦",
                "system_prompt": "你是灵梦。",
                "begin_scene": {"scene": "hakurei_shrine", "action": "正在扫院子"},
            }
        )
        errors = [d for d in diagnostics if d.severity == "error"]
        self.assertEqual(errors, [])

    def test_character_validator_rejects_unknown_begin_scene_field(self):
        from GensokyoAI.core.character_validator import CharacterValidator

        validator = CharacterValidator()
        diagnostics = validator.validate_character_dict(
            {
                "name": "灵梦",
                "system_prompt": "你是灵梦。",
                "begin_scene": {"scene": "hakurei_shrine", "bogus": 1},
            }
        )
        codes = {d.code for d in diagnostics if d.severity == "error"}
        self.assertIn("character.begin_scene.field.unknown", codes)


class SceneResolutionPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.library = Path(self._tmp.name)
        _write_scene(self.library, "hakurei_shrine", name="博丽神社")
        _write_scene(self.library, "magic_forest", name="魔法森林")
        _write_scene(self.library, "bamboo_forest", name="迷途竹林")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _manager(self, **overrides):
        return SceneManager(SceneConfig(enabled=True, library_path=self.library, **overrides))

    async def test_character_begin_scene_beats_default(self):
        manager = self._manager(default_scene="bamboo_forest")
        manager.set_character_begin_scene("magic_forest")
        resolved = await manager.resolve_initial_scene(None)
        self.assertEqual(resolved, "magic_forest")

    async def test_session_beats_character_begin_scene(self):
        manager = self._manager(default_scene="bamboo_forest")
        manager.set_character_begin_scene("magic_forest")
        resolved = await manager.resolve_initial_scene("hakurei_shrine")
        self.assertEqual(resolved, "hakurei_shrine")

    async def test_falls_back_to_default_when_character_scene_unknown(self):
        manager = self._manager(default_scene="bamboo_forest")
        manager.set_character_begin_scene("nonexistent")
        resolved = await manager.resolve_initial_scene(None)
        self.assertEqual(resolved, "bamboo_forest")


class SceneToolWhitelistTests(unittest.TestCase):
    def test_scene_tools_require_whitelist(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        # 未加入 builtin_tools 时不注入场景工具
        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=["time"]),
                model_config=ModelConfig(provider="openai", name="gpt-4o"),
                model_capabilities={ProviderCapability.CHAT, ProviderCapability.TOOLS},
            )
        )
        names = [schema["function"]["name"] for schema in result.tools]
        self.assertNotIn("scene_switch", names)
        self.assertNotIn("get_current_scene", names)

    def test_scene_tools_injected_when_whitelisted(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=["scene"]),
                model_config=ModelConfig(provider="openai", name="gpt-4o"),
                model_capabilities={ProviderCapability.CHAT, ProviderCapability.TOOLS},
            )
        )
        names = [schema["function"]["name"] for schema in result.tools]
        self.assertIn("scene_switch", names)
        self.assertIn("get_current_scene", names)


class SceneAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """通过真实 Agent + EventBus 验证工具→监听器→manager→广播→持久化全链路。"""

    @classmethod
    def setUpClass(cls):
        from GensokyoAI.core.agent.providers import ProviderFactory
        from GensokyoAI.core.agent.providers.base import BaseProvider
        from GensokyoAI.core.agent.types import UnifiedResponse

        class _SceneTestProvider(BaseProvider):
            async def chat(self, model, messages, tools=None, options=None, **kwargs):
                return UnifiedResponse(model=model)

            async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
                if False:
                    yield None

        ProviderFactory.register("scene_test", _SceneTestProvider)

    async def asyncSetUp(self):
        from unittest.mock import patch

        import yaml

        from GensokyoAI.core.agent import Agent
        from GensokyoAI.core.config import AppConfig, CharacterConfig, ModelConfig, SessionConfig

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.library = base / "scenes"
        self.library.mkdir()
        self.sessions = base / "sessions"

        for sid, name, connected in [
            ("hakurei_shrine", "博丽神社", ["magic_forest"]),
            ("magic_forest", "魔法森林", ["hakurei_shrine"]),
        ]:
            (self.library / f"{sid}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "id": sid,
                        "name": name,
                        "description": f"{name}的描述。",
                        "connected_scenes": connected,
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

        config = AppConfig(
            character=CharacterConfig(name="Reimu", system_prompt="你是灵梦。"),
            model=ModelConfig(provider="scene_test", name="test-model"),
            session=SessionConfig(save_path=self.sessions),
            scene=SceneConfig(
                enabled=True,
                library_path=self.library,
                default_scene="hakurei_shrine",
            ),
        )
        with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
            self.agent = Agent(config=config)
        self.agent.create_session()
        await self.agent.event_bus.start()
        await self.agent.scene_manager.load_library()
        await self.agent._sync_scene_for_current_session()

    async def asyncTearDown(self):
        await self.agent.event_bus.stop()
        self._tmp.cleanup()

    async def test_default_scene_synced_and_persisted(self):
        self.assertEqual(self.agent.scene_manager.current_scene_id, "hakurei_shrine")
        session = self.agent.session_manager.get_current_session()
        self.assertEqual(session.metadata.get("current_scene_id"), "hakurei_shrine")

    async def test_scene_switch_tool_flow_broadcasts_and_persists(self):
        from GensokyoAI.core.events import SystemEvent
        from GensokyoAI.tools.tool_builtin.scene import get_current_scene, scene_switch
        from GensokyoAI.tools.tool_context import bind_event_bus

        broadcasts: list[dict] = []
        self.agent.event_bus.subscribe(
            SystemEvent.SCENE_SWITCHED,
            lambda event: broadcasts.append(event.data),
        )

        # 事件总线现由 ToolExecutor 按调用注入；直接调用工具需显式绑定。
        with bind_event_bus(self.agent.event_bus):
            result = await scene_switch("magic_forest")
            self.assertIn("魔法森林", result)

            # 状态更新
            self.assertEqual(self.agent.scene_manager.current_scene_id, "magic_forest")
            # 持久化到会话
            session = self.agent.session_manager.get_current_session()
            self.assertEqual(session.metadata.get("current_scene_id"), "magic_forest")
            # 广播事件
            self.assertTrue(broadcasts)
            self.assertEqual(broadcasts[-1]["scene_id"], "magic_forest")

            # get_current_scene 工具能读回
            current = await get_current_scene()
            self.assertIn("魔法森林", current)

    async def test_scene_switch_invalid_returns_error_message(self):
        from GensokyoAI.tools.tool_builtin.scene import scene_switch
        from GensokyoAI.tools.tool_context import bind_event_bus

        with bind_event_bus(self.agent.event_bus):
            result = await scene_switch("nonexistent")
        self.assertIn("不存在", result)
        # 当前场景不变
        self.assertEqual(self.agent.scene_manager.current_scene_id, "hakurei_shrine")


class SceneRuntimeRpcTests(unittest.IsolatedAsyncioTestCase):
    """通过真实 RuntimeService + Agent 验证 scene.* RPC 方法全链路。"""

    @classmethod
    def setUpClass(cls):
        from GensokyoAI.core.agent.providers import ProviderFactory
        from GensokyoAI.core.agent.providers.base import BaseProvider
        from GensokyoAI.core.agent.types import UnifiedResponse

        class _SceneRpcProvider(BaseProvider):
            async def chat(self, model, messages, tools=None, options=None, **kwargs):
                return UnifiedResponse(model=model)

            async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
                if False:
                    yield None

        ProviderFactory.register("scene_rpc_test", _SceneRpcProvider)

    async def asyncSetUp(self):
        from unittest.mock import patch

        import yaml

        from GensokyoAI.core.agent import Agent
        from GensokyoAI.core.config import AppConfig, CharacterConfig, ModelConfig, SessionConfig
        from GensokyoAI.runtime.service import RuntimeService

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.library = base / "scenes"
        self.library.mkdir()

        for sid, name, connected in [
            ("hakurei_shrine", "博丽神社", ["magic_forest"]),
            ("magic_forest", "魔法森林", ["hakurei_shrine"]),
        ]:
            (self.library / f"{sid}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "id": sid,
                        "name": name,
                        "description": f"{name}的描述。",
                        "connected_scenes": connected,
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

        config = AppConfig(
            character=CharacterConfig(name="Reimu", system_prompt="你是灵梦。"),
            model=ModelConfig(provider="scene_rpc_test", name="test-model"),
            session=SessionConfig(save_path=base / "sessions"),
            scene=SceneConfig(
                enabled=True,
                library_path=self.library,
                default_scene="hakurei_shrine",
            ),
        )
        with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
            self.agent = Agent(config=config)
        self.agent.create_session()
        await self.agent.event_bus.start()
        await self.agent.scene_manager.load_library()
        await self.agent._sync_scene_for_current_session()

        self.service = RuntimeService()
        self.service.state.agent = self.agent

    async def asyncTearDown(self):
        await self.agent.event_bus.stop()
        self._tmp.cleanup()

    async def test_scene_methods_listed_in_runtime_info(self):
        info = await self.service.handle("runtime.info")
        for m in ("scene.current", "scene.list", "scene.get", "scene.switch", "scene.graph"):
            self.assertIn(m, info["methods"])

    async def test_scene_current(self):
        payload = await self.service.handle("scene.current")
        self.assertEqual(payload["id"], "hakurei_shrine")
        self.assertEqual(payload["name"], "博丽神社")
        self.assertIn("magic_forest", payload["connected_scenes"])

    async def test_scene_list(self):
        payload = await self.service.handle("scene.list")
        ids = {s["id"] for s in payload}
        self.assertEqual(ids, {"hakurei_shrine", "magic_forest"})

    async def test_scene_get(self):
        payload = await self.service.handle("scene.get", {"scene_id": "magic_forest"})
        self.assertEqual(payload["name"], "魔法森林")

    async def test_scene_get_unknown_returns_structured_error(self):
        payload = await self.service.handle(
            "scene.get", {"scene_id": "nope"}, structured_errors=True
        )
        self.assertIn("error", payload)
        with self.assertRaises(ValueError):
            await self.service.handle("scene.get", {"scene_id": "nope"})

    async def test_scene_switch_persists_and_returns(self):
        from GensokyoAI.core.events import SystemEvent

        broadcasts: list[dict] = []
        self.agent.event_bus.subscribe(
            SystemEvent.SCENE_SWITCHED,
            lambda event: broadcasts.append(event.data),
        )

        payload = await self.service.handle("scene.switch", {"scene_id": "magic_forest"})
        self.assertTrue(payload["switched"])
        self.assertEqual(payload["scene"]["id"], "magic_forest")
        # 复用工具路径：状态、持久化、广播三者一致
        self.assertEqual(self.agent.scene_manager.current_scene_id, "magic_forest")
        session = self.agent.session_manager.get_current_session()
        self.assertEqual(session.metadata.get("current_scene_id"), "magic_forest")
        self.assertTrue(broadcasts)
        self.assertEqual(broadcasts[-1]["scene_id"], "magic_forest")

    async def test_scene_switch_unknown_returns_structured_error(self):
        payload = await self.service.handle(
            "scene.switch", {"scene_id": "nonexistent"}, structured_errors=True
        )
        self.assertIn("error", payload)
        # 当前场景保持不变
        self.assertEqual(self.agent.scene_manager.current_scene_id, "hakurei_shrine")
        with self.assertRaises(ValueError):
            await self.service.handle("scene.switch", {"scene_id": "nonexistent"})

    async def test_scene_graph(self):
        graph = await self.service.handle("scene.graph")
        node_ids = {n["id"] for n in graph["nodes"]}
        self.assertEqual(node_ids, {"hakurei_shrine", "magic_forest"})
        self.assertEqual(graph["current_scene_id"], "hakurei_shrine")
        # 神社 → 魔法森林 的连通边存在
        self.assertIn({"from": "hakurei_shrine", "to": "magic_forest"}, graph["edges"])


if __name__ == "__main__":
    unittest.main()
