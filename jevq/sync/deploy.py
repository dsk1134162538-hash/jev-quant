"""每日同步 + 收盘后自动上线（gated by tests）。

原则：交易安全第一——本模块任何失败都不能影响交易；不做任何破坏性 git 操作（不出现
reset --hard / clean / force push）。所有 git 与测试执行都通过可注入的 run_git / run_tests
完成，方便测试用假 runner，不触网、不动真实仓库。
"""
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from jevq.sync.secrets import scan_staged

# 收盘后判断"是否需要上线"时比较的代码路径。
CODE_PATHS = ["jevq/", "run.py", "daemon.py", "desktop.py", "requirements.txt", "tests/"]

# git add 只加这些路径（数据/账本），绝不带上代码改动。
DATA_PATHS = ["reports", "exports", ".superpowers", ".gitignore"]

# git status --porcelain 里，以下前缀（或整个路径等于 .gitignore）视为"数据"改动，
# 其余的都算"代码"改动。
_ALLOWED_DIRTY_PREFIXES = ("reports/", "exports/", ".superpowers/")

_COMMIT_TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def _real_run_git(args: list[str], cwd: Path):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def _real_run_tests(python_exe: Path, cwd: Path, install_requirements: bool, log) -> tuple[bool, str]:
    if install_requirements:
        log("requirements.txt 有变化，先在隔离目录安装依赖")
        r = subprocess.run([str(python_exe), "-m", "pip", "install", "-r", "requirements.txt"],
                           cwd=cwd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return False, ("依赖安装失败：" + r.stdout + r.stderr)[-4000:]
    r = subprocess.run([str(python_exe), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                       cwd=cwd, capture_output=True, text=True, timeout=900)
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


def _count_failed(detail: str) -> int:
    m = re.search(r"(\d+) failed", detail)
    return int(m.group(1)) if m else 0


def _dirty_paths(status_stdout: str) -> list[str]:
    paths = []
    for line in status_stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().split(" -> ")[-1].strip('"')
        paths.append(path)
    return paths


def _has_uncommitted_code_changes(run_git) -> bool:
    """git add 之后再看 status：还剩下的（未staged的数据路径也算已处理过，这里只关心
    是否有数据路径之外的改动）就是"代码文件"改动，不应该被自动提交/上线。"""
    status = run_git(["status", "--porcelain"])
    for path in _dirty_paths(getattr(status, "stdout", "") or ""):
        if path == ".gitignore":
            continue
        if path.startswith(_ALLOWED_DIRTY_PREFIXES):
            continue
        return True
    return False


def sync_and_deploy(repo: Path, today: date, log, notifier, python_exe: Path,
                    running_commit: str | None, run_git=None, run_tests=None) -> dict:
    """跑完一个交易日（收盘报告已写完）后调用。返回 {"deploy": bool, "commit"?: str}。

    全程异常兜底：任何一步意外抛出，只记日志 + 微信提醒，返回 {"deploy": False}，绝不
    让同步/上线的问题影响交易或下一交易日的运行。
    """
    run_git = run_git or (lambda args: _real_run_git(args, repo))
    run_tests = run_tests or _real_run_tests
    try:
        return _sync_and_deploy(repo, today, log, notifier, python_exe, running_commit,
                                run_git, run_tests)
    except Exception as e:
        log(f"同步/上线出现异常：{e!r}")
        try:
            notifier.send("⚠️ 模拟盘提醒", f"同步/上线出现异常：{e!r}")
        except Exception:
            pass
        return {"deploy": False}


def _sync_and_deploy(repo: Path, today: date, log, notifier, python_exe: Path,
                     running_commit: str | None, run_git, run_tests) -> dict:
    # 1. 提交本机数据（只加数据路径，绝不带代码）
    add = run_git(["add", *DATA_PATHS])
    if add.returncode != 0:
        log(f"git add 失败：{add.stderr}")

    code_dirty = _has_uncommitted_code_changes(run_git)
    if code_dirty:
        log("本机有未提交的代码改动，跳过自动上线（数据仍会正常推送）")

    staged = run_git(["diff", "--cached", "--name-only"])
    if (getattr(staged, "stdout", "") or "").strip():
        hits = scan_staged(run_git)
        if hits:
            log("密钥扫描命中，中止提交：" + "；".join(hits))
            notifier.send("⚠️ 模拟盘提醒", "同步中止：暂存区检测到疑似密钥 " + "；".join(hits))
            run_git(["reset"])
            return {"deploy": False}
        commit_msg = f"chore: daily sync {today.isoformat()}\n\n{_COMMIT_TRAILER}"
        commit = run_git(["commit", "-m", commit_msg])
        if commit.returncode != 0:
            log(f"git commit 失败：{commit.stderr}")
            return {"deploy": False}

    # 2. fetch
    fetch = run_git(["fetch", "origin", "master"])
    if fetch.returncode != 0:
        log(f"git fetch 失败：{fetch.stderr}")
        notifier.send("⚠️ 模拟盘提醒", "同步失败：git fetch 出错")
        return {"deploy": False}

    # 3. 合并远端
    rebase = run_git(["rebase", "origin/master"])
    if rebase.returncode != 0:
        run_git(["rebase", "--abort"])
        log(f"rebase 冲突：{rebase.stderr}")
        notifier.send("⚠️ 模拟盘提醒", "同步冲突，需要人工处理")
        return {"deploy": False}

    # 4. push（失败只提醒，不算致命）
    push = run_git(["push", "origin", "HEAD:master"])
    if push.returncode != 0:
        log(f"git push 失败：{push.stderr}")
        notifier.send("⚠️ 模拟盘提醒", "同步失败：git push 出错")
        return {"deploy": False}

    if code_dirty:
        return {"deploy": False}

    head = run_git(["rev-parse", "HEAD"])
    new_head = (getattr(head, "stdout", "") or "").strip()
    if head.returncode != 0 or not new_head:
        log(f"读取 HEAD 失败：{getattr(head, 'stderr', '')}")
        return {"deploy": False}

    changed: list[str] | None = None
    if running_commit is not None and running_commit != new_head:
        diff = run_git(["diff", "--name-only", running_commit, new_head, "--", *CODE_PATHS])
        changed = (diff.stdout or "").splitlines() if diff.returncode == 0 else []
    elif running_commit == new_head:
        return {"deploy": False}
    # running_commit is None → 视为总是需要检查（daemon 启动时读不到当前运行的提交）

    if running_commit is not None and not changed:
        return {"deploy": False}

    # 6. 隔离测试：临时 worktree，跑完立即清理
    worktree_dir = Path(tempfile.mkdtemp(prefix="jevq_deploy_"))
    install_requirements = bool(changed) and "requirements.txt" in changed
    try:
        wt = run_git(["worktree", "add", "--detach", str(worktree_dir), "HEAD"])
        if wt.returncode != 0:
            log(f"创建隔离测试 worktree 失败：{wt.stderr}")
            notifier.send("⚠️ 模拟盘提醒", "同步失败：创建测试环境出错")
            return {"deploy": False}
        passed, detail = run_tests(python_exe, worktree_dir, install_requirements, log)
    finally:
        rm = run_git(["worktree", "remove", "--force", str(worktree_dir)])
        if rm.returncode != 0:
            log(f"清理隔离测试 worktree 失败：{rm.stderr}")

    if not passed:
        failed_n = _count_failed(detail)
        log(f"新代码测试未通过（{failed_n} failed）：{detail}")
        old = running_commit[:7] if running_commit else "?"
        notifier.send("⚠️ 模拟盘提醒", f"新代码测试未通过（{failed_n} failed），继续使用旧版本 {old}")
        return {"deploy": False}

    return {"deploy": True, "commit": new_head}
