"""环境变量配置覆盖 + 适配器配置目录约定 + 本地配置播种。

原独立模块 core/config_dirs.py 仅两个纯函数，并入本文件（避免为两个函数
单独拆文件；2026-08-02 用户定稿的 config/{adapter}/ 约定不变）。
"""

import os
import shutil
from pathlib import Path

from ..utils.logger import logger
from ..utils.url_security import validate_external_url
from .config_schema import AppConfig, AuthConfig, LogLevel


def apply_env_overrides(config: AppConfig) -> AppConfig:
    """应用环境变量。"""
    if os.getenv("GENSOKYOAI_PROVIDER"):
        config.model.provider = os.getenv("GENSOKYOAI_PROVIDER")  # type: ignore
    if os.getenv("GENSOKYOAI_MODEL"):
        config.model.name = os.getenv("GENSOKYOAI_MODEL")  # type: ignore
    if os.getenv("GENSOKYOAI_API_KEY"):
        config.model.api_key = os.getenv("GENSOKYOAI_API_KEY")  # type: ignore
    if (base_url := os.getenv("GENSOKYOAI_BASE_URL")):
        config.model.base_url = base_url  # type: ignore
        # SSRF 校验对齐 YAML 层（config_validator 518-528）：本地 ollama 由 provider spec 放行。
        # 延迟导入：adapters→config_env 在包初始化早期被引入，模块级引 specs 会触发循环
        from .agent.providers.specs import PROVIDER_SPECS

        model_spec = (
            PROVIDER_SPECS.get(config.model.provider)
            if isinstance(config.model.provider, str)
            else None
        )
        validate_external_url(
            base_url, allow_private=bool(model_spec and model_spec.allow_private_base_url)
        )
    if os.getenv("GENSOKYOAI_API_PATH"):
        config.model.api_path = os.getenv("GENSOKYOAI_API_PATH")  # type: ignore
    if os.getenv("GENSOKYOAI_AUTH_TYPE"):
        config.model.auth = config.model.auth or AuthConfig()
        config.model.auth.auth_type = os.getenv("GENSOKYOAI_AUTH_TYPE")  # type: ignore
    if (token_url := os.getenv("GENSOKYOAI_TOKEN_URL")):
        config.model.auth = config.model.auth or AuthConfig()
        config.model.auth.token_url = token_url  # type: ignore
        # OAuth token 端点同样是 URL，过 SSRF 校验（YAML 层尚未覆盖，此处补 env 路径）
        validate_external_url(token_url)
    if os.getenv("GENSOKYOAI_ACCESS_TOKEN"):
        config.model.auth = config.model.auth or AuthConfig()
        config.model.auth.access_token = os.getenv("GENSOKYOAI_ACCESS_TOKEN")  # type: ignore
    if os.getenv("GENSOKYOAI_REFRESH_TOKEN"):
        config.model.auth = config.model.auth or AuthConfig()
        config.model.auth.refresh_token = os.getenv("GENSOKYOAI_REFRESH_TOKEN")  # type: ignore
    if os.getenv("GENSOKYOAI_CLIENT_ID"):
        config.model.auth = config.model.auth or AuthConfig()
        config.model.auth.client_id = os.getenv("GENSOKYOAI_CLIENT_ID")  # type: ignore
    if os.getenv("GENSOKYOAI_CLIENT_SECRET"):
        config.model.auth = config.model.auth or AuthConfig()
        config.model.auth.client_secret = os.getenv("GENSOKYOAI_CLIENT_SECRET")  # type: ignore
    if os.getenv("GENSOKYOAI_RETRY_MAX_ATTEMPTS"):
        config.model.retry_max_attempts = int(os.getenv("GENSOKYOAI_RETRY_MAX_ATTEMPTS"))  # type: ignore
    if os.getenv("GENSOKYOAI_RETRY_INITIAL_DELAY"):
        config.model.retry_initial_delay = float(os.getenv("GENSOKYOAI_RETRY_INITIAL_DELAY"))  # type: ignore
    if os.getenv("GENSOKYOAI_RETRY_BACKOFF_FACTOR"):
        config.model.retry_backoff_factor = float(os.getenv("GENSOKYOAI_RETRY_BACKOFF_FACTOR"))  # type: ignore
    if os.getenv("GENSOKYOAI_RETRY_STATUS_CODES"):
        config.model.retry_status_codes = [
            int(code.strip())
            for code in os.getenv("GENSOKYOAI_RETRY_STATUS_CODES", "").split(",")
            if code.strip()
        ]  # type: ignore
    if os.getenv("GENSOKYOAI_THINKING_ENABLED"):
        config.model.thinking_enabled = os.getenv("GENSOKYOAI_THINKING_ENABLED").lower() == "true"  # type: ignore
    if os.getenv("GENSOKYOAI_REASONING_EFFORT"):
        config.model.reasoning_effort = os.getenv("GENSOKYOAI_REASONING_EFFORT")  # type: ignore
    if os.getenv("GENSOKYOAI_EMBEDDING_PROVIDER"):
        config.embedding.provider = os.getenv("GENSOKYOAI_EMBEDDING_PROVIDER")  # type: ignore
    if os.getenv("GENSOKYOAI_EMBEDDING_MODEL"):
        config.embedding.name = os.getenv("GENSOKYOAI_EMBEDDING_MODEL")  # type: ignore
    if os.getenv("GENSOKYOAI_EMBEDDING_API_KEY"):
        config.embedding.api_key = os.getenv("GENSOKYOAI_EMBEDDING_API_KEY")  # type: ignore
    if (embedding_base_url := os.getenv("GENSOKYOAI_EMBEDDING_BASE_URL")):
        config.embedding.base_url = embedding_base_url  # type: ignore
        # 与 YAML 层一致（config_validator 660-665）：embedding base_url 恒禁私网/内网
        validate_external_url(embedding_base_url)
    if os.getenv("GENSOKYOAI_EMBEDDING_DIMENSIONS"):
        config.embedding.dimensions = int(os.getenv("GENSOKYOAI_EMBEDDING_DIMENSIONS"))  # type: ignore
    if os.getenv("GENSOKYOAI_EMBEDDING_ENCODING_FORMAT"):
        config.embedding.encoding_format = os.getenv("GENSOKYOAI_EMBEDDING_ENCODING_FORMAT")  # type: ignore
    if os.getenv("GENSOKYOAI_EMBEDDING_TIMEOUT"):
        config.embedding.timeout = int(os.getenv("GENSOKYOAI_EMBEDDING_TIMEOUT"))  # type: ignore
    if embedding_use_proxy := os.getenv("GENSOKYOAI_EMBEDDING_USE_PROXY"):
        config.embedding.use_proxy = embedding_use_proxy.lower() == "true"
    if (log_level_env := os.getenv("GENSOKYOAI_LOG_LEVEL")):
        config.log_level = LogLevel(log_level_env.upper())  # 小写 debug 也认，不再 ValueError
    if os.getenv("GENSOKYOAI_LOG_CONSOLE"):
        config.log_console = os.getenv("GENSOKYOAI_LOG_CONSOLE").lower() == "true"  # type: ignore
    if debug_silent_output := os.getenv("GENSOKYOAI_DEBUG_SILENT_OUTPUT"):
        config.debug_silent_output = debug_silent_output.lower() == "true"
    if event_trace_enabled := os.getenv("GENSOKYOAI_EVENT_TRACE_ENABLED"):
        config.event_trace_enabled = event_trace_enabled.lower() == "true"
    if os.getenv("GENSOKYOAI_MEMORY_WORKING_TURNS"):
        config.memory.working_max_turns = int(
            os.getenv("GENSOKYOAI_MEMORY_WORKING_TURNS")  # type: ignore
        )
    return config


def adapter_config_dir(adapter_name: str, root_dir: Path | None = None) -> Path:
    """适配器私有配置目录（root_dir 为项目根解析基准，默认 cwd）。

    框架只约定目录（config/{adapter}/），配置格式与加载器完全归适配器自己——
    框架只做路径约定，零格式耦合。
    """
    return (root_dir or Path.cwd()) / "config" / adapter_name


def ensure_local_config(root_dir: Path | None = None) -> tuple[Path, bool]:
    """框架本地配置（config/local.yaml）不存在时从发行模板播种一份。

    返回 (配置路径, 是否本次新生成)。模板缺失时只返回路径不报错
    （加载层会按缺省继续）；已存在则原样返回、绝不覆盖。
    """
    base = root_dir or Path.cwd()
    local_path = base / "config" / "local.yaml"
    if local_path.exists():
        return local_path, False
    template = base / "tmp" / "template-conf.yaml"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if template.exists():
        shutil.copyfile(template, local_path)
        logger.info(
            f"首次运行已生成本地配置: {local_path}（请修改它，不要改 tmp/ 模板）"
        )
        return local_path, True
    return local_path, False
