"""日志实现 - 支持动态配置，并桥接标准 logging 到 Loguru"""

# GensokyoAI\utils\logging.py

import asyncio
import contextlib
import inspect
import logging as std_logging
import os
import sys
from pathlib import Path

from loguru import logger

# 默认关闭完整堆栈，避免日志被异常 traceback 刷屏；可通过环境变量开启
_LOGURU_FULL_TRACEBACK = os.environ.get("LOGURU_FULL_TRACEBACK", "0").lower() in (
    "1",
    "true",
    "yes",
)

# 默认抑制部分底层库的低级别日志，避免污染终端/文件
# 注意：匹配用 split(".")[0]（首段名），条目须写 "aiohttp" 而非 "aiohttp.access"（旧 CODE_INF 07#2）
_SUPPRESSED_LOW_LEVEL_LOGGERS = {"httpcore", "asyncio", "aiohttp"}

# 第三方框架的命名空间：WARNING 以下一律丢弃。
# 注意 nonebot 原生使用 loguru（不经 LoguruHandler 桥接），必须在 sink 层过滤；
# uvicorn/websockets 的连接日志同属这类刷屏噪音。
_SUPPRESSED_THIRD_PARTY_LOGGERS = {"nonebot", "uvicorn", "websockets"}


def _third_party_noise_filter(record) -> bool:
    """loguru sink 过滤器：第三方框架只保留 WARNING 及以上，自家日志不受限。"""
    name = (record["name"] or "").split(".")[0]
    if name in _SUPPRESSED_THIRD_PARTY_LOGGERS:
        return record["level"].no >= logger.level("WARNING").no
    return True


# 移除默认配置
logger.remove()

# 保存 handler IDs 以便后续管理
_handlers = {"console": None, "file": None}

# 默认不添加任何 handler
# 等待应用配置加载后再设置


class LoguruHandler(std_logging.Handler):
    def emit(self, record: std_logging.LogRecord):
        # 抑制 httpcore/asyncio/aiohttp.access 等库的 DEBUG/INFO 日志
        if (
            record.name.split(".")[0] in _SUPPRESSED_LOW_LEVEL_LOGGERS
            # 集合条目须用首段名（split(".")[0] 匹配）；此前 "aiohttp.access" 永不命中（旧 CODE_INF 07#2）
            and record.levelno < std_logging.WARNING
        ):
            return

        # 关闭噪音：Ctrl+C / 服务停机时，uvicorn 各 logger（run_asgi / send / error…）
        # 会把 KeyboardInterrupt / CancelledError 以「Exception in ASGI application」
        # 刷整段堆栈——这两个异常在 uvicorn 里只会是关闭信号与任务取消的下游，直接丢弃
        if record.name.split(".")[0] == "uvicorn":
            exc_type = record.exc_info[0] if record.exc_info else None
            if exc_type is not None and issubclass(
                exc_type, (KeyboardInterrupt, asyncio.CancelledError)
            ):
                return
            # 变体：traceback 以纯文本记入（无 exc_info），同样只在关闭级联中出现
            message = record.getMessage()
            if message.startswith("Traceback") and (
                "KeyboardInterrupt" in message or "asyncio.exceptions.CancelledError" in message
            ):
                return

        # 把其他库的 DEBUG 降级为我们的 TRACE，避免标准库 DEBUG 刷屏
        if record.levelno == std_logging.DEBUG:
            level = "TRACE"
        else:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == std_logging.__file__):
            frame = frame.f_back
            depth += 1

        # 默认只对 ERROR 及以上保留异常 traceback，防止 WARNING/INFO 被堆栈刷屏
        exc = (
            record.exc_info
            if _LOGURU_FULL_TRACEBACK or record.levelno >= std_logging.ERROR
            else False
        )

        logger.opt(depth=depth, exception=exc, colors=False).log(level, "{}", record.getMessage())


def setup_logging(
    log_level: str = "INFO",
    log_console: bool = True,
    log_file: Path | None = None,
    log_format: str | None = None,
    log_format_console: str | None = None,
    intercept_standard_logging: bool = True,
) -> None:
    """配置日志系统

    Args:
        log_level: 日志级别 (TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_console: 是否输出到控制台
        log_file: 日志文件路径
        log_format: 文件日志格式
        log_format_console: 控制台日志格式
        intercept_standard_logging: 是否拦截标准 logging 库的日志
    """
    global _handlers

    # 移除现有 handlers；id 可能已被外部 logger.remove()（如 nonebot.init）作废——
    # 此时 remove 会抛 ValueError，容忍并视为已移除
    if _handlers["console"] is not None:
        with contextlib.suppress(ValueError):
            logger.remove(_handlers["console"])
        _handlers["console"] = None
    if _handlers["file"] is not None:
        with contextlib.suppress(ValueError):
            logger.remove(_handlers["file"])
        _handlers["file"] = None

    # 默认格式
    if log_format is None:
        log_format = (
            "[{thread.name:^12}] {time:HH:mm:ss} | {level:<8} | "
            "{name}.{function}:{line:03d} | {message}"
        )

    if log_format_console is None:
        log_format_console = (
            "<level>[{thread.name:^12}] {time:HH:mm:ss} | {level:<8} | "
            "{name}.{function}:{line:03d} | {message}</level>"
        )

    # 添加控制台 handler
    if log_console:
        _handlers["console"] = logger.add(  # type: ignore
            sys.stderr,
            format=log_format_console,
            level=log_level,
            colorize=True,
            filter=_third_party_noise_filter,
        )

    # 添加文件 handler
    if log_file is not None:
        # 确保日志目录存在
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        _handlers["file"] = logger.add(  # type: ignore
            str(log_file),
            format=log_format,
            level=log_level,
            rotation="10 MB",
            compression="zip",
            backtrace=_LOGURU_FULL_TRACEBACK,
            diagnose=_LOGURU_FULL_TRACEBACK,
            enqueue=True,
            filter=_third_party_noise_filter,
        )

    # 拦截标准 logging
    if intercept_standard_logging:
        # 获取标准 logging 的根 Logger
        root_logger = std_logging.getLogger()
        # 移除原有的 Handler，避免重复输出
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        # 挂载 LoguruHandler
        root_logger.addHandler(LoguruHandler())
        # 设置级别为最低，让 Loguru 的 filter 决定是否输出
        root_logger.setLevel(std_logging.DEBUG)


# 为了兼容性，保留原有的 logger 导出
__all__ = [
    "logger",
    "setup_logging",
    "LoguruHandler",
]


# 测试代码
if __name__ == "__main__":
    print("=" * 60)
    print("测试 1: 仅控制台输出")
    print("=" * 60)

    setup_logging(log_level="DEBUG", log_console=True)
    logger.trace("这是一个 trace 级别的日志")
    logger.debug("这是一个 debug 级别的日志")
    logger.info("这是一个 info 级别的日志")
    logger.success("这是一个 success 级别的日志")
    logger.warning("这是一个 warning 级别的日志")
    logger.error("这是一个 error 级别的日志")
    logger.critical("这是一个 critical 级别的日志")

    print("\n" + "=" * 60)
    print("测试 2: 标准 logging 桥接测试")
    print("=" * 60)

    # 测试标准 logging 是否被桥接
    std_logging.debug("这是标准 logging 的 DEBUG 日志")
    std_logging.info("这是标准 logging 的 INFO 日志")
    std_logging.warning("这是标准 logging 的 WARNING 日志")
    std_logging.error("这是标准 logging 的 ERROR 日志")

    print("\n" + "=" * 60)
    print("测试 3: 文件日志测试")
    print("=" * 60)

    setup_logging(
        log_level="INFO",
        log_console=False,
        log_file=Path("logs/test.log"),
        intercept_standard_logging=True,
    )
    logger.info("这条日志只写入文件")
    std_logging.warning("这条标准 logging 的警告也只写入文件")

    print("日志已写入 logs/test.log")
    print("\n文件内容:")
    if Path("logs/test.log").exists():
        print(Path("logs/test.log").read_text(encoding="utf-8"))
