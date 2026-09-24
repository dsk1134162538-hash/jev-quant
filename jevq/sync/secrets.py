"""提交前密钥扫描：对 `git diff --cached` 的内容做正则匹配，命中就中止提交。

只回显命中片段的前 6 位 + "***"，绝不把完整密钥写进日志或微信推送。
"""
import re

# (正则, 描述)
_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk- 密钥"),
    (re.compile(r"apikey_[0-9a-f]{10,}"), "apikey_ 密钥"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "GitHub token (gho_)"),
    (re.compile(r"ANTHROPIC_(AUTH_TOKEN|API_KEY)\s*[:=]\s*[\"']?[^\"'\s<]{12,}", re.IGNORECASE),
     "ANTHROPIC token"),
    (re.compile(r"token[\"']?\s*[:=]\s*[\"']?[0-9a-f]{32}", re.IGNORECASE), "PushPlus token"),
]


def _redact(snippet: str) -> str:
    return snippet[:6] + "***"


def scan_staged(run_git) -> list[str]:
    """run_git 是形如 `run_git(args: list[str]) -> CompletedProcess` 的可注入 runner（stdout/stderr
    是文本）。返回命中描述列表，不回显完整密钥；没有命中返回空列表。"""
    diff = run_git(["diff", "--cached"])
    text = getattr(diff, "stdout", "") or ""
    hits = []
    for pattern, label in _PATTERNS:
        for m in pattern.finditer(text):
            hits.append(f"{label}：{_redact(m.group(0))}")
    return hits
