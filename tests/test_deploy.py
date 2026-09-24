from datetime import date
from pathlib import Path

from jevq.sync.deploy import sync_and_deploy

TODAY = date(2026, 9, 23)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, content):
        self.sent.append((title, content))


def make_run_git(*, status="", staged_names="x\n", full_diff="", rebase_ok=True, push_ok=True,
                 fetch_ok=True, rev_parse="new222", changed_files="", commit_ok=True,
                 worktree_add_ok=True, worktree_remove_ok=True):
    """假 git runner：按子命令分派固定响应，记录所有调用方便断言（尤其是「绝不出现破坏性
    操作」这一条）。"""
    calls = []

    def run_git(args):
        calls.append(list(args))
        cmd = args[0]
        if cmd == "add":
            return FakeResult(0)
        if cmd == "status":
            return FakeResult(0, status)
        if cmd == "diff" and len(args) == 2 and args[1] == "--cached":
            return FakeResult(0, full_diff)
        if cmd == "diff" and "--name-only" in args and "--cached" in args:
            return FakeResult(0, staged_names)
        if cmd == "diff" and args[1] == "--name-only":
            return FakeResult(0, changed_files)
        if cmd == "commit":
            return FakeResult(0 if commit_ok else 1, stderr="" if commit_ok else "commit failed")
        if cmd == "reset":
            return FakeResult(0)
        if cmd == "fetch":
            return FakeResult(0 if fetch_ok else 1, stderr="" if fetch_ok else "fetch failed")
        if cmd == "rebase" and "--abort" not in args:
            return FakeResult(0 if rebase_ok else 1, stderr="" if rebase_ok else "conflict")
        if cmd == "rebase":
            return FakeResult(0)
        if cmd == "push":
            return FakeResult(0 if push_ok else 1, stderr="" if push_ok else "push failed")
        if cmd == "rev-parse":
            return FakeResult(0, rev_parse + "\n")
        if cmd == "worktree" and args[1] == "add":
            return FakeResult(0 if worktree_add_ok else 1, stderr="" if worktree_add_ok else "wt failed")
        if cmd == "worktree":
            return FakeResult(0 if worktree_remove_ok else 1)
        return FakeResult(0)

    run_git.calls = calls
    return run_git


def _never_destructive(calls):
    for c in calls:
        joined = " ".join(str(x) for x in c)
        assert "--hard" not in joined, c
        assert not (c[0] == "clean"), c
        assert not (c[0] == "push" and ("--force" in c or "-f" in c)), c


def test_no_code_changes_only_pushes_data(tmp_path):
    run_git = make_run_git(rev_parse="abc1234")
    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             "abc1234", run_git=run_git, run_tests=lambda *a: (True, "ok"))
    assert result == {"deploy": False}
    assert ["push", "origin", "HEAD:master"] in run_git.calls
    assert not any(c[0] == "worktree" for c in run_git.calls)   # 无代码变化不需要隔离测试
    _never_destructive(run_git.calls)


def test_code_change_and_tests_pass_deploys(tmp_path):
    run_git = make_run_git(rev_parse="new222", changed_files="jevq/engine.py\n")
    tests_called = []

    def run_tests(python_exe, cwd, install_requirements, log):
        tests_called.append((python_exe, cwd, install_requirements))
        return True, "5 passed"

    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             "old111", run_git=run_git, run_tests=run_tests)
    assert result == {"deploy": True, "commit": "new222"}
    assert len(tests_called) == 1
    assert tests_called[0][2] is False                          # requirements.txt 没变
    assert any(c[0] == "worktree" and c[1] == "add" for c in run_git.calls)
    assert any(c[0] == "worktree" and c[1] == "remove" for c in run_git.calls)
    _never_destructive(run_git.calls)


def test_tests_fail_deploy_false_and_notifies(tmp_path):
    run_git = make_run_git(rev_parse="new222", changed_files="jevq/engine.py\n")
    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             "old111", run_git=run_git,
                             run_tests=lambda *a: (False, "1 failed, 4 passed"))
    assert result == {"deploy": False}
    assert any("1 failed" in content for _, content in notifier.sent)
    assert any(c[0] == "worktree" and c[1] == "remove" for c in run_git.calls)  # 失败也要清理
    _never_destructive(run_git.calls)


def test_requirements_change_triggers_install(tmp_path):
    run_git = make_run_git(rev_parse="new222", changed_files="requirements.txt\njevq/engine.py\n")
    seen = []

    def run_tests(python_exe, cwd, install_requirements, log):
        seen.append(install_requirements)
        return True, "ok"

    sync_and_deploy(tmp_path, TODAY, lambda m: None, FakeNotifier(), Path("python"),
                    "old111", run_git=run_git, run_tests=run_tests)
    assert seen == [True]


def test_rebase_conflict_aborts_and_does_not_push(tmp_path):
    run_git = make_run_git(rebase_ok=False)
    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             "abc1234", run_git=run_git, run_tests=lambda *a: (True, "ok"))
    assert result == {"deploy": False}
    assert not any(c[0] == "push" for c in run_git.calls)
    assert any(c == ["rebase", "--abort"] for c in run_git.calls)
    assert any("冲突" in content for _, content in notifier.sent)
    _never_destructive(run_git.calls)


def test_local_uncommitted_code_changes_skip_deploy_but_pushes_data(tmp_path):
    # status 显示 jevq/engine.py 是脏的（代码改动），staged_names 显示 reports/ 下有暂存改动
    run_git = make_run_git(status="M jevq/engine.py\nM reports/2026-09-23.md\n",
                           staged_names="reports/2026-09-23.md\n")
    tests_called = []
    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             "abc1234", run_git=run_git,
                             run_tests=lambda *a: tests_called.append(1) or (True, "ok"))
    assert result == {"deploy": False}
    assert any(c[0] == "commit" for c in run_git.calls)          # 数据照常提交
    assert any(c[0] == "push" for c in run_git.calls)            # 数据照常推送
    assert not tests_called                                      # 但不进入测试/上线判断
    assert any("本机有未提交的代码改动" in m for m in log)
    _never_destructive(run_git.calls)


def test_secret_hit_aborts_commit(tmp_path):
    run_git = make_run_git(staged_names="reports/2026-09-23.md\n",
                           full_diff="+token = '0123456789abcdef0123456789abcdef'\n")
    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             "abc1234", run_git=run_git, run_tests=lambda *a: (True, "ok"))
    assert result == {"deploy": False}
    assert not any(c[0] == "commit" for c in run_git.calls)
    assert not any(c[0] == "push" for c in run_git.calls)
    assert ["reset"] in run_git.calls                            # 撤销暂存，不是 reset --hard
    assert any("密钥" in content for _, content in notifier.sent)
    _never_destructive(run_git.calls)


def test_any_exception_returns_deploy_false(tmp_path):
    def boom(args):
        raise OSError("git 不见了")

    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             "abc1234", run_git=boom, run_tests=lambda *a: (True, "ok"))
    assert result == {"deploy": False}
    assert any("异常" in m for m in log)


def test_running_commit_none_always_checks_and_tests(tmp_path):
    run_git = make_run_git(rev_parse="abc999")
    tests_called = []
    log, notifier = [], FakeNotifier()
    result = sync_and_deploy(tmp_path, TODAY, log.append, notifier, Path("python"),
                             None, run_git=run_git,
                             run_tests=lambda *a: tests_called.append(1) or (True, "ok"))
    assert tests_called == [1]
    assert result == {"deploy": True, "commit": "abc999"}


def test_never_calls_reset_hard_clean_or_force_push(tmp_path):
    """把几种场景（正常上线、rebase 冲突、密钥命中、代码脏）都跑一遍，统一断言从不出现
    reset --hard / clean / push --force。"""
    scenarios = [
        make_run_git(rev_parse="new222", changed_files="jevq/engine.py\n"),
        make_run_git(rebase_ok=False),
        make_run_git(staged_names="reports/x.md\n", full_diff="+sk-abcdefghijklmnopqrstuvwx\n"),
        make_run_git(status="M jevq/engine.py\n"),
    ]
    for run_git in scenarios:
        sync_and_deploy(tmp_path, TODAY, lambda m: None, FakeNotifier(), Path("python"),
                        "old111", run_git=run_git, run_tests=lambda *a: (True, "ok"))
        _never_destructive(run_git.calls)
