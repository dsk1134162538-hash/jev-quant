"""通过本机 claude 命令行（Claude Code 的非交互模式）问 Claude，用 dj 的 claude.ai 订阅登录，不需要 API key。

和 Vibe-Research 接订阅的做法一样：claude.exe -p，问题走标准输入，关掉所有工具、不加载本机插件和配置，
只拿它的文字回答。三处要注意（10-10 接 Vibe 时踩过）：
- npm 版 claude 现在是原生 exe 外壳，直接调真正的 claude.exe；
- 命令行 claude 不读 Windows 系统代理（VPN），要显式给 HTTPS_PROXY；
- 外壳里遗留的 ANTHROPIC_API_KEY 之类会让它改走按量计费，调用前删掉。
任何失败（没装、超时、报错、回答里没有 JSON）都返回 None，调用方按「没答上」处理。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from jevq import config

_DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
_OBJ = re.compile(r"\{.*\}", re.S)


class ClaudeCLI:
    def __init__(self, exe: str | None = None, model: str = config.CLAUDE_MODEL, proxy: str | None = config.CLAUDE_PROXY,
                 timeout: float = config.CLAUDE_TIMEOUT, run=subprocess.run):
        self.exe = exe or os.path.expandvars(config.CLAUDE_BIN)
        self.model, self.proxy, self.timeout, self.run = model, proxy, timeout, run
        self.last_trace: dict | None = None

    def available(self) -> bool:
        return Path(self.exe).is_file()

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k.upper() not in _DROP_ENV}
        if self.proxy:
            env.update(HTTPS_PROXY=self.proxy, HTTP_PROXY=self.proxy, NO_PROXY="127.0.0.1,localhost")
        return env

    def ask_json(self, system: str, user: str, timeout: float | None = None) -> dict | None:
        """问一次，回答里的第一个 JSON 对象解析成 dict；失败返回 None。"""
        self.last_trace = None
        if not self.available():
            return None
        args = [self.exe, "-p", "--safe-mode", "--no-chrome", "--disable-slash-commands", "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}', "--tools", "", "--permission-mode", "dontAsk",
                "--no-session-persistence", "--output-format", "json", "--model", self.model,
                "--system-prompt", system]
        kwargs: dict = {"input": user, "capture_output": True, "text": True, "encoding": "utf-8",
                        "timeout": timeout or self.timeout, "env": self._env(), "cwd": tempfile.gettempdir()}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW      # 后台 pythonw 调它不弹黑窗
        try:
            done = self.run(args, **kwargs)
            outer = json.loads(done.stdout)
        except Exception as e:                                         # 超时、没装、输出不是 JSON
            print(f"[claude] 调用失败: {e!r}")
            return None
        result = outer.get("result")
        self.last_trace = {"model": self.model, "duration_ms": outer.get("duration_ms"),
                           "cost_usd": outer.get("total_cost_usd"), "result": str(result)[:500]}
        if outer.get("is_error") or not isinstance(result, str):
            return None
        for text in (result.strip(), *(m.group(0) for m in [_OBJ.search(result)] if m)):
            try:
                got = json.loads(text)
            except ValueError:
                continue
            if isinstance(got, dict):
                return got
        return None
