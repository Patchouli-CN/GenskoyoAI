import asyncio
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import msgspec
import yaml

from GensokyoAI.core.agent.types import ModelInfo, ProviderCapability, StreamChunk
from GensokyoAI.core.config import (
    CharacterValidator,
    ConfigLoader,
    EmbeddingConfig,
    ModelConfig,
    ResourceControlConfig,
)
from GensokyoAI.core.events import Event, EventBus, SystemEvent
from GensokyoAI.core.migrations import clear_migration_diagnostics
from GensokyoAI.core.schema_versions import (
    CHARACTER_PACKAGE_SCHEMA_VERSION,
    CONFIG_SCHEMA_VERSION,
    MEMORY_SCHEMA_VERSION,
    SESSION_EXPORT_SCHEMA_VERSION,
    SESSION_SCHEMA_VERSION,
)
from GensokyoAI.core.version import package_version
from GensokyoAI.memory.topic_store import TopicAwareStore
from GensokyoAI.runtime.dependencies import (
    OPTIONAL_PROVIDER_DEPENDENCIES,
    DependencyError,
    dependency_status,
    packages_for_providers,
)
from GensokyoAI.runtime.rpc import (
    RUNTIME_BREAKING_CHANGES,
    RUNTIME_PROTOCOL_MAJOR_VERSION,
    RUNTIME_PROTOCOL_VERSION,
    RpcMethodNotFoundError,
    dispatch_rpc,
    legacy_rpc_methods,
    resolve_rpc_handler,
    rpc_methods,
)
from GensokyoAI.runtime.service import RuntimeService, runtime_compatibility_notes
from GensokyoAI.session.context import SessionContext
from GensokyoAI.session.persistence import SessionPersistence
from GensokyoAI.tools.errors import ToolError, ToolExecutionError


class RuntimeDependencyTests(unittest.TestCase):
    def test_dependency_mapping_includes_expected_provider_aliases(self):
        self.assertEqual(OPTIONAL_PROVIDER_DEPENDENCIES["deepseek"], ["openai>=1.0.0"])
        self.assertEqual(
            packages_for_providers(["openai", "deepseek", "openai_responses"]),
            ["openai>=1.0.0"],
        )

    def test_dependency_status_reports_missing_imports(self):
        def fake_find_spec(name):
            return object() if name == "openai" else None

        with patch("importlib.util.find_spec", side_effect=fake_find_spec):
            status = dependency_status(["deepseek", "claude"])

        self.assertTrue(status["providers"]["deepseek"]["installed"])
        self.assertFalse(status["providers"]["claude"]["installed"])
        self.assertEqual(status["providers"]["claude"]["missing_imports"], ["anthropic"])

    def test_dependency_status_rejects_unknown_provider(self):
        with self.assertRaises(DependencyError) as ctx:
            dependency_status(["not-a-provider"])

        self.assertEqual(ctx.exception.code, "unsupported_provider_dependency")
        self.assertIn("not-a-provider", ctx.exception.details["providers"])

    def test_runtime_service_exposes_dependency_and_model_methods(self):
        service = RuntimeService()

        async def run():
            with patch("importlib.util.find_spec", return_value=None):
                status = await service.handle(
                    "dependency.status",
                    {"providers": ["openai"]},
                )
            legacy = await service.handle("dependency_status", {"providers": []})
            info = await service.handle("runtime.info")
            return status, legacy, info

        status, legacy, info = asyncio.run(run())

        self.assertIn("openai", status["providers"])
        self.assertEqual(legacy["providers"], {})
        self.assertIn("dependency.status", info["methods"])
        self.assertIn("model.list", info["methods"])
        self.assertIn("model.info", info["methods"])
        self.assertIn("install_dependencies", info["legacy_methods"])


class FakeModelRegistry:
    def __init__(self):
        self.list_calls = []
        self.info_calls = []

    async def list_models(self, config, *, refresh=False, overrides=None):
        self.list_calls.append((config, refresh, overrides))
        return [
            ModelInfo(
                id="gpt-test",
                name="GPT Test",
                context_window=4096,
                capabilities=[ProviderCapability.CHAT, ProviderCapability.TOOLS],
                owned_by="tests",
                metadata={"source": "fake"},
            )
        ]

    async def get_model_info(self, config, model_id=None, *, refresh=False, overrides=None):
        self.info_calls.append((config, model_id, refresh, overrides))
        return ModelInfo(
            id=model_id or config.name,
            name="Selected Test Model",
            context_window=8192,
            capabilities=[ProviderCapability.CHAT],
            owned_by="tests",
            metadata={"selected": True},
        )


class MigrationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        clear_migration_diagnostics()

    def tearDown(self):
        clear_migration_diagnostics()

    def test_runtime_info_exposes_empty_migration_diagnostics_and_schema_versions(self):
        service = RuntimeService()

        async def run():
            return await service.handle("runtime.info")

        info = asyncio.run(run())

        self.assertEqual(info["package_version"], package_version())
        self.assertIn("migration.diagnostics", info["capabilities"])
        self.assertIn("runtime.versioning", info["capabilities"])
        self.assertEqual(info["schema_versions"]["config"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(info["schema_versions"]["session"], SESSION_SCHEMA_VERSION)
        self.assertEqual(info["schema_versions"]["memory"], MEMORY_SCHEMA_VERSION)
        self.assertEqual(info["schema_versions"]["session_export"], SESSION_EXPORT_SCHEMA_VERSION)
        self.assertEqual(
            info["schema_versions"]["character_package"], CHARACTER_PACKAGE_SCHEMA_VERSION
        )
        self.assertEqual(info["deprecated_fields"], [])
        self.assertEqual(info["compatibility_notes"], runtime_compatibility_notes())
        self.assertTrue(info["compatibility_notes"])
        self.assertEqual(info["compatibility_notes"][0]["scope"], "runtime.rpc.legacy_methods")
        self.assertEqual(info["compatibility_notes"][0]["status"], "deprecated")
        self.assertEqual(info["migration_diagnostics"]["recent"], [])
        self.assertEqual(info["migration_diagnostics"]["counts"]["migrated"], 0)

    def test_session_migration_records_runtime_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="reimu", metadata={"title": "legacy"})
            session_file = Path(tmp) / "reimu" / f"{session.session_id}.json"
            session_file.parent.mkdir(parents=True)
            session_file.write_text(
                json.dumps(
                    {
                        "session": session.to_dict(),
                        "messages": [{"role": "user", "content": "legacy"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            persistence.rebuild_index()

            persistence.load_session("reimu", session.session_id)
            info = asyncio.run(RuntimeService().handle("runtime.info"))

            diagnostics = info["migration_diagnostics"]
            self.assertEqual(diagnostics["counts"]["migrated"], 1)
            item = diagnostics["recent"][-1]
            self.assertEqual(item["source"], "session")
            self.assertEqual(item["status"], "migrated")
            self.assertIsNone(item["from_schema_version"])
            self.assertEqual(item["to_schema_version"], SESSION_SCHEMA_VERSION)
            self.assertEqual(item["format"], "gensokyoai.session.file")
            self.assertEqual(item["path"], str(session_file))
            self.assertEqual(
                item["backup_path"], str(session_file.with_name(f"{session_file.name}.bak"))
            )
            self.assertEqual(item["diagnostics"], [])
            self.assertIn("migrated_at", item)

    def test_session_read_failure_records_failed_migration_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="marisa")
            session_file = Path(tmp) / "marisa" / f"{session.session_id}.json"
            session_file.parent.mkdir(parents=True)
            session_file.write_text("{ broken json", encoding="utf-8")
            session_file.with_name(f"{session_file.name}.bak").write_text(
                "{ also broken", encoding="utf-8"
            )
            persistence.rebuild_index()

            with self.assertRaises(json.JSONDecodeError):
                persistence.load_session("marisa", session.session_id)
            info = asyncio.run(RuntimeService().handle("runtime.info"))

            diagnostics = info["migration_diagnostics"]
            self.assertEqual(diagnostics["counts"]["failed"], 1)
            item = diagnostics["recent"][-1]
            self.assertEqual(item["source"], "session")
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["to_schema_version"], SESSION_SCHEMA_VERSION)
            self.assertEqual(item["format"], "gensokyoai.session.file")
            self.assertEqual(item["path"], str(session_file))
            self.assertIn("could not be read", item["message"])
            codes = {diag["code"] for diag in item["diagnostics"]}
            self.assertIn("migration.session.read_failed", codes)
            self.assertIn("migration.session.quarantined", codes)

    def test_memory_topic_store_migration_records_runtime_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            legacy_payload = {
                "topics": [
                    {
                        "id": "topic-1",
                        "name": "偏好",
                        "summary": "喝茶",
                        "created_at": "2024-01-01T00:00:00",
                        "last_updated": "2024-01-01T00:00:00",
                        "last_accessed": "2024-01-01T00:00:00",
                        "message_count": 1,
                        "message_ids": ["memory-1"],
                    }
                ],
                "memories": [
                    {
                        "id": "memory-1",
                        "content": "灵梦喜欢喝茶",
                        "topic_id": "topic-1",
                        "timestamp": "2024-01-01T00:00:00",
                    }
                ],
            }
            path.write_bytes(msgspec.json.encode(legacy_payload))

            TopicAwareStore(path)
            info = asyncio.run(RuntimeService().handle("runtime.info"))

            diagnostics = info["migration_diagnostics"]
            self.assertEqual(diagnostics["counts"]["migrated"], 1)
            item = diagnostics["recent"][-1]
            self.assertEqual(item["source"], "memory.topic_store")
            self.assertEqual(item["status"], "migrated")
            self.assertIsNone(item["from_schema_version"])
            self.assertEqual(item["to_schema_version"], MEMORY_SCHEMA_VERSION)
            self.assertEqual(item["format"], "gensokyoai.memory.topic_store")
            self.assertEqual(item["path"], str(path))
            self.assertIsNone(item["backup_path"])

    def test_memory_topic_store_load_failure_records_failed_migration_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            path.write_bytes(b"{ broken json")

            TopicAwareStore(path)
            info = asyncio.run(RuntimeService().handle("runtime.info"))

            diagnostics = info["migration_diagnostics"]
            self.assertEqual(diagnostics["counts"]["failed"], 1)
            item = diagnostics["recent"][-1]
            self.assertEqual(item["source"], "memory.topic_store")
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["to_schema_version"], MEMORY_SCHEMA_VERSION)
            self.assertEqual(item["format"], "gensokyoai.memory.topic_store")
            self.assertEqual(item["path"], str(path))
            self.assertIn("could not be loaded", item["message"])
            self.assertEqual(
                item["diagnostics"][0]["code"], "migration.memory.topic_store.load_failed"
            )


class SessionPersistenceIndexTests(unittest.TestCase):
    def test_delete_session_uses_index_before_removing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="reimu")
            persistence.save_session(session)
            session_file = Path(tmp) / "reimu" / f"{session.session_id}.json"
            self.assertTrue(session_file.exists())
            self.assertEqual(persistence._session_index[session.session_id], "reimu")

            deleted = persistence.delete_session(session.session_id)

            self.assertTrue(deleted)
            self.assertFalse(session_file.exists())
            self.assertNotIn(session.session_id, persistence._session_index)

    def test_delete_session_async_uses_index_before_removing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="marisa")
            persistence.save_session(session)
            session_file = Path(tmp) / "marisa" / f"{session.session_id}.json"

            async def run():
                return await persistence.delete_session_async(session.session_id)

            deleted = asyncio.run(run())

            self.assertTrue(deleted)
            self.assertFalse(session_file.exists())
            self.assertNotIn(session.session_id, persistence._session_index)

    def test_save_session_creates_backup_before_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="reimu", metadata={"title": "old"})
            persistence.save_session(session)
            session.metadata["title"] = "new"

            persistence.save_session(session)

            session_file = Path(tmp) / "reimu" / f"{session.session_id}.json"
            backup_file = session_file.with_name(f"{session_file.name}.bak")
            self.assertTrue(backup_file.exists())
            backup_data = json.loads(backup_file.read_text(encoding="utf-8"))
            current_data = json.loads(session_file.read_text(encoding="utf-8"))
            self.assertEqual(backup_data["session"]["metadata"]["title"], "old")
            self.assertEqual(current_data["session"]["metadata"]["title"], "new")
            self.assertEqual(current_data["schema_version"], SESSION_SCHEMA_VERSION)
            self.assertFalse(list(session_file.parent.glob("*.tmp")))

    def test_load_messages_recovers_from_backup_when_json_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="reimu")
            persistence.save_session(session)
            persistence.save_messages(session.session_id, [{"role": "user", "content": "backup"}])
            session_file = Path(tmp) / "reimu" / f"{session.session_id}.json"
            backup_file = session_file.with_name(f"{session_file.name}.bak")
            self.assertTrue(backup_file.exists())
            session_file.write_text("{ broken json", encoding="utf-8")

            messages = persistence.load_messages(session.session_id)

            self.assertEqual(messages, [])
            restored_data = json.loads(session_file.read_text(encoding="utf-8"))
            self.assertEqual(restored_data["session"]["session_id"], session.session_id)
            self.assertFalse((session_file.parent / "quarantine").exists())

    def test_load_session_quarantines_corrupt_json_when_backup_is_unusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="marisa")
            persistence.save_session(session)
            session_file = Path(tmp) / "marisa" / f"{session.session_id}.json"
            session_file.write_text("{ broken json", encoding="utf-8")
            session_file.with_name(f"{session_file.name}.bak").write_text(
                "{ also broken", encoding="utf-8"
            )

            with self.assertRaises(json.JSONDecodeError):
                persistence.load_session("marisa", session.session_id)

            self.assertFalse(session_file.exists())
            quarantined = list((Path(tmp) / "marisa" / "quarantine").glob("*.bad"))
            self.assertEqual(len(quarantined), 1)
            self.assertIn(session.session_id, quarantined[0].name)

    def test_async_load_messages_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="sanae")
            persistence.save_session(session)
            persistence.save_messages(session.session_id, [{"role": "user", "content": "backup"}])
            session_file = Path(tmp) / "sanae" / f"{session.session_id}.json"
            session_file.write_text("{ broken json", encoding="utf-8")

            messages = asyncio.run(persistence.load_messages_async(session.session_id))

            self.assertEqual(messages, [])
            self.assertTrue(session_file.exists())

    def test_load_legacy_session_file_migrates_schema_metadata_and_preserves_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SessionPersistence(Path(tmp))
            session = SessionContext(character_id="reimu", metadata={"title": "legacy"})
            session_file = Path(tmp) / "reimu" / f"{session.session_id}.json"
            session_file.parent.mkdir(parents=True)
            legacy_payload = {
                "session": session.to_dict(),
                "messages": [{"role": "user", "content": "legacy"}],
            }
            session_file.write_text(
                json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8"
            )
            persistence.rebuild_index()

            loaded = persistence.load_session("reimu", session.session_id)
            messages = persistence.load_messages(session.session_id)

            self.assertIsNotNone(loaded)
            self.assertEqual(messages, legacy_payload["messages"])
            migrated = json.loads(session_file.read_text(encoding="utf-8"))
            backup = json.loads(
                session_file.with_name(f"{session_file.name}.bak").read_text(encoding="utf-8")
            )
            self.assertEqual(migrated["schema_version"], SESSION_SCHEMA_VERSION)
            self.assertEqual(migrated["format"], "gensokyoai.session.file")
            self.assertEqual(migrated["created_by"], "GensokyoAI")
            self.assertEqual(migrated["migration_history"][0]["to_version"], SESSION_SCHEMA_VERSION)
            self.assertNotIn("schema_version", backup)

    def test_topic_store_migrates_legacy_payload_and_writes_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            legacy_payload = {
                "topics": [
                    {
                        "id": "topic-1",
                        "name": "偏好",
                        "summary": "喝茶",
                        "created_at": "2024-01-01T00:00:00",
                        "last_updated": "2024-01-01T00:00:00",
                        "last_accessed": "2024-01-01T00:00:00",
                        "message_count": 1,
                        "message_ids": ["memory-1"],
                    }
                ],
                "memories": [
                    {
                        "id": "memory-1",
                        "content": "灵梦喜欢喝茶",
                        "topic_id": "topic-1",
                        "timestamp": "2024-01-01T00:00:00",
                    }
                ],
            }
            path.write_bytes(msgspec.json.encode(legacy_payload))

            store = TopicAwareStore(path)
            memories = store.get_all()
            migrated = msgspec.json.decode(path.read_bytes())

            self.assertEqual(store.topic_count, 1)
            self.assertEqual(store.memory_count, 1)
            self.assertEqual(memories[0]["topic_name"], "偏好")
            self.assertEqual(migrated["schema_version"], MEMORY_SCHEMA_VERSION)
            self.assertEqual(migrated["format"], "gensokyoai.memory.topic_store")
            self.assertEqual(migrated["created_by"], "GensokyoAI")
            self.assertEqual(migrated["migration_history"][0]["to_version"], MEMORY_SCHEMA_VERSION)


class PackagingConfigurationTests(unittest.TestCase):
    def _load_pyproject(self) -> dict[str, Any]:
        with open(Path("pyproject.toml"), "rb") as f:
            return tomllib.load(f)

    def _requirements_entries(self) -> list[str]:
        entries = []
        for line in Path("requirements.txt").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            entries.append(stripped.split("#", 1)[0].strip())
        return entries

    def test_requirements_are_minimal_core_dependencies_without_provider_sdks(self):
        requirements = self._requirements_entries()

        self.assertIn("aiohttp>=3.9", requirements)
        self.assertIn("ayafileio>=1.4.8", requirements)
        self.assertIn("ddgs>=9.14.4", requirements)
        self.assertNotIn("ollama", requirements)
        self.assertNotIn("openai", requirements)
        self.assertNotIn("anthropic", requirements)
        self.assertNotIn("google-genai", requirements)

    def test_release_package_version_and_core_dependencies_are_consistent(self):
        pyproject = self._load_pyproject()
        project = pyproject["project"]
        requirements = set(self._requirements_entries())

        self.assertEqual(project["version"], "2026.7.14.0")
        self.assertEqual(package_version(), "2026.7.14.0")
        self.assertEqual(set(project["dependencies"]), requirements)

    def test_pyproject_declares_default_all_and_dev_dependency_groups(self):
        pyproject = self._load_pyproject()
        optional = pyproject["project"]["optional-dependencies"]
        groups = pyproject["dependency-groups"]

        self.assertEqual(optional["default"], ["ollama"])
        self.assertEqual(optional["deepseek"], OPTIONAL_PROVIDER_DEPENDENCIES["deepseek"])
        self.assertEqual(optional["openrouter"], OPTIONAL_PROVIDER_DEPENDENCIES["openrouter"])
        self.assertIn("ollama", optional["all"])
        self.assertIn("openai>=1.0.0", optional["all"])
        self.assertIn("pytest>=8.0", groups["dev"])
        self.assertIn("ruff>=0.6.0", groups["dev"])
        self.assertIn("pyright>=1.1.390", groups["dev"])
        self.assertIn("build>=1.2.0", groups["dev"])
        self.assertEqual(
            pyproject["tool"]["pytest"]["ini_options"]["testpaths"],
            ["tests", "tests_security"],
        )
        self.assertNotIn("asyncio_mode", pyproject["tool"]["pytest"]["ini_options"])
        self.assertEqual(pyproject["tool"]["coverage"]["report"]["fail_under"], 45)
        self.assertEqual(pyproject["tool"]["pyright"]["typeCheckingMode"], "basic")


class RuntimeModelRpcTests(unittest.TestCase):
    def test_model_list_and_info_return_json_compatible_model_metadata(self):
        service = RuntimeService()
        fake_registry = FakeModelRegistry()
        cast(Any, service)._model_registry = fake_registry
        cast(Any, service.state).agent = SimpleNamespace(
            config=SimpleNamespace(model=ModelConfig(provider="openai", name="gpt-test"))
        )

        async def run():
            listed = await service.handle(
                "model.list",
                {
                    "refresh": True,
                    "overrides": {
                        "gpt-test": {
                            "capabilities_add": ["custom"],
                        }
                    },
                },
            )
            info = await service.handle("model.info", {"model_id": "gpt-test-v2"})
            return listed, info

        listed, info = asyncio.run(run())

        self.assertEqual(listed["provider"], "openai")
        self.assertEqual(listed["model"], "gpt-test")
        self.assertEqual(listed["models"][0]["id"], "gpt-test")
        self.assertEqual(listed["models"][0]["context_window"], 4096)
        self.assertIn(ProviderCapability.TOOLS, listed["models"][0]["capabilities"])
        self.assertEqual(listed["models"][0]["metadata"], {"source": "fake"})
        self.assertTrue(fake_registry.list_calls[0][1])
        self.assertIn("gpt-test", fake_registry.list_calls[0][2])

        self.assertEqual(info["provider"], "openai")
        self.assertEqual(info["requested_model"], "gpt-test-v2")
        self.assertEqual(info["model"]["id"], "gpt-test-v2")
        self.assertEqual(info["model"]["metadata"], {"selected": True})
        self.assertEqual(fake_registry.info_calls[0][1], "gpt-test-v2")


class RuntimeRpcDispatchTests(unittest.TestCase):
    def test_rpc_method_lists_are_owned_by_runtime_rpc_module(self):
        methods = rpc_methods()
        added_since_v2026_5_13_0 = {
            "session.messages",
            "session.replace_messages",
            "session.regenerate_from",
            "initiative_timer.current",
            "initiative_timer.update",
            "initiative_timer.cancel",
            "initiative_timer.trigger",
            "initiative_timer.hesitation",
            "initiative_timer.hesitation.set",
            "scene.current",
            "scene.list",
            "scene.get",
            "scene.switch",
            "scene.graph",
        }

        self.assertEqual(len(added_since_v2026_5_13_0), 14)
        self.assertTrue(added_since_v2026_5_13_0.issubset(methods))
        self.assertIn("runtime.info", methods)
        self.assertIn("dependency.status", methods)
        self.assertIn("model.list", methods)
        self.assertIn("model.info", methods)
        self.assertIn("agent.send_message_stream", methods)
        self.assertIn("session.current", methods)
        self.assertIn("session.delete", methods)
        self.assertIn("session.export", methods)
        self.assertIn("session.rename", methods)
        self.assertIn("session.messages", methods)
        self.assertIn("session.replace_messages", methods)
        self.assertIn("session.regenerate_from", methods)
        self.assertIn("session.rollback", methods)
        self.assertNotIn("init", rpc_methods())
        self.assertIn("init", legacy_rpc_methods())
        self.assertIn("install_dependencies", legacy_rpc_methods())
        self.assertIn("send_message_stream", legacy_rpc_methods())
        self.assertIn("memory.list", rpc_methods())
        self.assertIn("memory.search", rpc_methods())
        self.assertIn("memory.get", rpc_methods())
        self.assertIn("memory.update", rpc_methods())
        self.assertIn("memory.delete", rpc_methods())
        self.assertIn("memory.graph", rpc_methods())
        self.assertIn("character.validate", rpc_methods())

    def test_runtime_protocol_metadata_documents_versions_and_deprecations(self):
        service = RuntimeService()

        async def run():
            return await service.handle("runtime.info")

        info = asyncio.run(run())

        self.assertEqual(RUNTIME_PROTOCOL_VERSION, "2.0.0")
        self.assertEqual(RUNTIME_PROTOCOL_MAJOR_VERSION, 2)
        self.assertTrue(RUNTIME_BREAKING_CHANGES)
        self.assertEqual(info["protocol_version"], "2.0.0")
        self.assertEqual(info["protocol_major_version"], 2)
        self.assertEqual(info["package_version"], "2026.7.14.0")
        self.assertEqual(info["schema_versions"]["memory"], 2)
        self.assertIn("agent.streaming", info["capabilities"])
        self.assertIn("agent.reasoning.public", info["capabilities"])
        self.assertIn("runtime.events", info["capabilities"])
        self.assertIn("runtime.transport_discovery", info["capabilities"])
        self.assertIn("memory.management", info["capabilities"])
        self.assertEqual(info["stream_protocol"]["reasoning_default"], "public")
        self.assertIn("generation_id", info["stream_protocol"]["correlation_fields"])
        self.assertEqual(
            {transport["name"] for transport in info["transports"]},
            {"json-lines", "http", "websocket", "sse"},
        )
        self.assertEqual(info["breaking_changes"], list(RUNTIME_BREAKING_CHANGES))
        self.assertTrue(info["method_specs"])
        self.assertEqual(info["deprecated_fields"], [])
        self.assertEqual(info["compatibility_notes"], runtime_compatibility_notes())
        self.assertEqual(info["compatibility_notes"][0]["scope"], "runtime.rpc.legacy_methods")
        self.assertEqual(info["compatibility_notes"][0]["status"], "deprecated")
        self.assertIn("replacement", info["compatibility_notes"][0])

        legacy_init = next(item for item in info["deprecated_methods"] if item["method"] == "init")
        self.assertTrue(legacy_init["deprecated"])
        self.assertEqual(legacy_init["replacement"], "agent.init")
        self.assertEqual(legacy_init["remove_after"], "2.0.0")

        runtime_info = next(
            item for item in info["method_specs"] if item["method"] == "runtime.info"
        )
        self.assertEqual(runtime_info["namespace"], "runtime")
        self.assertFalse(runtime_info["legacy"])
        self.assertFalse(runtime_info["deprecated"])

    def test_runtime_api_document_mentions_declared_versioning_metadata(self):
        text = Path("docs/runtime_api.md").read_text(encoding="utf-8")

        self.assertIn("runtime.rpc.legacy_methods", text)
        self.assertIn("deprecated_fields", text)
        self.assertIn("compatibility_notes", text)
        for note in runtime_compatibility_notes():
            self.assertIn(note["scope"], text)
            self.assertIn(note["status"], text)

    def test_resolve_rpc_handler_maps_namespaced_and_legacy_methods(self):
        service = RuntimeService()

        self.assertEqual(resolve_rpc_handler(service, "runtime.info").__name__, "info")
        self.assertEqual(resolve_rpc_handler(service, "init").__name__, "init")
        self.assertEqual(
            resolve_rpc_handler(service, "dependency.status").__name__,
            "dependency_status",
        )
        self.assertEqual(resolve_rpc_handler(service, "model.list").__name__, "list_models")
        self.assertEqual(resolve_rpc_handler(service, "model.info").__name__, "model_info")
        self.assertEqual(
            resolve_rpc_handler(service, "agent.send_message_stream").__name__,
            "send_message_stream",
        )
        self.assertEqual(
            resolve_rpc_handler(service, "session.current").__name__, "current_session"
        )
        self.assertEqual(resolve_rpc_handler(service, "session.delete").__name__, "delete_session")
        self.assertEqual(resolve_rpc_handler(service, "session.export").__name__, "export_session")
        self.assertEqual(resolve_rpc_handler(service, "session.rename").__name__, "rename_session")
        self.assertEqual(
            resolve_rpc_handler(service, "session.messages").__name__,
            "session_messages",
        )
        self.assertEqual(
            resolve_rpc_handler(service, "session.replace_messages").__name__,
            "session_replace_messages",
        )
        self.assertEqual(
            resolve_rpc_handler(service, "session.regenerate_from").__name__,
            "session_regenerate_from",
        )
        self.assertEqual(
            resolve_rpc_handler(service, "session.rollback").__name__,
            "rollback_session",
        )
        self.assertEqual(resolve_rpc_handler(service, "memory.list").__name__, "memory_list")
        self.assertEqual(resolve_rpc_handler(service, "memory.search").__name__, "memory_search")
        self.assertEqual(resolve_rpc_handler(service, "memory.graph").__name__, "memory_graph")
        self.assertEqual(
            resolve_rpc_handler(service, "character.validate").__name__,
            "validate_character",
        )

    def test_dispatch_rpc_raises_structured_method_not_found_error(self):
        service = RuntimeService()

        async def run():
            await dispatch_rpc(service, "not.registered", {})

        with self.assertRaises(RpcMethodNotFoundError) as ctx:
            asyncio.run(run())

        self.assertEqual(ctx.exception.code, "method_not_found")
        self.assertTrue(ctx.exception.recoverable)
        self.assertEqual(ctx.exception.details["method"], "not.registered")
        self.assertIn("runtime.info", ctx.exception.details["allowed_methods"])

    def test_runtime_service_handle_raises_rpc_error_by_default(self):
        service = RuntimeService()

        async def run():
            return await service.handle("not.registered", {})

        with self.assertRaises(RpcMethodNotFoundError):
            asyncio.run(run())

    def test_dispatch_rpc_can_wrap_tool_execution_error_as_runtime_error_response(self):
        class ToolFailingService:
            async def info(self):
                raise ToolExecutionError(
                    ToolError(
                        error_code="tool.test_failed",
                        technical_message="tool technical failure",
                        user_message="tool user failure",
                        recoverable=False,
                        details={"scope": "runtime"},
                    )
                )

        async def run():
            return await dispatch_rpc(
                ToolFailingService(), "runtime.info", {}, structured_errors=True
            )

        response = asyncio.run(run())

        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "tool.test_failed")
        self.assertEqual(response["error"], "tool technical failure")
        self.assertEqual(response["error_object"]["user_message"], "tool user failure")
        self.assertFalse(response["error_object"]["recoverable"])
        self.assertEqual(response["error_object"]["details"], {"scope": "runtime"})


class FakeWorkingMemory:
    def __init__(self, messages):
        self.messages = messages

    def get_context(self):
        return list(self.messages)


class FakeRuntimeSessionPersistence:
    def __init__(self, manager):
        self.manager = manager
        self.saved_sessions = []

    def load_messages(self, session_id):
        return list(self.manager.messages_by_session.get(session_id, []))

    def save_session(self, session):
        self.saved_sessions.append(session.session_id)

    def replace_messages(self, session_id, messages):
        self.manager.messages_by_session[session_id] = [dict(message) for message in messages]


class FakeRuntimeSessionManager:
    def __init__(self):
        self.current = SessionContext(character_id="reimu", total_turns=1)
        self.sessions = {self.current.session_id: self.current}
        self.messages_by_session = {
            self.current.session_id: [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好呀"},
            ]
        }
        self.deleted = []
        self.saved = False
        self.persistence = FakeRuntimeSessionPersistence(self)

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_current_session(self):
        return self.current

    def list_sessions(self):
        return list(self.sessions.values())

    def get_working_memory(self, session_id=None):
        sid = session_id or (self.current.session_id if self.current else "")
        return FakeWorkingMemory(self.messages_by_session.get(sid, []))

    def set_current_session(self, session_id):
        if session_id not in self.sessions:
            return False
        self.current = self.sessions[session_id]
        return True

    def replace_messages(self, session_id, messages):
        if session_id not in self.sessions:
            return False
        normalized = [dict(message) for message in messages]
        self.messages_by_session[session_id] = normalized
        self.sessions[session_id].total_turns = len(normalized) // 2
        self.sessions[session_id].touch()
        self.persistence.replace_messages(session_id, normalized)
        self.persistence.save_session(self.sessions[session_id])
        return True

    def delete_session(self, session_id):
        if session_id not in self.sessions:
            return False
        self.deleted.append(session_id)
        del self.sessions[session_id]
        self.messages_by_session.pop(session_id, None)
        if self.current and self.current.session_id == session_id:
            self.current = None
        return True

    def save_current(self):
        self.saved = True
        if self.current:
            messages = self.messages_by_session.get(self.current.session_id, [])
            self.current.total_turns = len(messages) // 2


class FakeRuntimeSemanticMemory:
    def __init__(self):
        self.item = {
            "id": "memory-1",
            "content": "灵梦喜欢喝茶",
            "importance": 0.8,
            "topic_name": "偏好",
            "diagnostics": {"embedding_used": False},
        }
        self.deleted = False

    def list_memories(self, **kwargs):
        return {
            "items": [self.item],
            "total": 1,
            "limit": kwargs.get("limit"),
            "offset": kwargs.get("offset"),
        }

    async def search_async(self, **kwargs):
        return [{**self.item, "score": 0.9, "matched_by": ["memory_keyword"]}]

    def get_memory(self, memory_id):
        return self.item if memory_id == "memory-1" and not self.deleted else None

    async def update_memory(self, memory_id, **kwargs):
        if memory_id != "memory-1" or self.deleted:
            return None
        self.item = {
            **self.item,
            **{key: value for key, value in kwargs.items() if value is not None},
        }
        return self.item

    async def delete_memory(self, memory_id):
        if memory_id != "memory-1" or self.deleted:
            return False
        self.deleted = True
        return True

    def get_topic_graph(self):
        return {"nodes": [{"id": "topic-1", "recall_weight": 0.8}], "edges": []}


class RuntimeMemoryRpcTests(unittest.TestCase):
    def test_memory_rpc_methods_return_public_memory_payloads(self):
        service = RuntimeService()
        memory = FakeRuntimeSemanticMemory()
        cast(Any, service.state).agent = SimpleNamespace(semantic_memory=memory)

        async def run():
            listed = await service.handle("memory.list", {"limit": 10})
            searched = await service.handle("memory.search", {"query": "喝茶"})
            fetched = await service.handle("memory.get", {"memory_id": "memory-1"})
            updated = await service.handle(
                "memory.update", {"memory_id": "memory-1", "importance": 0.9}
            )
            graph = await service.handle("memory.graph")
            deleted = await service.handle("memory.delete", {"memory_id": "memory-1"})
            missing = await service.handle(
                "memory.get", {"memory_id": "memory-1"}, structured_errors=True
            )
            return listed, searched, fetched, updated, graph, deleted, missing

        listed, searched, fetched, updated, graph, deleted, missing = asyncio.run(run())

        self.assertEqual(listed["total"], 1)
        self.assertEqual(searched["items"][0]["score"], 0.9)
        self.assertFalse(searched["diagnostics"]["embedding_used"])
        self.assertEqual(fetched["id"], "memory-1")
        self.assertTrue(updated["updated"])
        self.assertEqual(updated["memory"]["importance"], 0.9)
        self.assertEqual(graph["topic_count"], 1)
        self.assertTrue(deleted["deleted"])
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error_code"], "runtime.error")

    def test_memory_rpc_requires_initialized_agent(self):
        service = RuntimeService()

        response = asyncio.run(service.handle("memory.list", structured_errors=True))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "runtime.error")
        self.assertIn("Runtime is not initialized", response["error"])


class RuntimeSessionRpcTests(unittest.TestCase):
    def test_current_and_delete_session_return_json_compatible_payloads(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        cast(Any, service.state).agent = SimpleNamespace(session_manager=manager)
        assert manager.current is not None
        session_id = manager.current.session_id

        async def run():
            current = await service.handle("session.current")
            deleted = await service.handle("session.delete", {"session_id": session_id})
            missing = await service.handle(
                "session.delete", {"session_id": session_id}, structured_errors=True
            )
            return current, deleted, missing

        current, deleted, missing = asyncio.run(run())

        self.assertEqual(current["session_id"], session_id)
        self.assertTrue(deleted["deleted"])
        self.assertTrue(deleted["was_current"])
        self.assertIsNone(deleted["current_session"])
        self.assertEqual(deleted["remaining_count"], 0)
        self.assertEqual(deleted["remaining_sessions"], [])
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error_code"], "runtime.error")

    def test_export_session_returns_complete_machine_readable_payload(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        cast(Any, service.state).agent = SimpleNamespace(
            session_manager=manager,
            config=SimpleNamespace(character=SimpleNamespace(name="博丽灵梦")),
        )
        assert manager.current is not None
        session_id = manager.current.session_id

        async def run():
            exported = await service.handle("session.export")
            missing = await service.handle(
                "session.export", {"session_id": "missing"}, structured_errors=True
            )
            return exported, missing

        exported, missing = asyncio.run(run())

        self.assertEqual(exported["format"], "gensokyoai.session.export")
        self.assertEqual(exported["version"], 1)
        self.assertEqual(exported["schema_version"], 1)
        self.assertEqual(exported["session_schema_version"], SESSION_SCHEMA_VERSION)
        self.assertEqual(exported["memory_schema_version"], MEMORY_SCHEMA_VERSION)
        self.assertTrue(exported["is_current"])
        self.assertEqual(exported["character"]["name"], "博丽灵梦")
        self.assertEqual(exported["session"]["session_id"], session_id)
        self.assertEqual(exported["message_count"], 2)
        self.assertEqual(exported["messages"][0]["content"], "你好")
        self.assertIn("runtime", exported)
        self.assertTrue(manager.saved)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error_code"], "runtime.error")

    def test_rename_session_stores_title_in_metadata(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        cast(Any, service.state).agent = SimpleNamespace(session_manager=manager)
        assert manager.current is not None
        session_id = manager.current.session_id

        async def run():
            renamed = await service.handle("session.rename", {"title": " 新标题 "})
            invalid = await service.handle(
                "session.rename", {"title": "   "}, structured_errors=True
            )
            return renamed, invalid

        renamed, invalid = asyncio.run(run())

        self.assertEqual(renamed["session_id"], session_id)
        self.assertEqual(renamed["metadata"]["title"], "新标题")
        self.assertIn(session_id, manager.persistence.saved_sessions)
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error_code"], "runtime.error")

    def test_rollback_session_validates_mode_and_saves_current_memory(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        rollback_calls = []

        def rollback(num=1, mode="turns"):
            rollback_calls.append((num, mode))
            assert manager.current is not None
            manager.messages_by_session[manager.current.session_id] = []

        cast(Any, service.state).agent = SimpleNamespace(
            session_manager=manager,
            rollback=rollback,
        )

        async def run():
            rolled_back = await service.handle(
                "session.rollback",
                {"num": 2, "mode": "messages"},
            )
            invalid = await service.handle("session.rollback", {"num": 0}, structured_errors=True)
            return rolled_back, invalid

        rolled_back, invalid = asyncio.run(run())

        self.assertTrue(rolled_back["rolled_back"])
        self.assertEqual(rolled_back["num"], 2)
        self.assertEqual(rolled_back["mode"], "messages")
        self.assertEqual(rolled_back["before_total_turns"], 1)
        self.assertEqual(rolled_back["after_total_turns"], 0)
        self.assertEqual(rolled_back["before_message_count"], 2)
        self.assertEqual(rolled_back["after_message_count"], 0)
        self.assertEqual(rolled_back["message_count"], 0)
        self.assertEqual(rollback_calls, [(2, "messages")])
        self.assertTrue(manager.saved)
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error_code"], "runtime.error")

    def test_session_messages_and_replace_messages_support_frontend_editing(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        cast(Any, service.state).agent = SimpleNamespace(session_manager=manager)
        assert manager.current is not None
        session_id = manager.current.session_id
        edited_messages = [
            {"role": "system", "content": "背景设定"},
            {"role": "user", "content": "改写", "reasoning_content": "keep"},
            {"role": "assistant", "content": "新回复"},
        ]

        async def run():
            before = await service.handle("session.messages", {"session_id": session_id})
            replaced = await service.handle(
                "session.replace_messages",
                {"session_id": session_id, "messages": edited_messages},
            )
            invalid = await service.handle(
                "session.replace_messages",
                {"session_id": session_id, "messages": [{"role": "bad", "content": "x"}]},
                structured_errors=True,
            )
            return before, replaced, invalid

        before, replaced, invalid = asyncio.run(run())

        self.assertEqual(before["message_count"], 2)
        self.assertTrue(replaced["replaced"])
        self.assertEqual(replaced["message_count"], 3)
        self.assertEqual(replaced["messages"][1]["reasoning_content"], "keep")
        self.assertEqual(manager.messages_by_session[session_id], edited_messages)
        self.assertEqual(manager.current.total_turns, 1)
        self.assertIn(session_id, manager.persistence.saved_sessions)
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error_code"], "runtime.error")

    def test_session_regenerate_from_truncates_to_user_message_and_generates_reply(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        assert manager.current is not None
        session_id = manager.current.session_id
        send_calls = []
        resume_calls = []

        def resume_session(target_session_id):
            resume_calls.append(target_session_id)
            return manager.set_current_session(target_session_id)

        async def start():
            return None

        async def send(message, system_contexts=None):
            send_calls.append((message, system_contexts))
            manager.messages_by_session[session_id].append({"role": "user", "content": message})
            manager.messages_by_session[session_id].append(
                {"role": "assistant", "content": "重新生成"}
            )
            return SimpleNamespace(content="重新生成")

        cast(Any, service.state).agent = SimpleNamespace(
            session_manager=manager,
            resume_session=resume_session,
            start=start,
            send=send,
        )

        async def run():
            return await service.handle(
                "session.regenerate_from",
                {"session_id": session_id, "message_index": 1, "system_contexts": ["ctx"]},
            )

        result = asyncio.run(run())

        self.assertTrue(result["regenerated"])
        self.assertEqual(result["user_message_index"], 0)
        self.assertEqual(result["content"], "重新生成")
        self.assertEqual(send_calls, [("你好", ["ctx"])])
        self.assertEqual(manager.messages_by_session[session_id][-1]["content"], "重新生成")
        self.assertIn(session_id, resume_calls)


class RuntimeStreamingRpcTests(unittest.TestCase):
    def test_send_message_exposes_reasoning_by_default(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()

        async def send(message, system_contexts=None):
            return SimpleNamespace(content="回答", reasoning_content="思考")

        cast(Any, service.state).agent = SimpleNamespace(
            send=send,
            session_manager=manager,
        )
        service.state.started = True

        result = asyncio.run(service.handle("agent.send_message", {"message": "问题"}))

        self.assertEqual(result["content"], "回答")
        self.assertEqual(result["reasoning_content"], "思考")

    def test_send_message_stream_returns_stable_event_list_and_finish_event(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        start_calls = []

        async def start():
            start_calls.append(True)

        async def send_stream(message, system_contexts=None):
            yield StreamChunk(reasoning_content="先思考")
            yield StreamChunk(content="你")
            yield StreamChunk(content="好", status="streaming")

        cast(Any, service.state).agent = SimpleNamespace(
            start=start,
            send_stream=send_stream,
            session_manager=manager,
        )
        service.state.started = False

        async def run():
            return await service.handle(
                "agent.send_message_stream",
                {"message": "hi", "system_contexts": ["ctx"]},
            )

        result = asyncio.run(run())

        self.assertEqual(start_calls, [True])
        self.assertEqual(result["role"], "assistant")
        self.assertEqual(result["content"], "你好")
        self.assertEqual(result["reasoning_content"], "先思考")
        self.assertTrue(result["generation_id"])
        self.assertEqual(result["events"][0]["type"], "reasoning")
        self.assertEqual(result["events"][0]["reasoning_content"], "先思考")
        self.assertEqual(result["events"][1]["type"], "content")
        self.assertEqual(result["events"][1]["content"], "你")
        self.assertEqual(result["events"][2]["type"], "content")
        self.assertEqual(result["events"][2]["status"], "streaming")
        self.assertEqual(result["events"][-1]["type"], "finish")
        self.assertEqual(result["events"][-1]["content"], "你好")
        assert manager.current is not None
        self.assertEqual(result["session"]["session_id"], manager.current.session_id)


class RuntimeResourceControlTests(unittest.TestCase):
    def test_resource_control_config_validation_reports_invalid_limits(self):
        diagnostics = ConfigLoader().validate_dict(
            {
                "resource_control": {
                    "runtime_max_concurrent": 0,
                    "runtime_queue_size": -1,
                    "overflow_policy": "wait",
                    "acquire_timeout_seconds": 0,
                }
            }
        )

        codes = {item.code for item in diagnostics}
        paths = {item.path for item in diagnostics}
        self.assertIn("config.field.range", codes)
        self.assertIn("config.resource_control.wait_without_timeout", codes)
        self.assertIn("resource_control.runtime_max_concurrent", paths)
        self.assertIn("resource_control.runtime_queue_size", paths)

    def test_config_validation_reports_p1_6_schema_and_resource_warnings(self):
        diagnostics = ConfigLoader().validate_dict(
            {
                "config_schema_version": 0,
                "model": {
                    "provider": "deepseek",
                    "thinking_enabled": False,
                    "reasoning_effort": "high",
                },
                "resource_control": {
                    "runtime_max_concurrent": 1,
                    "runtime_queue_size": 3,
                    "session_max_concurrent": 2,
                    "default_timeout_seconds": 120,
                    "dependency_install_timeout_seconds": 30,
                    "overflow_policy": "reject",
                },
            }
        )

        codes = {item.code for item in diagnostics}
        paths = {item.path for item in diagnostics}
        self.assertIn("config.schema_version.outdated", codes)
        self.assertIn("config.model.reasoning_effort_ignored", codes)
        self.assertIn("config.resource_control.queue_unused", codes)
        self.assertIn("config.resource_control.limit_shadowed", codes)
        self.assertIn("config.resource_control.dependency_timeout_short", codes)
        self.assertIn("config_schema_version", paths)
        self.assertIn("resource_control.session_max_concurrent", paths)

    def test_config_validation_rejects_future_schema_version_and_ollama_api_path(self):
        diagnostics = ConfigLoader().validate_dict(
            {
                "config_schema_version": 999,
                "model": {
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11434",
                    "api_path": "/v1/chat/completions",
                },
            }
        )

        errors = {item.code for item in diagnostics if item.severity == "error"}
        self.assertIn("config.schema_version.unsupported", errors)
        self.assertIn("config.provider.api_path_unsupported", errors)

    def test_config_validation_tightens_provider_field_matrix_errors(self):
        diagnostics = ConfigLoader().validate_dict(
            {
                "model": {
                    "provider": "deepseek",
                    "api_path": "/custom/chat/completions",
                    "web_search_enabled": True,
                    "web_search_strategy": "explicit",
                }
            }
        )

        payloads = [item.to_dict() for item in diagnostics]
        errors = {item["code"] for item in payloads if item["severity"] == "error"}
        warnings = {item["code"] for item in payloads if item["severity"] == "warning"}
        paths = {item["path"] for item in payloads}

        self.assertIn("config.provider.field_unsupported", errors)
        self.assertIn("config.provider.web_search_unsupported", errors)
        self.assertIn("model.api_path", paths)
        self.assertIn("model.web_search_enabled", paths)
        self.assertNotIn("config.provider.field_discouraged", warnings)

    def test_config_validation_keeps_soft_provider_matrix_warnings(self):
        diagnostics = ConfigLoader().validate_dict(
            {
                "model": {
                    "provider": "ollama",
                    "api_key": "sk-not-needed",
                    "reasoning_effort": "high",
                }
            }
        )

        warnings = [
            item for item in diagnostics if item.code == "config.provider.field_discouraged"
        ]
        errors = [item for item in diagnostics if item.severity == "error"]

        self.assertEqual(
            {item.path for item in warnings}, {"model.api_key", "model.reasoning_effort"}
        )
        self.assertEqual(errors, [])

    def test_runtime_info_exposes_resource_control_capability_and_snapshot(self):
        service = RuntimeService()

        async def run():
            return await service.handle("runtime.info")

        info = asyncio.run(run())

        self.assertIn("resource_control.runtime_gates", info["capabilities"])
        self.assertTrue(info["resource_control"]["enabled"])
        self.assertIn("runtime", info["resource_control"]["gates"])
        self.assertIn("provider", info["resource_control"]["gates"])
        self.assertIn("model", info["resource_control"]["gates"])
        self.assertIn("tool", info["resource_control"]["gates"])
        self.assertIn("web_search", info["resource_control"]["gates"])
        self.assertIn("image_generation", info["resource_control"]["gates"])
        self.assertIn("dependency_install", info["resource_control"]["categories"])

    def test_send_message_returns_structured_resource_limit_when_gate_is_full(self):
        service = RuntimeService()
        started = asyncio.Event()
        release = asyncio.Event()
        manager = FakeRuntimeSessionManager()

        async def send(message, system_contexts=None):
            started.set()
            await release.wait()
            return SimpleNamespace(content="ok")

        resource_control = ResourceControlConfig(
            runtime_max_concurrent=1,
            runtime_queue_size=0,
            session_max_concurrent=1,
            acquire_timeout_seconds=0,
        )
        cast(Any, service.state).agent = SimpleNamespace(
            config=SimpleNamespace(resource_control=resource_control),
            send=send,
            session_manager=manager,
        )
        service.state.started = True
        cast(Any, service)._resource_gates = service._build_resource_gates(resource_control)

        async def run():
            first = asyncio.create_task(service.handle("agent.send_message", {"message": "one"}))
            await started.wait()
            second = await service.handle(
                "agent.send_message", {"message": "two"}, structured_errors=True
            )
            release.set()
            first_result = await first
            return first_result, second

        first_result, second = asyncio.run(run())

        self.assertEqual(first_result["content"], "ok")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "resource.limit_exceeded")
        self.assertEqual(second["error_object"]["details"]["resource"], "runtime")
        self.assertEqual(service._resource_gates["runtime"].active, 0)

    def test_send_message_wait_policy_times_out_when_session_gate_is_full(self):
        service = RuntimeService()
        started = asyncio.Event()
        release = asyncio.Event()
        manager = FakeRuntimeSessionManager()

        async def send(message, system_contexts=None):
            started.set()
            await release.wait()
            return SimpleNamespace(content="ok")

        resource_control = ResourceControlConfig(
            runtime_max_concurrent=2,
            runtime_queue_size=1,
            session_max_concurrent=1,
            acquire_timeout_seconds=0.01,
            overflow_policy="wait",
        )
        cast(Any, service.state).agent = SimpleNamespace(
            config=SimpleNamespace(resource_control=resource_control),
            send=send,
            session_manager=manager,
        )
        service.state.started = True
        cast(Any, service)._resource_gates = service._build_resource_gates(resource_control)

        async def run():
            first = asyncio.create_task(service.handle("agent.send_message", {"message": "one"}))
            await started.wait()
            second = await service.handle(
                "agent.send_message", {"message": "two"}, structured_errors=True
            )
            release.set()
            first_result = await first
            return first_result, second

        first_result, second = asyncio.run(run())

        self.assertEqual(first_result["content"], "ok")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "resource.limit_exceeded")
        self.assertEqual(second["error_object"]["details"]["resource"], "agent_message")
        self.assertEqual(second["error_object"]["details"]["reason"], "acquire_timeout")
        self.assertEqual(service._resource_gates["agent_message"].active, 0)

    def test_stream_cancellation_releases_resource_gates(self):
        service = RuntimeService()
        manager = FakeRuntimeSessionManager()
        resource_control = ResourceControlConfig(
            runtime_max_concurrent=1,
            runtime_queue_size=1,
            session_max_concurrent=1,
            stream_max_concurrent=1,
            acquire_timeout_seconds=0.05,
        )

        async def send_stream(message, system_contexts=None):
            yield StreamChunk(content="首")
            await asyncio.sleep(60)

        cast(Any, service.state).agent = SimpleNamespace(
            config=SimpleNamespace(resource_control=resource_control),
            send_stream=send_stream,
            session_manager=manager,
        )
        service.state.started = True
        cast(Any, service)._resource_gates = service._build_resource_gates(resource_control)

        async def run():
            iterator = service.iter_message_stream("hi")
            first = await iterator.__anext__()
            await cast(Any, iterator).aclose()
            return first

        first = asyncio.run(run())

        self.assertEqual(first["content"], "首")
        self.assertEqual(service._resource_gates["runtime"].active, 0)
        self.assertEqual(service._resource_gates["agent_message"].active, 0)
        self.assertEqual(service._resource_gates["stream"].active, 0)

    def test_dependency_install_uses_configured_timeout_and_releases_gate(self):
        service = RuntimeService()
        resource_control = ResourceControlConfig(
            dependency_install_max_concurrent=1,
            dependency_install_timeout_seconds=7,
        )
        cast(Any, service.state).agent = SimpleNamespace(
            config=SimpleNamespace(resource_control=resource_control)
        )
        cast(Any, service)._resource_gates = service._build_resource_gates(resource_control)

        def fake_install(providers, scope="current_runtime", timeout=600):
            return {"providers": providers, "scope": scope, "timeout": timeout}

        async def run():
            with patch("GensokyoAI.runtime.service.install_dependencies", side_effect=fake_install):
                return await service.handle(
                    "dependency.install",
                    {"providers": ["openai"]},
                )

        result = asyncio.run(run())

        self.assertEqual(result["timeout"], 7)
        self.assertEqual(service._resource_gates["runtime"].active, 0)
        self.assertEqual(service._resource_gates["dependency_install"].active, 0)


class RuntimeEventSubscriptionTests(unittest.TestCase):
    def test_runtime_event_payload_redacts_sensitive_fields_recursively(self):
        event = Event(
            type=SystemEvent.MODEL_AUTH,
            source="test",
            data={
                "api_key": "sk-secret",
                "Authorization": "Bearer token",
                "nested": {
                    "refresh_token": "refresh-secret",
                    "safe": "visible",
                },
                "items": [{"password": "pw", "value": 1}],
            },
            metadata={"headers": {"X-Test": "1"}, "safe_meta": "ok"},
        )

        payload = RuntimeService._runtime_event_payload(event)

        self.assertEqual(payload["data"]["api_key"], "[redacted]")
        self.assertEqual(payload["data"]["Authorization"], "[redacted]")
        self.assertEqual(payload["data"]["nested"]["refresh_token"], "[redacted]")
        self.assertEqual(payload["data"]["nested"]["safe"], "visible")
        self.assertEqual(payload["data"]["items"][0]["password"], "[redacted]")
        self.assertEqual(payload["data"]["items"][0]["value"], 1)
        self.assertEqual(payload["metadata"]["headers"], "[redacted]")
        self.assertEqual(payload["metadata"]["safe_meta"], "ok")

    def test_event_subscription_reports_backpressure_when_queue_is_full(self):
        service = RuntimeService()
        event_bus = EventBus(enable_trace=False)
        agent = SimpleNamespace(event_bus=event_bus)
        cast(Any, service.state).agent = agent

        async def run():
            subscription = await service.create_event_subscription(
                event_types=["tool.call.started"],
                queue_size=1,
            )
            queue = subscription["queue"]
            await event_bus._process_event(
                Event(
                    type=SystemEvent.TOOL_CALL_STARTED,
                    source="test",
                    data={"name": "first"},
                )
            )
            await event_bus._process_event(
                Event(
                    type=SystemEvent.TOOL_CALL_STARTED,
                    source="test",
                    data={"name": "second", "api_key": "secret"},
                )
            )
            payload = queue.get_nowait()
            queue.task_done()
            await service.close_event_subscription(subscription["subscription_id"])
            return payload

        payload = asyncio.run(run())

        self.assertEqual(payload["type"], "runtime.backpressure.dropped")
        self.assertEqual(payload["data"]["dropped_count"], 1)
        self.assertEqual(payload["data"]["queue_size"], 1)
        self.assertEqual(payload["data"]["dropped_event_type"], "tool.call.started")
        self.assertNotIn("api_key", payload["data"])
        self.assertEqual(event_bus.stats["subscriber_count"], 0)


class ConfigValidationAndRuntimePathTests(unittest.TestCase):
    def test_config_loader_rejects_unknown_fields_and_invalid_ranges(self):
        with self.assertRaisesRegex(ValueError, "model.unknown"):
            ConfigLoader()._dict_to_config({"model": {"unknown": True}})
        with self.assertRaisesRegex(ValueError, "model.temperature"):
            ConfigLoader()._dict_to_config({"model": {"temperature": 9}})
        with self.assertRaisesRegex(ValueError, "tool.web_search.max_results"):
            ConfigLoader()._dict_to_config({"tool": {"web_search": {"max_results": 0}}})

    def test_config_loader_accepts_legacy_initiative_timer_edit_message_alias(self):
        loader = ConfigLoader()

        diagnostics = loader.validate_dict(
            {"initiative_timer": {"allow_frontend_edit_message": False}}
        )
        config = loader._dict_to_config(
            {"initiative_timer": {"allow_frontend_edit_message": False}}
        )

        self.assertFalse(config.initiative_timer.allow_frontend_edit_summary)
        self.assertFalse(any(item.severity == "error" for item in diagnostics))

    def test_config_loader_prefers_current_initiative_timer_edit_summary_field(self):
        config = ConfigLoader()._dict_to_config(
            {
                "initiative_timer": {
                    "allow_frontend_edit_message": False,
                    "allow_frontend_edit_summary": True,
                }
            }
        )

        self.assertTrue(config.initiative_timer.allow_frontend_edit_summary)

    def test_config_loader_accepts_hesitation_enabled_field(self):
        config = ConfigLoader()._dict_to_config({"initiative_timer": {"hesitation_enabled": True}})
        diagnostics = ConfigLoader().validate_dict(
            {"initiative_timer": {"hesitation_enabled": True}}
        )

        self.assertTrue(config.initiative_timer.hesitation_enabled)
        self.assertFalse(any(item.severity == "error" for item in diagnostics))

    def test_runtime_exposes_initiative_hesitation_rpc_methods(self):
        self.assertIn("initiative_timer.hesitation", rpc_methods())
        self.assertIn("initiative_timer.hesitation.set", rpc_methods())

    def test_runtime_hesitation_rpc_reads_and_sets_agent_state(self):
        class FakeAgent:
            def __init__(self):
                self.enabled = False

            def initiative_hesitation_status(self):
                return {"enabled": self.enabled, "config_path": "config/default.yaml"}

            def set_initiative_hesitation_enabled(self, enabled, *, persist=True):
                self.enabled = bool(enabled)
                return {
                    "enabled": self.enabled,
                    "persist": persist,
                    "config_path": "config/default.yaml",
                }

        service = RuntimeService()
        service.state.agent = cast(Any, FakeAgent())

        status = asyncio.run(service.handle("initiative_timer.hesitation"))
        self.assertFalse(status["enabled"])

        updated = asyncio.run(service.handle("initiative_timer.hesitation.set", {"enabled": True}))
        self.assertTrue(updated["enabled"])
        self.assertTrue(updated["persist"])

    def test_config_loader_returns_structured_diagnostics(self):
        diagnostics = ConfigLoader().validate_dict(
            {
                "model": {
                    "temperature": 3,
                    "web_search_enabled": True,
                    "web_search_strategy": "off",
                },
                "think_engine": {
                    "random_walk_steps_min": 5,
                    "random_walk_steps_max": 2,
                },
            }
        )

        payloads = [item.to_dict() for item in diagnostics]
        paths = {item["path"] for item in payloads}
        codes = {item["code"] for item in payloads}

        self.assertIn("model.temperature", paths)
        self.assertIn("model.web_search_strategy", paths)
        self.assertIn("think_engine.random_walk_steps_max", paths)
        self.assertIn("config.model.web_search_conflict", codes)
        self.assertTrue(all("severity" in item for item in payloads))

    def test_config_loader_warns_for_provider_missing_api_key(self):
        diagnostics = ConfigLoader().validate_dict({"model": {"provider": "openai"}})

        self.assertTrue(
            any(
                item.code == "config.model.api_key_missing"
                and item.severity == "warning"
                and item.path == "model.api_key"
                for item in diagnostics
            )
        )
        ConfigLoader()._dict_to_config({"model": {"provider": "openai"}})

    def test_config_loader_reports_provider_field_matrix(self):
        diagnostics = ConfigLoader().validate_dict(
            {
                "model": {
                    "provider": "ollama",
                    "api_key": "sk-not-needed",
                    "web_search_enabled": True,
                    "web_search_strategy": "explicit",
                }
            }
        )

        warnings = {item.code for item in diagnostics if item.severity == "warning"}
        errors = {item.code for item in diagnostics if item.severity == "error"}
        self.assertIn("config.provider.field_discouraged", warnings)
        self.assertIn("config.provider.field_unsupported", errors)
        self.assertIn("config.provider.web_search_unsupported", errors)

    def test_runtime_resolve_optional_rejects_paths_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = RuntimeService(root_dir=root)
            inside = service._resolve_optional("config/default.yaml")
            self.assertIsNotNone(inside)
            assert inside is not None
            self.assertTrue(inside.is_relative_to(root.resolve()))
            with self.assertRaisesRegex(ValueError, "outside Runtime root"):
                service._resolve_optional(str(root.parent / "secret.yaml"))

    def test_config_loader_load_reports_friendly_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(yaml.safe_dump({"model": {"timeout": 0}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "model.timeout"):
                ConfigLoader().load(path)


class EventBusP0Tests(unittest.TestCase):
    def test_event_bus_backpressure_stats_and_critical_flush(self):
        delivered = []

        async def handler(event):
            delivered.append(event.type)

        async def run():
            bus = EventBus(enable_trace=False, max_queue_size=1)
            bus.subscribe(SystemEvent.PERSISTENCE_SAVE_COMPLETED, handler)
            bus.publish(Event(type=SystemEvent.PERSISTENCE_SAVE_COMPLETED, source="test"))
            bus.publish(Event(type=SystemEvent.MODEL_COMPLETED, source="test"))
            await bus.flush_critical(timeout=0.1)
            return bus.stats

        stats = asyncio.run(run())

        self.assertEqual(delivered, [SystemEvent.PERSISTENCE_SAVE_COMPLETED])
        self.assertEqual(stats["dropped"], 1)
        self.assertEqual(stats["critical_flushed"], 1)
        self.assertEqual(stats["queue_max_size"], 1)


class RuntimeConfigValidationApiTests(unittest.TestCase):
    def test_config_validate_rpc_returns_structured_diagnostics(self):
        service = RuntimeService()

        async def run():
            return await service.handle(
                "config.validate",
                {
                    "config": {
                        "model": {
                            "provider": "ollama",
                            "api_key": "sk-not-needed",
                            "web_search_enabled": True,
                            "temperature": 3,
                        }
                    },
                    "model_overrides": {"retry_status_codes": [99]},
                    "embedding_overrides": {"dimensions": 0},
                },
            )

        result = asyncio.run(run())
        paths = {item["path"] for item in result["diagnostics"]}
        codes = {item["code"] for item in result["diagnostics"]}
        error_codes = {
            item["code"] for item in result["diagnostics"] if item["severity"] == "error"
        }
        warning_codes = {
            item["code"] for item in result["diagnostics"] if item["severity"] == "warning"
        }

        self.assertFalse(result["ok"])
        self.assertEqual(result["source"], "inline")
        self.assertIn("model.temperature", paths)
        self.assertIn("model.retry_status_codes", paths)
        self.assertIn("embedding.dimensions", paths)
        self.assertIn("model.web_search_enabled", paths)
        self.assertIn("config.provider.field_discouraged", codes)
        self.assertIn("config.provider.field_unsupported", error_codes)
        self.assertIn("config.provider.web_search_unsupported", error_codes)
        self.assertIn("config.provider.field_discouraged", warning_codes)
        self.assertGreaterEqual(result["error_count"], 5)

    def test_config_validate_method_is_advertised(self):
        service = RuntimeService()

        async def run():
            info = await service.handle("runtime.info")
            return info

        info = asyncio.run(run())

        self.assertIn("config.validate", info["methods"])
        self.assertIn("config.validation", info["capabilities"])


class CharacterValidationTests(unittest.TestCase):
    def test_character_validator_reports_structured_errors_and_warnings(self):
        diagnostics = CharacterValidator().validate_character_dict(
            {
                "name": " ",
                "system_prompt": "x" * 12001,
                "unknown": True,
                "example_dialogue": [
                    {"user": "hi", "assistant": "hello", "extra": "no"},
                    {"user": "", "assistant": 1},
                    "bad item",
                ],
                "metadata": [],
            }
        )

        payloads = [item.to_dict() for item in diagnostics]
        paths = {item["path"] for item in payloads}
        codes = {item["code"] for item in payloads}

        self.assertIn("name", paths)
        self.assertIn("unknown", paths)
        self.assertIn("example_dialogue.0.extra", paths)
        self.assertIn("example_dialogue.1.user", paths)
        self.assertIn("example_dialogue.1.assistant", paths)
        self.assertIn("example_dialogue.2", paths)
        self.assertIn("metadata", paths)
        self.assertIn("character.prompt.length_warning", codes)
        self.assertTrue(any(item["severity"] == "warning" for item in payloads))

    def test_config_loader_load_character_rejects_invalid_character_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(
                yaml.safe_dump({"name": "Bad", "example_dialogue": [{"user": "hi"}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "system_prompt"):
                ConfigLoader().load_character(path)

    def test_character_validate_rpc_returns_preview_and_diagnostics(self):
        service = RuntimeService()

        async def run():
            return await service.handle(
                "character.validate",
                {
                    "character_data": {
                        "name": "测试角色",
                        "system_prompt": "你是测试角色。",
                        "greeting": "你好",
                        "example_dialogue": [{"user": "hi", "assistant": "hello"}],
                        "metadata": {"species": "test"},
                    }
                },
            )

        result = asyncio.run(run())

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "inline")
        self.assertEqual(result["preview"]["name"], "测试角色")
        self.assertEqual(result["preview"]["system_prompt_length"], 7)
        self.assertEqual(result["preview"]["example_count"], 1)
        self.assertEqual(result["preview"]["metadata"], {"species": "test"})

    def test_character_list_returns_structured_diagnostics_for_invalid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            characters_dir = root / "characters"
            characters_dir.mkdir()
            (characters_dir / "good.yaml").write_text(
                yaml.safe_dump({"name": "Good", "system_prompt": "ok"}),
                encoding="utf-8",
            )
            (characters_dir / "bad.yaml").write_text(
                yaml.safe_dump({"name": "Bad", "metadata": []}),
                encoding="utf-8",
            )
            service = RuntimeService(root_dir=root)

            async def run():
                return await service.handle("character.list")

            result = asyncio.run(run())

            by_id = {item["id"]: item for item in result}
            self.assertTrue(by_id["good"]["ok"])
            self.assertFalse(by_id["bad"]["ok"])
            self.assertIn("diagnostics", by_id["bad"])
            self.assertIn("preview", by_id["good"])

    def test_builtin_character_files_have_no_validation_errors(self):
        validator = CharacterValidator()
        character_paths = [
            *Path("characters").glob("*.yaml"),
            *Path("characters").glob("*.yml"),
            *Path("characters/zh_cn").glob("*.yaml"),
            *Path("characters/zh_cn").glob("*.yml"),
        ]

        errors_by_file = {}
        for path in character_paths:
            errors = [
                item for item in validator.validate_character_file(path) if item.severity == "error"
            ]
            if errors:
                errors_by_file[str(path)] = [item.to_dict() for item in errors]

        self.assertEqual(errors_by_file, {})


class RuntimeOverrideTests(unittest.TestCase):
    def test_model_overrides_reject_invalid_values(self):
        config = ModelConfig(provider="openai", name="old")

        with self.assertRaisesRegex(ValueError, "model.temperature"):
            RuntimeService._apply_model_overrides(config, {"temperature": 9})
        with self.assertRaisesRegex(ValueError, "model.retry_status_codes"):
            RuntimeService._apply_model_overrides(config, {"retry_status_codes": [99]})

    def test_embedding_overrides_reject_invalid_values(self):
        config = EmbeddingConfig(provider="openai", name="embed")

        with self.assertRaisesRegex(ValueError, "embedding.dimensions"):
            RuntimeService._apply_embedding_overrides(config, {"dimensions": 0})

    def test_model_overrides_allow_runtime_api_related_fields(self):
        config = ModelConfig(provider="openai", name="old")

        RuntimeService._apply_model_overrides(
            config,
            {
                "api_path": "/custom/chat/completions",
                "extra_headers": {"X-Test": "1"},
                "web_search_enabled": True,
                "web_search_strategy": "explicit",
                "retry_max_attempts": 5,
                "retry_initial_delay": 0.5,
                "retry_backoff_factor": 1.5,
                "retry_status_codes": [500, 502, 429],
                "not_allowed": "ignored",
            },
        )

        self.assertEqual(config.api_path, "/custom/chat/completions")
        self.assertEqual(config.extra_headers, {"X-Test": "1"})
        self.assertTrue(config.web_search_enabled)
        self.assertEqual(config.web_search_strategy, "explicit")
        self.assertEqual(config.retry_max_attempts, 5)
        self.assertEqual(config.retry_initial_delay, 0.5)
        self.assertEqual(config.retry_backoff_factor, 1.5)
        self.assertEqual(config.retry_status_codes, [500, 502, 429])
        self.assertFalse(hasattr(config, "not_allowed"))


if __name__ == "__main__":
    unittest.main()
