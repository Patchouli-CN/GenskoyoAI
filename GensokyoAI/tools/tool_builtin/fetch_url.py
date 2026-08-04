"""fetch_url 工具：抓取指定 URL 的正文内容（aiohttp + SSRF 校验 + 截断）。

定位：知识站点（knowledge_sites 配置）的首选抓取通道——角色/作品/设定等
领域知识优先抓这些权威站；与领域无关的内容才走 web_search 搜索。
技术细节（异常栈/URL 内部）只进日志，给模型的只有干净结果。
"""

import html
import re

import aiohttp

from ...utils.logger import logger
from ...utils.url_security import UnsafeUrlError, validate_external_url
from ..base import tool

# 抓取与输出护栏
_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MAX_BYTES = 1_048_576  # 1 MB 读取上限
_MAX_OUTPUT_CHARS = 4000  # 给模型的正文上限（防一次抓取打爆上下文）
_USER_AGENT = {"User-Agent": "GensokyoAI fetch_url/1.0"}

_SCRIPT_STYLE_PATTERN = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """粗粒度 HTML → 纯文本（剥 script/style + 标签替空格 + 实体反转义）。"""
    text = _SCRIPT_STYLE_PATTERN.sub(" ", text)
    text = _TAG_PATTERN.sub(" ", text)
    return html.unescape(text)


@tool(parallel_safe=True)
async def fetch_url(url: str) -> dict:
    """抓取指定 URL 的正文内容（自动去 HTML 标签、截断到 4000 字）。查角色/作品/设定等领域知识时，优先抓取知识站点（见工具指令里的知识站点表）里的页面或调用其站内搜索/API；与领域无关的搜索请用 web_search。"""
    url = url.strip()
    if not url:
        return {"ok": False, "error": "URL 不能为空"}
    try:
        validate_external_url(url)
    except UnsafeUrlError as error:
        return {"ok": False, "error": f"这个地址不允许访问（{error.reason}）"}
    try:
        async with aiohttp.ClientSession(
            timeout=_FETCH_TIMEOUT, headers=_USER_AGENT
        ) as session, session.get(url, allow_redirects=True) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            raw = await response.content.read(_MAX_BYTES + 1)
    except Exception as error:
        # 技术细节只进日志；给模型一句干净的人话（ddg 错误串泄漏台词的教训）
        logger.debug(f"fetch_url 抓取失败（{url[:80]}）: {error}")
        return {"ok": False, "error": f"抓取失败（{type(error).__name__}），稍后再试"}
    text = raw.decode("utf-8", "replace")
    if "html" in content_type.lower():
        text = _strip_html(text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()
    truncated = len(text) > _MAX_OUTPUT_CHARS
    return {
        "ok": 200 <= status < 300,
        "status": status,
        "content": text[:_MAX_OUTPUT_CHARS],
        "truncated": truncated,
    }
