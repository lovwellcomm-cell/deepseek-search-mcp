"""
安装 / 卸载 DeepSeek 搜索 MCP（只用 Python 自带的库，不需要额外安装任何东西）

支持：Windows / macOS；Claude 桌面版 / Claude Code（命令行版）

用法（一般直接运行 setup.bat 或 setup.sh 即可，它们会调用这个文件）：
    python install.py                安装
    python install.py --uninstall    从 Claude 配置里移除（文件夹本身不删）
    python install.py --config 路径   手动指定 Claude 桌面版配置文件的位置
                                     （Windows 上也可以把配置文件直接拖到 setup.bat 上）

安装做了这几件事：
    1. 在本文件夹里建一个独立的 Python 虚拟环境 .venv（不影响你电脑上其他 Python 项目）
    2. 按 requirements.txt 安装依赖，逐个核对哈希，防止安装到被篡改的包
    3. 让你输入 DeepSeek API Key，保存到 deepseek.key
    4. （可选）真实搜索一次，确认能用
    5. 加入 Claude 桌面版（先备份配置）和 / 或 Claude Code
"""

from __future__ import annotations

import getpass
import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
VENV_PY = VENV / "Scripts" / "python.exe" if IS_WINDOWS else VENV / "bin" / "python"
SERVER = HERE / "server.py"
KEY_FILE = HERE / "deepseek.key"
SERVER_NAME = "deepseek-search"
SAVED_CONFIG_PATH = HERE / "claude_config_path.txt"  # 记住找到的配置文件位置，卸载时直接用
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"  # 清华镜像，官方源慢时备用


def step(n: int, text: str) -> None:
    print(f"\n[{n}/5] {text}")


def ask_yes(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}] ").strip().lower()
    return default if not answer else answer in ("y", "yes", "是")


def stop(message: str) -> None:
    print(f"\n✗ {message}")
    sys.exit(1)


# ================================================================ Claude 桌面版
def candidate_config_patterns() -> list[str]:
    """Claude 桌面版配置文件可能在的位置（不同系统、不同安装方式不一样）。"""
    if IS_MAC:
        return [str(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")]

    patterns = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        patterns.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # 新版：%LOCALAPPDATA%\Claude-Data\anthropicpbc.claude_xxxx\
        patterns.append(os.path.join(local, "Claude-Data", "*", "claude_desktop_config.json"))
        # MSIX 安装时，Windows 会把应用的文件转存到 Packages\<包名>\LocalCache\ 下面
        pkg = os.path.join(local, "Packages", "*Claude*", "LocalCache")
        patterns.append(os.path.join(pkg, "Roaming", "Claude", "claude_desktop_config.json"))
        patterns.append(os.path.join(pkg, "Local", "Claude-Data", "*", "claude_desktop_config.json"))
        patterns.append(os.path.join(pkg, "**", "claude_desktop_config.json"))  # 兜底：整个包里搜一遍
    return patterns


def find_claude_config(explicit: str | None) -> Path | None:
    """找到 Claude 桌面版的配置文件。找不到返回 None。"""
    # 1. 用户手动指定（--config，或者把配置文件拖到 setup.bat 上）
    if explicit:
        path = Path(explicit.strip().strip('"'))
        if not path.is_file():
            stop(f"指定的配置文件不存在：{path}")
        return path

    # 2. 上次安装时记住的位置
    if SAVED_CONFIG_PATH.exists():
        saved = Path(SAVED_CONFIG_PATH.read_text(encoding="utf-8").strip())
        if saved.is_file():
            return saved

    # 3. 自动在常见位置查找
    found = {Path(p) for pattern in candidate_config_patterns() for p in glob.glob(pattern, recursive=True)}
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)  # 找到多个时，用最近修改过的那个


def claude_desktop_running() -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe"], capture_output=True, text=True, errors="ignore")
        return "Claude.exe" in out.stdout
    if IS_MAC:
        return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True).returncode == 0
    return False


def wait_for_claude_closed() -> None:
    how = "右下角托盘图标 → 右键 → 退出" if IS_WINDOWS else "菜单栏 Claude → Quit，或按 Cmd+Q"
    while claude_desktop_running():
        input(f"\n! Claude 桌面版正在运行。请彻底退出（{how}），\n  否则它关闭时可能把配置改回去。退出后按回车继续……")


def load_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        stop(f"Claude 配置文件格式有误（第 {e.lineno} 行），为了安全没有做任何修改：\n  {path}")
    if not isinstance(data, dict):
        stop("Claude 配置文件格式不对（最外层不是 {...}），没有做任何修改。")
    return data


def backup_config(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"claude_desktop_config.backup-{stamp}.json")
    n = 2
    while backup.exists():  # 同一秒内备份两次时，不要覆盖前一个备份
        backup = path.with_name(f"claude_desktop_config.backup-{stamp}-{n}.json")
        n += 1
    shutil.copy2(path, backup)
    return backup


def save_config(path: Path, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    json.loads(text)  # 写之前再确认一次是合法 JSON
    path.write_text(text + "\n", encoding="utf-8")


def add_to_desktop(config_path: Path) -> bool:
    print(f"  配置文件：{config_path}")
    wait_for_claude_closed()
    data = load_config(config_path)  # 先读，格式不对就直接停，不会改坏
    servers = data.setdefault("mcpServers", {})
    if SERVER_NAME in servers and not ask_yes(f"  配置里已有 {SERVER_NAME}，要覆盖吗？"):
        print("  跳过，配置没有改动。")
        return False
    backup = backup_config(config_path)
    print(f"  已备份原配置：{backup.name}")
    servers[SERVER_NAME] = {"command": str(VENV_PY), "args": [str(SERVER)]}
    save_config(config_path, data)
    SAVED_CONFIG_PATH.write_text(str(config_path), encoding="utf-8")
    others = [name for name in servers if name != SERVER_NAME]
    print(f"  ✓ 已加入 Claude 桌面版。其他 MCP（{', '.join(others) or '无'}）保持不变。")
    return True


def remove_from_desktop(config_path: Path) -> None:
    print(f"  配置文件：{config_path}")
    wait_for_claude_closed()
    data = load_config(config_path)
    servers = data.get("mcpServers", {})
    if SERVER_NAME not in servers:
        print("  桌面版配置里没有 deepseek-search。")
        return
    backup = backup_config(config_path)
    del servers[SERVER_NAME]
    save_config(config_path, data)
    print(f"  ✓ 已从 Claude 桌面版移除（原配置备份为 {backup.name}）。")


# ================================================================ Claude Code
def claude_cli() -> str | None:
    return shutil.which("claude")


def claude_code_has_server(cli: str) -> bool:
    out = subprocess.run([cli, "mcp", "get", SERVER_NAME], capture_output=True, text=True, errors="ignore")
    return out.returncode == 0


def add_to_claude_code(cli: str) -> bool:
    if claude_code_has_server(cli):
        if not ask_yes(f"  Claude Code 里已有 {SERVER_NAME}，要覆盖吗？"):
            print("  跳过。")
            return False
        subprocess.run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"], capture_output=True)
    # --scope user：对你所有的项目都生效
    cmd = [cli, "mcp", "add", "--scope", "user", SERVER_NAME, "--", str(VENV_PY), str(SERVER)]
    if subprocess.run(cmd).returncode != 0:
        print("  ✗ 加入 Claude Code 失败。可以手动运行：")
        print(f'    claude mcp add --scope user {SERVER_NAME} -- "{VENV_PY}" "{SERVER}"')
        return False
    print("  ✓ 已加入 Claude Code（对所有项目生效）。")
    return True


# ================================================================ 安装
def install(config_arg: str | None) -> None:
    print("=" * 56)
    print(" 安装 DeepSeek 搜索 MCP")
    print("=" * 56)

    if sys.version_info < (3, 11):
        stop(f"需要 Python 3.11 或更新版本，当前是 {sys.version.split()[0]}。")

    # 1. 虚拟环境
    step(1, "创建独立的 Python 虚拟环境 .venv")
    if VENV_PY.exists():
        print("  已存在，跳过。")
    else:
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        print("  完成。")

    # 2. 安装依赖（核对哈希）
    step(2, "安装依赖（逐个核对哈希）")
    pip = [str(VENV_PY), "-m", "pip", "install", "--disable-pip-version-check",
           "--require-hashes", "--only-binary=:all:", "-r", str(HERE / "requirements.txt")]
    if subprocess.run(pip).returncode != 0:
        print("\n  从官方源安装失败（国内网络常见）。")
        if not ask_yes("  改用清华镜像重试吗？（哈希照样核对，镜像改不了包的内容）"):
            stop("已取消。")
        if subprocess.run(pip + ["-i", MIRROR]).returncode != 0:
            stop("依赖安装失败。请把上面的报错信息复制下来排查。")
    print("  完成。")

    # 3. API Key
    step(3, "设置 DeepSeek API Key")
    if KEY_FILE.exists() and not ask_yes("  已经有 deepseek.key 了，要换一个新 Key 吗？", default=False):
        print("  沿用现有 Key。")
    else:
        print("  到 https://platform.deepseek.com 的「API keys」页面创建一个 Key。")
        print("  粘贴时屏幕上不会显示任何字符，这是正常的，粘贴后直接按回车。")
        while True:
            key = getpass.getpass("  API Key: ").strip().strip("\"'")
            if key.startswith("sk-") and len(key) > 20:
                break
            print("  格式看起来不对（应该以 sk- 开头），请重新粘贴。")
        KEY_FILE.write_text(key, encoding="utf-8")
        if not IS_WINDOWS:
            KEY_FILE.chmod(0o600)  # macOS / Linux：只有你自己能读这个文件
        print(f"  已保存：{key[:5]}…{key[-4:]}")

    # 4. 自检
    step(4, "自检（可选）")
    if ask_yes("  真实搜索一次确认能用？（计费 1 次，约 0.005 元）"):
        if subprocess.run([str(VENV_PY), str(SERVER), "--selftest"]).returncode != 0:
            stop("自检没通过，Claude 配置没有改动。按上面的提示处理后，再运行一次安装脚本。")

    # 5. 加入 Claude
    step(5, "加入 Claude")
    added = []

    config_path = find_claude_config(config_arg)
    if config_path:
        print("  · 发现 Claude 桌面版")
        if ask_yes("  加入 Claude 桌面版？") and add_to_desktop(config_path):
            added.append("Claude 桌面版")
    else:
        print("  · 没找到 Claude 桌面版的配置文件（没装桌面版的话可以忽略）")

    cli = claude_cli()
    if cli:
        print("  · 发现 Claude Code")
        if ask_yes("  加入 Claude Code？") and add_to_claude_code(cli):
            added.append("Claude Code")

    print("\n" + "=" * 56)
    if added:
        print(f" ✓ 安装完成，已加入：{'、'.join(added)}")
        print("   重新打开 Claude 后生效。")
        print("   用量记录：本文件夹里的 usage.log")
        print(f"   卸载：运行 {'uninstall.bat' if IS_WINDOWS else 'bash setup.sh --uninstall'}")
    else:
        print(" 依赖和 Key 已就绪，但还没有加入任何 Claude。")
        if not config_path:
            print("\n 如果装了 Claude 桌面版：打开 设置 → 开发者 → 编辑配置，找到 claude_desktop_config.json，")
            if IS_WINDOWS:
                print(" 把它拖到 setup.bat 图标上；或者运行：")
            print(f'   python install.py --config "配置文件的完整路径"')
            print("\n 在这些位置都没找到：")
            for pattern in candidate_config_patterns():
                print(f"   {pattern}")
    print("=" * 56)


# ================================================================ 卸载
def uninstall(config_arg: str | None) -> None:
    config_path = find_claude_config(config_arg)
    if config_path:
        print("Claude 桌面版：")
        remove_from_desktop(config_path)
    cli = claude_cli()
    if cli and claude_code_has_server(cli):
        subprocess.run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"], capture_output=True)
        print("Claude Code：\n  ✓ 已移除。")
    if not config_path and not cli:
        print("没有找到任何 Claude 配置。")
    print("\n本文件夹没有删除。不再需要的话可以手动删掉整个文件夹。")


if __name__ == "__main__":
    args = sys.argv[1:]
    config_arg = args[args.index("--config") + 1] if "--config" in args else None
    # 把配置文件拖到 setup.bat 上时，文件路径会作为参数传进来
    dropped = [a for a in args if a.lower().endswith(".json")]
    if config_arg is None and dropped:
        config_arg = dropped[0]
    try:
        if "--uninstall" in args:
            uninstall(config_arg)
        else:
            install(config_arg)
    except KeyboardInterrupt:
        stop("已取消。")
