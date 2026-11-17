# DeepSeek Search MCP

[![tests](https://github.com/lovwellcomm-cell/deepseek-search-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/lovwellcomm-cell/deepseek-search-mcp/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[简体中文](README.md) | English

An MCP server that adds [DeepSeek's in-house search engine](https://api-docs.deepseek.com/guides/search) to Claude (Desktop and Claude Code) as a **supplement** to Claude's built-in web search — filling its gaps on Chinese-language and China-specific information.

## Design: supplement, not division of labor

Instead of routing "Chinese → DeepSeek, English → Claude", Claude **always searches with its built-in tool first**, and calls DeepSeek only when the results show one of five gaps:

1. **Missing Chinese primary sources** on a China-related topic
2. **Too few relevant results** (fewer than 3, or an aspect uncovered)
3. **Outdated results** for a question about recent events
4. **A key fact rests on a single source** — cross-check it against an independent index
5. **A page can't be opened** by the built-in fetch tool — fall back to `deepseek_fetch`

DeepSeek is queried only for the gap; results are merged, deduplicated, and conflicts are resolved in favor of primary and higher-authority sources. This costs less than routing every Chinese query to DeepSeek, keeps Claude's default behavior intact, and enables independent cross-checking of key facts.

The rules live in both the MCP tool descriptions (`server.py`, section 7) and the bundled [Skill](skills/deepseek-search-routing/SKILL.md), so Claude sees one consistent policy.

## Tools

| Tool | Description |
|---|---|
| `deepseek_search` | Web search returning title, URL, site, date, authority tier (0-4), relevance score and a 600-800 char summary. Supports `site:`, `after:` and other operators plus a freshness filter |
| `deepseek_fetch` | Fetch a single page as Markdown, with a configurable length cap |

## Security

- API base URL is hard-coded (`https://api.deepseek.com`), not overridable via environment, and redirects are not followed
- The API key is redacted from all error messages and stored in a local `deepseek.key` file (git-ignored), not in Claude's config
- All dependencies are pinned by version **and hash** (`pip --require-hashes`) to block tampered packages
- `deepseek_fetch` rejects localhost and private-network addresses
- Both tools are annotated read-only
- The installer backs up Claude's config before editing and aborts without changes if the config is malformed

## Platforms

| | Claude Desktop | Claude Code |
|---|---|---|
| Windows 10/11 | ✅ tested on a real machine | ✅ |
| macOS (Apple Silicon) | ⚠️ supported, not yet tested on a real machine | ⚠️ same |
| macOS (Intel) | ❌ a dependency no longer ships Intel Mac wheels | ❌ |

Requires **Python 3.11+**.

## Install

1. Create a DeepSeek API key at [platform.deepseek.com](https://platform.deepseek.com) (top up a small balance first).
2. `git clone https://github.com/lovwellcomm-cell/deepseek-search-mcp.git` — keep it in a permanent location.
3. Fully quit Claude Desktop, then run **`setup.bat`** (Windows) or **`bash setup.sh`** (macOS). The installer creates an isolated venv, installs hash-pinned dependencies, stores your key, optionally runs a live self-test, and registers the server with Claude Desktop and/or Claude Code (`claude mcp add --scope user`).
4. Install the bundled Skill: zip `skills/deepseek-search-routing` and upload it in Claude's Skills settings, or copy the folder into `~/.claude/skills/` for Claude Code.

Uninstall with `uninstall.bat` (Windows) or `bash setup.sh --uninstall` (macOS).

## Cost

DeepSeek charges **¥5 (≈ $0.75) per 1,000 successful requests** for Search and for Fetch separately. Failed, timed-out and empty-result requests are free. Every billed call is logged to `usage.log`.

## Development

```bash
python -m venv .venv
.venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.txt
.venv/bin/python tests/test_server.py server.py
```

Tests run against a local mock DeepSeek server (no network, no cost) and cover result shaping, truncation, retry on rate limits, error handling, key redaction, private-address blocking and usage logging. CI runs them on Windows, macOS and Linux with Python 3.11 and 3.13.

## Acknowledgments

- [DeepSeek Search & Fetch API docs](https://api-docs.deepseek.com/guides/search)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- Truncation, retry and URL-validation ideas informed by [BryleXia/deepseek-search-fetch-mcp](https://github.com/BryleXia/deepseek-search-fetch-mcp) (MIT)

Not affiliated with DeepSeek or Anthropic. Licensed under [MIT](LICENSE).
