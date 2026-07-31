"""角色 YAML 诊断与校验工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config_schema import BeginScene, CharacterConfig, MotivationWeightsConfig
from .config_validator import ConfigDiagnostic, ConfigValidationError


class CharacterValidator:
    """角色 YAML 字典校验器。"""

    ALLOWED_TOP_LEVEL_FIELDS = {
        "name",
        "system_prompt",
        "greeting",
        "begin_scene",
        "example_dialogue",
        "metadata",
        "motivation_weights",
        "emotion_baseline",
    }
    REQUIRED_FIELDS = {"name", "system_prompt"}
    SYSTEM_PROMPT_WARNING_LENGTH = 12000
    GREETING_WARNING_LENGTH = 500
    EXAMPLE_DIALOGUE_WARNING_LENGTH = 1200
    EXAMPLE_DIALOGUE_TOTAL_WARNING_LENGTH = 6000

    def validate_character_file(self, path: Path) -> list[ConfigDiagnostic]:
        """读取并校验角色 YAML 文件，返回结构化诊断列表。"""

        try:
            with open(path, encoding="utf-8") as file:
                data = yaml.safe_load(file) or {}
        except yaml.YAMLError as error:
            return [
                self._error(
                    "$",
                    f"Character YAML is invalid: {error}",
                    "请检查 YAML 缩进、冒号和列表格式。",
                    code="character.yaml.invalid",
                )
            ]
        return self.validate_character_dict(data)

    def validate_character_dict(self, data: Any) -> list[ConfigDiagnostic]:
        """校验角色 YAML 解析后的字典。"""

        diagnostics: list[ConfigDiagnostic] = []
        if not isinstance(data, dict):
            diagnostics.append(
                self._error(
                    "$",
                    "Character root must be an object",
                    "请确认角色 YAML 顶层是 key/value 对象。",
                    code="character.type.invalid",
                )
            )
            return diagnostics

        self._validate_unknown_fields(data, diagnostics)
        self._validate_required_fields(data, diagnostics)
        self._validate_string_field("name", data.get("name"), diagnostics, required=True)
        self._validate_string_field(
            "system_prompt",
            data.get("system_prompt"),
            diagnostics,
            required=True,
            warning_length=self.SYSTEM_PROMPT_WARNING_LENGTH,
            warning_code="character.prompt.length_warning",
            warning_suggestion="请确认人设 prompt 是否过长；过长会增加上下文成本并可能挤占对话记忆。",
        )
        self._validate_string_field(
            "greeting",
            data.get("greeting"),
            diagnostics,
            warning_length=self.GREETING_WARNING_LENGTH,
            warning_code="character.greeting.length_warning",
            warning_suggestion="建议保持开场白简洁，避免首次回复占用过多上下文。",
        )
        self._validate_begin_scene(data.get("begin_scene"), diagnostics)
        self._validate_example_dialogue(data.get("example_dialogue"), diagnostics)
        self._validate_metadata(data.get("metadata"), diagnostics)
        self._validate_motivation_weights(data.get("motivation_weights"), diagnostics)
        self._validate_emotion_baseline(data.get("emotion_baseline"), diagnostics)
        return diagnostics

    def build_preview(
        self,
        data: Any,
        *,
        fallback_id: str | None = None,
    ) -> dict[str, Any] | None:
        """从角色字典构建安全预览结构。"""

        if not isinstance(data, dict):
            return None
        system_prompt = data.get("system_prompt")
        greeting = data.get("greeting")
        begin_scene = self._normalize_begin_scene(data.get("begin_scene"))
        example_dialogue = data.get("example_dialogue")
        metadata = data.get("metadata")
        return {
            "id": fallback_id,
            "name": data.get("name") if isinstance(data.get("name"), str) else fallback_id,
            "system_prompt_length": len(system_prompt) if isinstance(system_prompt, str) else 0,
            "greeting_length": len(greeting) if isinstance(greeting, str) else 0,
            "has_begin_scene": begin_scene is not None,
            "begin_scene_id": begin_scene.scene if begin_scene else None,
            "example_count": len(example_dialogue) if isinstance(example_dialogue, list) else 0,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    @staticmethod
    def raise_for_errors(diagnostics: list[ConfigDiagnostic]) -> None:
        """若存在 error 诊断则抛出兼容配置校验异常。"""

        errors = [item for item in diagnostics if item.severity == "error"]
        if errors:
            raise ConfigValidationError(errors)

    def to_character_config(self, data: dict[str, Any]) -> CharacterConfig:
        """在调用方完成校验后构造角色配置对象。"""

        return CharacterConfig(
            name=data["name"],
            system_prompt=data["system_prompt"],
            greeting=data.get("greeting", ""),
            begin_scene=self._normalize_begin_scene(data.get("begin_scene")),
            example_dialogue=data.get("example_dialogue"),
            metadata=data.get("metadata", {}),
            motivation_weights=self._normalize_motivation_weights(
                data.get("motivation_weights")
            ),
            emotion_baseline=self._normalize_emotion_baseline(data.get("emotion_baseline")),
        )

    _MOTIVATION_WEIGHT_FIELDS = frozenset(
        {
            "expression_drive",
            "emotional_charge",
            "relational_need",
            "situational_relevance",
        }
    )

    def _validate_motivation_weights(self, value: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        """校验 motivation_weights：四个维度权重，各 0~1，缺省维度用默认值。"""
        self._validate_weight_dict(
            value,
            self._MOTIVATION_WEIGHT_FIELDS,
            "motivation_weights",
            "四维权重仅支持 expression_drive / emotional_charge / relational_need / situational_relevance。",
            "权重取值范围 0~1；总和保持 1 时对话欲量纲不变。",
            diagnostics,
        )

    def _normalize_motivation_weights(self, value: Any) -> MotivationWeightsConfig:
        """构造权重配置：缺省维度回落默认（通用人格基线）。"""
        return MotivationWeightsConfig(
            **self._normalize_weight_dict(value, self._MOTIVATION_WEIGHT_FIELDS)
        )

    _EMOTION_BASELINE_FIELDS = frozenset(
        {
            "anger",
            "sorrow",
            "fear",
            "happy",
            "love",
            "surprised",
            "disgust",
            "shame",
        }
    )

    def _validate_emotion_baseline(self, value: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        """校验 emotion_baseline：八维情绪基线，各 0~1，缺省全 0（平稳）。"""
        self._validate_weight_dict(
            value,
            self._EMOTION_BASELINE_FIELDS,
            "emotion_baseline",
            "情绪基线仅支持 anger/sorrow/fear/happy/love/surprised/disgust/shame 八维。",
            "情绪基线取值范围 0~1（0 为平静）。",
            diagnostics,
        )

    def _normalize_emotion_baseline(self, value: Any) -> dict[str, float]:
        """归一化情绪基线：仅保留合法的八维数值。"""
        return self._normalize_weight_dict(value, self._EMOTION_BASELINE_FIELDS)

    def _validate_weight_dict(
        self,
        value: Any,
        fields: frozenset[str],
        section: str,
        fields_hint: str,
        range_hint: str,
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        """校验「维度名 → 0~1 数值」的权重/基线字典（四维/八维共用）。"""
        if value is None:
            return
        if not isinstance(value, dict):
            diagnostics.append(
                self._error(
                    section,
                    f"Character field '{section}' must be an object",
                    "请写成 key/value 对象（如 {happy: 0.4, ...}）。",
                    code=f"character.{section}.type",
                )
            )
            return
        for field_name in sorted(set(value) - fields):
            diagnostics.append(
                self._error(
                    f"{section}.{field_name}",
                    f"Unknown {section} field '{field_name}'",
                    fields_hint,
                    code=f"character.{section}.field_unknown",
                )
            )
        for field_name in sorted(fields & set(value)):
            field_value = value[field_name]
            if isinstance(field_value, bool) or not isinstance(field_value, int | float):
                diagnostics.append(
                    self._error(
                        f"{section}.{field_name}",
                        f"{section} field '{field_name}' must be a number",
                        "请填写 0~1 之间的数值。",
                        code=f"character.{section}.field_type",
                    )
                )
            elif not 0.0 <= field_value <= 1.0:
                diagnostics.append(
                    self._error(
                        f"{section}.{field_name}",
                        f"{section} field '{field_name}' must be in [0, 1]",
                        range_hint,
                        code=f"character.{section}.field_range",
                    )
                )

    @staticmethod
    def _normalize_weight_dict(value: Any, fields: frozenset[str]) -> dict[str, float]:
        """归一化维度字典：仅保留合法的维度数值。"""
        if not isinstance(value, dict):
            return {}
        return {
            name: float(value[name])
            for name in fields & set(value)
            if isinstance(value[name], int | float) and not isinstance(value[name], bool)
        }

    def _validate_unknown_fields(
        self,
        data: dict[str, Any],
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        for field_name in sorted(set(data) - self.ALLOWED_TOP_LEVEL_FIELDS):
            diagnostics.append(
                self._error(
                    field_name,
                    f"Unknown character field '{field_name}'",
                    "请检查字段名拼写，或确认当前角色 YAML 版本是否支持该字段。",
                    code="character.field.unknown",
                )
            )

    def _validate_required_fields(
        self,
        data: dict[str, Any],
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        for field_name in sorted(self.REQUIRED_FIELDS - set(data)):
            diagnostics.append(
                self._error(
                    field_name,
                    f"Required character field '{field_name}' is missing",
                    "请补充角色名称和 system_prompt。",
                    code="character.field.required",
                )
            )

    def _validate_string_field(
        self,
        path: str,
        value: Any,
        diagnostics: list[ConfigDiagnostic],
        *,
        required: bool = False,
        warning_length: int | None = None,
        warning_code: str = "character.field.length_warning",
        warning_suggestion: str | None = None,
    ) -> None:
        if value is None:
            return
        if not isinstance(value, str):
            diagnostics.append(
                self._error(
                    path,
                    f"Character field '{path}' must be a string",
                    "请填写字符串文本。",
                    code="character.field.type",
                )
            )
            return
        if required and not value.strip():
            diagnostics.append(
                self._error(
                    path,
                    f"Character field '{path}' must not be empty",
                    "请填写非空文本。",
                    code="character.field.empty",
                )
            )
            return
        if warning_length is not None and len(value) > warning_length:
            diagnostics.append(
                self._warning(
                    path,
                    f"Character field '{path}' is long ({len(value)} chars)",
                    warning_suggestion,
                    code=warning_code,
                )
            )

    _BEGIN_SCENE_ALLOWED_FIELDS = {"scene", "action"}

    def _validate_begin_scene(self, value: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        """校验 begin_scene：支持旧的纯字符串写法与新的 {scene, action} 结构。"""
        if value is None:
            return
        # 旧写法：纯字符串（等价于只填 action）
        if isinstance(value, str):
            self._validate_string_field(
                "begin_scene",
                value,
                diagnostics,
                warning_length=self.GREETING_WARNING_LENGTH,
                warning_code="character.begin_scene.length_warning",
                warning_suggestion="建议保持开场描述简洁，避免首次回复占用过多上下文。",
            )
            return
        # 新写法：结构化映射
        if not isinstance(value, dict):
            diagnostics.append(
                self._error(
                    "begin_scene",
                    "Character field 'begin_scene' must be a string or an object with "
                    "'scene'/'action'",
                    "请写成一段开场描述字符串，或 {scene: 场景id, action: 开场动作}。",
                    code="character.begin_scene.type",
                )
            )
            return
        for sub in sorted(set(value) - self._BEGIN_SCENE_ALLOWED_FIELDS):
            diagnostics.append(
                self._error(
                    f"begin_scene.{sub}",
                    f"Unknown begin_scene field '{sub}'",
                    "begin_scene 仅支持 scene 与 action 两个字段。",
                    code="character.begin_scene.field.unknown",
                )
            )
        self._validate_string_field("begin_scene.scene", value.get("scene"), diagnostics)
        self._validate_string_field(
            "begin_scene.action",
            value.get("action"),
            diagnostics,
            warning_length=self.GREETING_WARNING_LENGTH,
            warning_code="character.begin_scene.length_warning",
            warning_suggestion="建议保持开场动作简洁，避免首次回复占用过多上下文。",
        )

    @staticmethod
    def _normalize_begin_scene(value: Any) -> BeginScene | None:
        """把 begin_scene 归一化为 BeginScene；无内容时返回 None。"""
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return BeginScene(action=text) if text else None
        if isinstance(value, dict):
            scene = value.get("scene")
            action = value.get("action")
            begin = BeginScene(
                scene=scene.strip() if isinstance(scene, str) and scene.strip() else None,
                action=action.strip() if isinstance(action, str) else "",
            )
            return begin if begin.has_content else None
        return None

    def _validate_example_dialogue(
        self,
        value: Any,
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            diagnostics.append(
                self._error(
                    "example_dialogue",
                    "Character field 'example_dialogue' must be a list",
                    "请使用列表格式，每项包含 user 和 assistant。",
                    code="character.example_dialogue.type",
                )
            )
            return

        total_length = 0
        for index, item in enumerate(value):
            item_path = f"example_dialogue.{index}"
            if not isinstance(item, dict):
                diagnostics.append(
                    self._error(
                        item_path,
                        "Example dialogue item must be an object",
                        "请将每条示例对话写成包含 user 和 assistant 的对象。",
                        code="character.example_dialogue.item_type",
                    )
                )
                continue
            unknown = sorted(set(item) - {"user", "assistant"})
            for field_name in unknown:
                diagnostics.append(
                    self._error(
                        f"{item_path}.{field_name}",
                        f"Unknown example dialogue field '{field_name}'",
                        "示例对话每项仅支持 user 和 assistant。",
                        code="character.example_dialogue.field_unknown",
                    )
                )
            for field_name in ("user", "assistant"):
                field_path = f"{item_path}.{field_name}"
                field_value = item.get(field_name)
                if field_value is None:
                    diagnostics.append(
                        self._error(
                            field_path,
                            f"Example dialogue field '{field_name}' is required",
                            "请为每条示例对话补充 user 和 assistant。",
                            code="character.example_dialogue.field_required",
                        )
                    )
                    continue
                if not isinstance(field_value, str):
                    diagnostics.append(
                        self._error(
                            field_path,
                            f"Example dialogue field '{field_name}' must be a string",
                            "请填写字符串文本。",
                            code="character.example_dialogue.field_type",
                        )
                    )
                    continue
                if not field_value.strip():
                    diagnostics.append(
                        self._error(
                            field_path,
                            f"Example dialogue field '{field_name}' must not be empty",
                            "请填写非空文本。",
                            code="character.example_dialogue.field_empty",
                        )
                    )
                    continue
                total_length += len(field_value)
                if len(field_value) > self.EXAMPLE_DIALOGUE_WARNING_LENGTH:
                    diagnostics.append(
                        self._warning(
                            field_path,
                            f"Example dialogue field '{field_name}' is long ({len(field_value)} chars)",
                            "建议缩短单条示例对话，保留最能体现角色风格的内容。",
                            code="character.example_dialogue.length_warning",
                        )
                    )
        if total_length > self.EXAMPLE_DIALOGUE_TOTAL_WARNING_LENGTH:
            diagnostics.append(
                self._warning(
                    "example_dialogue",
                    f"Example dialogue total length is long ({total_length} chars)",
                    "建议控制示例对话总长度，避免挤占运行时上下文。",
                    code="character.example_dialogue.total_length_warning",
                )
            )

    def _validate_metadata(self, value: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            diagnostics.append(
                self._error(
                    "metadata",
                    "Character field 'metadata' must be an object",
                    "请将 metadata 写成 key/value 对象。",
                    code="character.metadata.type",
                )
            )

    @staticmethod
    def _error(
        path: str,
        message: str,
        suggestion: str | None = None,
        *,
        code: str = "character.validation.error",
    ) -> ConfigDiagnostic:
        return ConfigDiagnostic(
            code=code, path=path, severity="error", message=message, suggestion=suggestion
        )

    @staticmethod
    def _warning(
        path: str,
        message: str,
        suggestion: str | None = None,
        *,
        code: str = "character.validation.warning",
    ) -> ConfigDiagnostic:
        return ConfigDiagnostic(
            code=code, path=path, severity="warning", message=message, suggestion=suggestion
        )
