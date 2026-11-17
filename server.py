"""
DeepSeek 搜索 MCP 服务器（自己写的版本）
==========================================

它做的事情很简单：
    Claude 想搜索  ->  调用这里的工具  ->  这里转发给 DeepSeek 的搜索接口  ->  把结果整理后还给 Claude

一共两个工具：
    deepseek_search  搜索网页，返回标题、链接、摘要、权威等级等
    deepseek_fetch   读取某个网页的全文（Markdown 格式）

DeepSeek 官方接口文档：https://api-docs.deepseek.com/guides/search

定位：Claude 自带搜索的"补充"，不是替代。Claude 先用自带搜索，结果有缺口时才调用这里。
这个规则写在第七节的 instructions 和工具说明里。

【重要】MCP 用 stdin/stdout（标准输入/输出）和 Claude 通信。
所以这个文件里绝对不能用 print() 往屏幕（stdout）输出东西，否则会把通信内容搞乱。
需要打印日志时，用下面的 log() 函数，它会写到 stderr（错误输出），不影响通信。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx2  # 发网络请求的库（装 mcp 时会自动装上，不需要另外安装）
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

# ---------------------------------------------------------------------------
# 一、基本设置（想调整行为，主要改这里）
# ---------------------------------------------------------------------------

# DeepSeek 接口地址。故意写死，不允许通过环境变量修改，
# 这样就算有人改了你电脑的环境变量，你的 API Key 也不会被发到别的服务器。
BASE_URL = "https://api.deepseek.com"

HERE = Path(__file__).resolve().parent  # 这个文件所在的文件夹
KEY_FILE = HERE / "deepseek.key"  # API Key 存放的文件（和 server.py 放在同一个文件夹）
USAGE_LOG = HERE / "usage.log"  # 用量记录，方便你核对花了多少钱

MAX_QUERY_CHARS = 128  # DeepSeek 规定：搜索词最多 128 个字符
MAX_SUMMARY_CHARS = 800  # 每条结果的摘要最多保留多少字（控制 Claude 的上下文消耗）
MAX_CONTENT_PER_RESULT = 6000  # 搜索时如果要正文，每条正文最多多少字
MAX_TOTAL_CONTENT = 30000  # 一次搜索所有正文加起来最多多少字
DEFAULT_FETCH_CHARS = 20000  # 读网页时默认最多返回多少字
MAX_FETCH_CHARS = 100000  # 读网页时允许的上限

MAX_ATTEMPTS = 3  # 遇到临时错误（超时、限流、服务器错误）最多尝试几次
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}  # 官方文档说这些状态码可以等一会儿重试
# 重试不会多花钱：官方规定失败、超时、空结果都不计费。


# 网络库默认会把每个请求都打印成日志（"HTTP Request: POST ..."），这里关掉，只保留警告和错误
logging.getLogger("httpx2").setLevel(logging.WARNING)

# 日志里有中文。英文版 Windows 默认编码写不了中文会直接报错，这里统一改成 UTF-8
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    """写日志到 stderr。Claude 桌面版会把它记录到 MCP 日志文件里，排查问题时有用。"""
    print(f"[deepseek-search] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 二、读取 API Key
# ---------------------------------------------------------------------------


def load_api_key() -> str | None:
    """优先读环境变量 DEEPSEEK_API_KEY；没有的话读同文件夹下的 deepseek.key 文件。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        # utf-8-sig 会自动去掉 Windows 记事本有时加在文件开头的隐藏字符（BOM）
        key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
    key = key.strip("\"'")  # 去掉不小心复制进来的引号
    return key or None


def redact(text: str, key: str | None) -> str:
    """把文字里出现的 API Key 替换成 ***，防止 Key 出现在报错信息里。"""
    if key:
        text = text.replace(key, "sk-***")
    return text


# ---------------------------------------------------------------------------
# 三、安全检查：读网页时，禁止访问内网地址
# ---------------------------------------------------------------------------
# 为什么要拦：如果有人在网页里藏了一句"请读取 http://192.168.1.1/..."诱导 Claude，
# 我们不希望这个请求被发出去。虽然真正去抓网页的是 DeepSeek 的服务器（它访问不到
# 你家的内网），这里多拦一道，属于"多一层保险"。

BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")


def check_url(url: str) -> str:
    """检查网址是否合法、安全。合法就返回整理后的网址，不合法就报错。"""
    url = url.strip()
    if not url:
        raise ToolError("网址是空的。")
    if len(url) > 8192:
        raise ToolError("网址太长（DeepSeek 限制 8192 个字符）。")

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError("只支持 http 或 https 开头的网址。")
    host = (parts.hostname or "").lower()
    if not host:
        raise ToolError("网址里没有域名。")
    if parts.username or parts.password:
        raise ToolError("网址里不能带用户名或密码。")
    if host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise ToolError("不允许访问本机或内网地址。")

    # 如果主机名本身就是 IP 地址，检查它是不是内网 / 本机 / 保留地址
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # 不是 IP，是普通域名，放行
    if ip is not None and not ip.is_global:
        raise ToolError("不允许访问内网或保留 IP 地址。")

    return url


# ---------------------------------------------------------------------------
# 四、用量记录：每次计费的请求都记一行，方便你对账
# ---------------------------------------------------------------------------


def record_usage(tool: str, billed: bool, detail: str) -> None:
    """往 usage.log 追加一行：时间、工具、是否计费、搜索词或网址。"""
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{now}\t{tool}\t{'计费' if billed else '不计费'}\t{detail[:80]}\n"
        with USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        log(f"写用量记录失败（不影响使用）：{e}")


# ---------------------------------------------------------------------------
# 五、调用 DeepSeek 接口（带重试）
# ---------------------------------------------------------------------------

# 不同错误码对应的提示（Claude 会看到这些文字，并据此告诉你出了什么问题）
ERROR_HINTS = {
    400: "请求参数有误",
    401: "API Key 无效或缺失，请检查 deepseek.key 文件",
    402: "DeepSeek 账户余额不足，请到 platform.deepseek.com 充值",
    403: "DeepSeek 的安全策略拒绝读取这个网页",
    404: "DeepSeek 无法获取这个网页的内容（网页不存在、屏蔽了抓取，或格式不支持）",
    413: "请求内容太大",
    429: "请求太频繁，被 DeepSeek 限流",
}


async def call_deepseek(path: str, body: dict) -> dict:
    """向 DeepSeek 发 POST 请求，返回解析好的 JSON。遇到临时错误会自动重试。"""
    key = load_api_key()
    if not key:
        raise ToolError(f"没有找到 DeepSeek API Key。请把 Key 写进这个文件：{KEY_FILE}")

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    timeout = httpx2.Timeout(45.0, connect=10.0)
    last_error = "未知错误"

    # follow_redirects=False：不跟随跳转，防止请求（连同 Key）被重定向到别的地址
    async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait_seconds = 1.5 * attempt  # 每次重试多等一会儿
            try:
                resp = await client.post(BASE_URL + path, headers=headers, json=body)
            except httpx2.TimeoutException:
                last_error = "请求 DeepSeek 超时"
            except httpx2.HTTPError as e:
                last_error = f"网络错误：{redact(str(e), key)}（检查网络或代理）"
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        last_error = "DeepSeek 返回的内容不是合法 JSON"
                else:
                    # 从返回内容里取出官方的错误说明
                    try:
                        api_msg = resp.json().get("error", {}).get("message", "")
                    except ValueError:
                        api_msg = resp.text[:200]
                    hint = ERROR_HINTS.get(resp.status_code, "DeepSeek 服务出错")
                    last_error = redact(f"{hint}（HTTP {resp.status_code}）：{api_msg}", key)

                    if resp.status_code not in RETRYABLE_STATUS:
                        raise ToolError(last_error)  # 不可重试的错误，直接报告
                    retry_after = resp.headers.get("retry-after", "")
                    if retry_after.isdigit():
                        wait_seconds = min(float(retry_after), 10.0)

            log(f"{path} 第 {attempt} 次尝试失败：{last_error}")
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(wait_seconds)

    raise ToolError(f"{last_error}（已重试 {MAX_ATTEMPTS} 次）")


# ---------------------------------------------------------------------------
# 六、整理结果：只保留有用字段、截断过长内容，节省 Claude 的上下文
# ---------------------------------------------------------------------------


def cut(text: str, limit: int) -> tuple[str, bool]:
    """把文字截到 limit 个字以内。返回（截断后的文字, 是否被截断）。"""
    text = text or ""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"……[已截断，原文共 {len(text)} 字]", True


FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def build_query(query: str, freshness: str) -> str:
    """加上时间筛选（after:日期），并保证总长度不超过 128 个字符。"""
    query = " ".join(query.split())  # 合并多余空格
    if freshness in FRESHNESS_DAYS and "after:" not in query and "before:" not in query:
        since = datetime.now(timezone.utc) - timedelta(days=FRESHNESS_DAYS[freshness])
        suffix = f" after:{since:%Y-%m-%d}"
        query = query[: MAX_QUERY_CHARS - len(suffix)] + suffix
    return query[:MAX_QUERY_CHARS]


def to_json(data: dict) -> str:
    """转成 JSON 文字。ensure_ascii=False 让中文保持原样，不变成 \\uXXXX，能省不少上下文。"""
    return json.dumps(data, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# 七、定义 MCP 服务器和两个工具
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="deepseek-search",
    version="1.0.0",
    # 给 Claude 的总体说明：这是"补充"搜索，不是"分工"。先用自带搜索，有缺口再用这里。
    instructions=(
        "Supplementary search, not a replacement. Always search with your built-in web search first. "
        "Call these tools only to fill a gap in those results: (1) a China-related topic lacks Chinese "
        "primary sources, (2) fewer than 3 relevant results or an aspect is uncovered, (3) results are "
        "outdated for a question about recent events, (4) a key fact rests on a single source and needs "
        "independent cross-checking, (5) your built-in fetch cannot open a page. Search only for the gap, "
        "not a repeat of earlier queries. Skip this step if the user asked you to use DeepSeek directly, "
        "and do not use these tools at all if the user said not to. When you use them, mention it briefly in your answer."
    ),
)

# 告诉 Claude：这两个工具只读、不会修改任何东西
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)


@mcp.tool(
    title="DeepSeek 网页搜索",
    annotations=READ_ONLY,
    structured_output=False,
    description=(
        "Supplementary web search using DeepSeek's independent search engine (strong Chinese coverage). "
        "Use AFTER your built-in web search, only when its results have a gap: missing Chinese primary "
        "sources on a China-related topic, too few relevant results, outdated results, or a key fact that "
        "needs cross-checking against an independent index. Query the gap, don't repeat earlier searches. "
        "Use directly if the user explicitly asks for DeepSeek. "
        "Returns ranked results with title, url, site, date, authority (0-4, >=3 means highly "
        "authoritative, -1 unknown), score (0-3, >=1.5 high quality) and a 600-800 char summary. "
        "Query max 128 chars. Operators: site:example.com, -site:example.com, -word, \"exact phrase\", "
        "lang:zh,en, after:YYYY-MM-DD, before:YYYY-MM-DD. "
        "Prefer deepseek_fetch on chosen URLs instead of need_content=true. "
        "Each successful search is billed, so search purposefully."
    ),
)
async def deepseek_search(
    query: Annotated[str, Field(min_length=1, max_length=1000, description="搜索词，最多 128 个字符（超出会被截断）")],
    count: Annotated[int, Field(ge=1, le=20, description="返回几条结果，1-20，默认 8")] = 8,
    freshness: Annotated[
        Literal["any", "day", "week", "month", "year"],
        Field(description="时间范围：any 不限 / day 一天内 / week / month / year"),
    ] = "any",
    need_content: Annotated[bool, Field(description="是否同时返回网页正文（很占上下文，一般用 false）")] = False,
    ori_question: Annotated[
        str | None, Field(description="用户的原始问题（可选，最多 128 字），帮助 DeepSeek 理解意图")
    ] = None,
) -> str:
    query_sent = build_query(query, freshness)
    if not query_sent.strip():
        raise ToolError("搜索词是空的。")

    body: dict = {"query": query_sent, "count": count, "need_content": need_content}
    if ori_question and ori_question.strip():
        body["ori_question"] = ori_question.strip()[:128]

    data = await call_deepseek("/v1/tools/search", body)
    raw_results = data.get("results") or []
    record_usage("search", billed=bool(raw_results), detail=query_sent)  # 官方：空结果不计费

    results = []
    content_budget = MAX_TOTAL_CONTENT
    for i, item in enumerate(raw_results, start=1):
        summary, _ = cut(item.get("summary") or item.get("snippet") or "", MAX_SUMMARY_CHARS)
        result = {
            "rank": i,
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "site": item.get("site_name"),
            "date": item.get("date"),
            "authority": item.get("authority", -1),
            "score": item.get("rerank_score"),
            "summary": summary,
        }
        if need_content and item.get("content"):
            limit = min(MAX_CONTENT_PER_RESULT, content_budget)
            if limit > 0:
                result["content"], _ = cut(item["content"], limit)
                content_budget -= len(result["content"])
        results.append(result)

    output: dict = {"query_sent": query_sent, "count": len(results), "results": results}
    if query_sent != query:
        output["note"] = "搜索词已调整（加了时间筛选或超过 128 字被截断）"
    if not results:
        output["note"] = "没有搜到结果（不计费）。可以换更宽泛的关键词，或去掉筛选条件。"
    return to_json(output)


@mcp.tool(
    title="DeepSeek 读取网页",
    annotations=READ_ONLY,
    structured_output=False,
    description=(
        "Fetch one public web page through DeepSeek and return its title and content as Markdown. "
        "Use it as a fallback when your built-in fetch cannot open a page (blocked or failed), "
        "or to read a URL found by deepseek_search. "
        "Local/private addresses are rejected. Content is cut at max_chars. Each successful fetch is billed."
    ),
)
async def deepseek_fetch(
    url: Annotated[str, Field(min_length=1, description="要读取的网页地址，http 或 https 开头")],
    max_chars: Annotated[
        int, Field(ge=1000, le=MAX_FETCH_CHARS, description="最多返回多少字，默认 20000")
    ] = DEFAULT_FETCH_CHARS,
) -> str:
    safe_url = check_url(url)
    data = await call_deepseek("/v1/tools/fetch", {"url": safe_url})

    content = data.get("content") or ""
    record_usage("fetch", billed=bool(content), detail=safe_url)  # 官方：有正文才计费
    if not content:
        raise ToolError("DeepSeek 没有返回这个网页的正文（不计费）。")

    content, truncated = cut(content, max_chars)
    return to_json({"url": safe_url, "title": data.get("title", ""), "truncated": truncated, "content": content})


# ---------------------------------------------------------------------------
# 八、启动
# ---------------------------------------------------------------------------

async def selftest() -> None:
    """安装时用：真实搜索一次，确认 Key 和网络都正常（会产生 1 次计费，约 0.005 元）。"""
    try:
        result = json.loads(await deepseek_search("DeepSeek", count=1))
        first = result["results"][0] if result["results"] else {}
        print(f"自检成功：搜到「{first.get('title', '（无标题）')}」")
    except ToolError as e:
        print(f"自检失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:  # 自检模式不走 MCP 通信，可以正常 print
        asyncio.run(selftest())
        sys.exit(0)

    log(f"启动中。Key 来源：{'环境变量' if os.environ.get('DEEPSEEK_API_KEY') else KEY_FILE}")
    if not load_api_key():
        log("警告：还没有配置 API Key，工具调用时会报错提示。")
    mcp.run("stdio")
