"""
测试 server.py。全部在本机完成，不会访问真正的 DeepSeek：
  1. 用 stdio 真实启动 server.py，检查握手、工具列表、没有 Key 时的提示、内网地址拦截
  2. 在本机起一个"假 DeepSeek"服务器，检查结果整理、截断、重试、错误处理、用量记录
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

SERVER_FILE = Path(sys.argv[1]).resolve()
PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def text_of(result):
    return "".join(getattr(c, "text", "") for c in result.content)


# ---------------------------------------------------------------- 假 DeepSeek
class FakeDeepSeek(BaseHTTPRequestHandler):
    calls = []  # 记录收到的请求
    search_429_times = 0  # 前几次搜索返回 429，用来测重试

    def log_message(self, *a):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization", "")
        FakeDeepSeek.calls.append((self.path, auth, body))

        if auth != "Bearer sk-test-local":
            return self._send(401, {"error": {"message": f"bad key {auth[7:]}"}})

        if self.path == "/v1/tools/search":
            if FakeDeepSeek.search_429_times > 0:
                FakeDeepSeek.search_429_times -= 1
                return self._send(429, {"error": {"message": "rate limited"}})
            if body["query"].startswith("空"):
                return self._send(200, {"results": []})
            results = [
                {
                    "title": f"结果{i}",
                    "url": f"https://example.cn/{i}",
                    "snippet": "短摘要",
                    "summary": "长摘要" * 500,  # 1500 字，应该被截到 800
                    "content": "正文" * 5000,  # 10000 字
                    "site_name": "示例网",
                    "date": "2026-11-01T00:00:00Z",
                    "authority": 3,
                    "rerank_score": 2.1,
                    "site_icon": "https://example.cn/icon.png",
                }
                for i in range(body["count"])
            ]
            return self._send(200, {"results": results})

        if self.path == "/v1/tools/fetch":
            if "404" in body["url"]:
                return self._send(404, {"error": {"message": "no content"}})
            return self._send(200, {"title": "网页标题", "content": "内容" * 20000})

        self._send(404, {"error": {"message": "unknown path"}})


# ---------------------------------------------------------------- 测试 1：stdio
async def test_stdio():
    print("测试 1：真实启动 server.py（stdio，没有 Key）")
    tmp = Path(tempfile.mkdtemp())
    # 复制到临时目录，确保旁边没有 deepseek.key
    (tmp / "server.py").write_text(SERVER_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    params = StdioServerParameters(command=sys.executable, args=[str(tmp / "server.py")], env=env)

    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        names = sorted(t.name for t in tools)
        check("工具列表是 deepseek_fetch / deepseek_search", names == ["deepseek_fetch", "deepseek_search"], names)
        check("两个工具都标记为只读", all(t.annotations and t.annotations.read_only_hint for t in tools))
        search_schema = next(t for t in tools if t.name == "deepseek_search").input_schema
        check("search 只要求 query 必填", search_schema.get("required") == ["query"], search_schema.get("required"))

        r = await client.call_tool("deepseek_search", {"query": "测试"})
        check("没有 Key 时返回清楚的错误提示", r.is_error and "deepseek.key" in text_of(r), text_of(r))

        for bad in ["http://192.168.1.1/admin", "http://localhost:8080", "http://[::1]/", "file:///C:/x", "http://a:b@x.com", "http://nas.local/"]:
            r = await client.call_tool("deepseek_fetch", {"url": bad})
            check(f"拦截 {bad}", r.is_error, text_of(r))

        r = await client.call_tool("deepseek_search", {"query": "x", "count": 99})
        check("count 超出范围会被拒绝", r.is_error)


# ---------------------------------------------------------------- 测试 2：假 DeepSeek
async def test_with_fake_deepseek():
    print("测试 2：连接本机的假 DeepSeek")
    httpd = HTTPServer(("127.0.0.1", 0), FakeDeepSeek)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    spec = importlib.util.spec_from_file_location("server_under_test", SERVER_FILE)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    srv.BASE_URL = f"http://127.0.0.1:{httpd.server_port}"  # 只在测试里改成本机地址
    tmp = Path(tempfile.mkdtemp())
    srv.USAGE_LOG = tmp / "usage.log"
    srv.KEY_FILE = tmp / "deepseek.key"
    real_sleep = asyncio.sleep
    srv.asyncio.sleep = lambda s: real_sleep(0)  # 测试时不真的等待

    # Key 从文件读取（带 BOM 和引号，模拟记事本保存）
    srv.KEY_FILE.write_text('\ufeff"sk-test-local"\n', encoding="utf-8")
    os.environ.pop("DEEPSEEK_API_KEY", None)

    async with Client(srv.mcp) as client:
        r = await client.call_tool("deepseek_search", {"query": "国产 AI 办公", "count": 3})
        data = json.loads(text_of(r))
        check("搜索成功，返回 3 条", not r.is_error and data["count"] == 3, text_of(r)[:200])
        first = data["results"][0]
        check("摘要被截到 800 字以内（加截断提示）", first["summary"].startswith("长摘要") and len(first["summary"]) < 830)
        check("默认不返回正文", "content" not in first)
        check("不返回无用字段 site_icon", "site_icon" not in first)
        check("中文没有被转成 \\uXXXX", "\\u" not in text_of(r))
        path, auth, body = FakeDeepSeek.calls[-1]
        check("从 key 文件读到的 Key 去掉了 BOM 和引号", auth == "Bearer sk-test-local", auth)

        r = await client.call_tool("deepseek_search", {"query": "新闻", "count": 10, "need_content": True})
        data = json.loads(text_of(r))
        total = sum(len(x.get("content", "")) for x in data["results"])
        check("正文总量受上限控制", total <= srv.MAX_TOTAL_CONTENT + 200, total)

        r = await client.call_tool("deepseek_search", {"query": "政策", "freshness": "week"})
        sent = FakeDeepSeek.calls[-1][2]["query"]
        check("时间筛选会加上 after:日期", sent.startswith("政策 after:20"), sent)

        long_q = "很长" * 100
        await client.call_tool("deepseek_search", {"query": long_q, "freshness": "month"})
        sent = FakeDeepSeek.calls[-1][2]["query"]
        check("超长搜索词截到 128 字以内且保留时间筛选", len(sent) <= 128 and "after:" in sent, len(sent))

        FakeDeepSeek.search_429_times = 2
        n_before = len(FakeDeepSeek.calls)
        r = await client.call_tool("deepseek_search", {"query": "重试测试"})
        check("遇到 429 自动重试后成功", not r.is_error and len(FakeDeepSeek.calls) - n_before == 3)

        FakeDeepSeek.search_429_times = 5
        r = await client.call_tool("deepseek_search", {"query": "一直限流"})
        check("一直失败时最多试 3 次并报错", r.is_error and "已重试 3 次" in text_of(r), text_of(r))
        FakeDeepSeek.search_429_times = 0

        r = await client.call_tool("deepseek_search", {"query": "空结果"})
        check("空结果给出提示", "没有搜到结果" in text_of(r))

        r = await client.call_tool("deepseek_fetch", {"url": "https://example.cn/page"})
        data = json.loads(text_of(r))
        check("读网页默认截到 2 万字左右", data["truncated"] and len(data["content"]) < 20100)

        r = await client.call_tool("deepseek_fetch", {"url": "https://example.cn/404"})
        check("404 给出中文提示且不重试", r.is_error and "无法获取" in text_of(r), text_of(r))

        # 错误的 Key：报错信息里不能出现 Key 本身
        srv.KEY_FILE.write_text("sk-WRONG-SECRET-123", encoding="utf-8")
        r = await client.call_tool("deepseek_search", {"query": "x"})
        check("Key 错误时提示 401", r.is_error and "401" in text_of(r))
        check("报错信息里 Key 被隐藏", "sk-WRONG-SECRET-123" not in text_of(r), text_of(r))

    log_lines = srv.USAGE_LOG.read_text(encoding="utf-8").splitlines()
    billed = [l for l in log_lines if "\t计费\t" in l]
    unbilled = [l for l in log_lines if "\t不计费\t" in l]
    check("用量记录：计费和不计费分开记", len(billed) == 6 and len(unbilled) == 1, (len(billed), len(unbilled)))
    httpd.shutdown()


async def main():
    await test_stdio()
    await test_with_fake_deepseek()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
