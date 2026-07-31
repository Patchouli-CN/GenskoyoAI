"""美化异常堆栈模块"""

# GensokyoAI\utils\exec_hook.py

import inspect
import multiprocessing
import sys
import threading
import traceback

from .logger import logger


def format_stack_trace(exctype, value, tb, max_depth=15, nested=False) -> list[str]:
    tb_list = traceback.extract_tb(tb)
    exception_info: list[str] = []

    if nested:
        exception_info.append(f"{exctype.__name__}: {value}\n")
    else:
        # 获取当前进程和线程名称
        process_name = multiprocessing.current_process().name
        thread_name = threading.current_thread().name
        exception_info.append(
            f"Exception in process: {process_name}, thread: {thread_name}; {exctype.__name__}: {value}",
        )
        exception_info.append("Traceback (most recent call last):")

    # 限制堆栈跟踪的深度
    limited_tb_list = tb_list[:max_depth]
    more_frames = len(tb_list) - max_depth

    for _i, (filename, lineno, funcname, _line) in enumerate(limited_tb_list):
        # 获取函数所在的模块名
        module_name = inspect.getmodulename(filename)
        exception_info.append(
            f"  at {module_name}.{funcname} in ({filename}:{lineno})",
        )

    if more_frames > 0:
        exception_info.append(f"  ... {more_frames} more ...")

    # 检查是否有原因和其他信息
    cause = getattr(value, "__cause__", None)
    context = getattr(value, "__context__", None)

    if cause:
        exception_info.append("Caused by: ")
        exception_info.extend(
            format_stack_trace(type(cause), cause, cause.__traceback__, nested=True)
        )
    if context and not cause:
        exception_info.append("Original exception: ")
        exception_info.extend(
            format_stack_trace(type(context), context, context.__traceback__, nested=True)
        )

    return exception_info


def extract_exception(exctype, value, tb) -> list[str] | None:
    # 获取回溯信息并格式化为字符串
    if all(x is None for x in (exctype, value, tb)):
        return None

    tb_str = format_stack_trace(exctype, value, tb)

    return tb_str


def sys_excepthook(exctype: type[BaseException], value: BaseException, tb):
    # 获取异常信息并打印到控制台
    if exctype in (KeyboardInterrupt, SystemExit):
        # 静默处理，不打印 CRITICAL 日志
        # 但如果是 SystemExit 且带有非零退出码，可以记录一下
        if type(value) is SystemExit:
            logger.warning(f"程序退出，退出码: {value.code}")
        return

    exception_info = extract_exception(exctype, value, tb)
    if exception_info:
        for exception_element in exception_info:
            logger.critical(exception_element)


def set_exechook():
    """
    设置全局异常处理函数
    """
    sys.excepthook = sys_excepthook


if __name__ == "__main__":
    set_exechook()
