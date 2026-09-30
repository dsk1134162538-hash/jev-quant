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
CODE_PATHS = ["jevq/", "run.py", "daemon.py", "desktop.py", "live.py", "requirements.txt", "tests/"]

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


def _rebase_and_push(run_git, log, notifier) -> bool:
    """把 origin/master rebase 到本地分支上并推送。失败（冲突或 push 出错）时已经记过日志、
    发过提醒，调用方只需要据此决定要不要继续。"""
    rebase = run_git(["rebase", "origin/master"])
    if rebase.returncode != 0:
        run_git(["rebase", "--abort"])
        log(f"rebase 冲突：{rebase.stderr}")
        notifier.send("⚠️ 模拟盘提醒", "同步冲突，需要人工处理")
        return False

    push = run_git(["push", "origin", "HEAD:master"])
    if push.returncode != 0:
        log(f"git push 失败：{push.stderr}")
        notifier.send("⚠️ 模拟盘提醒", "同步失败：git push 出错")
        return False

    return True


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
    # 逐个只 add 磁盘上存在的路径：DATA_PATHS 混在一起 add 时，只要有一个路径不存在
    # （如 .superpowers 目录还没建），整条 add 命令都会失败，其余路径也进不了暂存区。
    existing_data_paths = [p for p in DATA_PATHS if (repo / p).exists()]
    if existing_data_paths:
        add = run_git(["add", *existing_data_paths])
        if add.returncode != 0:
            log(f"git add 失败：{add.stderr}")

    code_dirty = _has_uncommitted_code_changes(run_git)
    if code_dirty:
        log("本机有未提交的代码改动，跳过自动上线（数据仍会正常推送）")

    staged = run_git(["diff", "--cached", "--name-only", "--", *DATA_PATHS])
    staged_files = [p for p in (getattr(staged, "stdout", "") or "").splitlines() if p.strip()]
    if staged_files:
        hits = scan_staged(run_git)
        if hits:
            log("密钥扫描命中，中止提交：" + "；".join(hits))
            notifier.send("⚠️ 模拟盘提醒", "同步中止：暂存区检测到疑似密钥 " + "；".join(hits))
            run_git(["reset", "--", *DATA_PATHS])
            return {"deploy": False}
        commit_msg = f"chore: daily sync {today.isoformat()}\n\n{_COMMIT_TRAILER}"
        # 提交暂存区里实际存在的文件，而不是原始的 DATA_PATHS：像 .superpowers 这种目录下
        # 没有已跟踪/已暂存内容时，拿它当 commit 的 pathspec 会报 "did not match any
        # file(s) known to git" 而失败，导致数据一直卡在暂存区、永远推不出去。
        commit = run_git(["commit", "-m", commit_msg, "--", *staged_files])
        if commit.returncode != 0:
            log(f"git commit 失败：{commit.stderr}")
            return {"deploy": False}

    # 2. fetch
    fetch = run_git(["fetch", "origin", "master"])
    if fetch.returncode != 0:
        log(f"git fetch 失败：{fetch.stderr}")
        notifier.send("⚠️ 模拟盘提醒", "同步失败：git fetch 出错")
        return {"deploy": False}

    # 3. 本机代码是脏的：不测试、不上线，数据照旧 rebase 到 origin/master 之上推送
    if code_dirty:
        _rebase_and_push(run_git, log, notifier)
        return {"deploy": False}

    # 4. 判断 origin/master 相对正在运行的版本，代码有没有变化——这里用 origin/master 而不是
    #    本地 HEAD，因为还没 rebase，磁盘上跑的仍是 running_commit
    changed = None  # None = 不知道有没有变化（读不到运行中的提交，或 git diff 失败）→ 总要先测试
    if running_commit is not None:
        diff = run_git(["diff", "--name-only", running_commit, "origin/master", "--", *CODE_PATHS])
        if diff.returncode == 0:
            changed = (diff.stdout or "").splitlines()
        else:
            log(f"比较代码变化失败，按有变化处理：{diff.stderr}")

    if changed is not None and not changed:
        # 代码没变：不用测试，直接把 origin/master 合过来推送
        _rebase_and_push(run_git, log, notifier)
        return {"deploy": False}

    # 5. 代码有变化（或没有 running_commit 可比较）：先在临时 worktree 里测 origin/master，
    #    测试通过才允许 rebase + push；不通过就保持磁盘上的旧代码继续跑，今天的数据也不推
    worktree_dir = Path(tempfile.mkdtemp(prefix="jevq_deploy_"))
    install_requirements = bool(changed) and "requirements.txt" in changed
    try:
        wt = run_git(["worktree", "add", "--detach", str(worktree_dir), "origin/master"])
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
        notifier.send("⚠️ 模拟盘提醒",
                      f"新代码测试未通过（{failed_n} failed），继续使用旧版本 {old}，今天的数据没推送")
        return {"deploy": False}

    # 6. 测试通过，这才 rebase + push，磁盘上换成测过的新代码
    if not _rebase_and_push(run_git, log, notifier):
        return {"deploy": False}

    head = run_git(["rev-parse", "HEAD"])
    new_head = (getattr(head, "stdout", "") or "").strip()
    if head.returncode != 0 or not new_head:
        log(f"读取 HEAD 失败：{getattr(head, 'stderr', '')}")
        return {"deploy": False}

    return {"deploy": True, "commit": new_head}
