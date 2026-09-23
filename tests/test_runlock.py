import pytest

from jevq.runlock import hold_run_lock, is_held


def test_second_holder_fails_until_released(tmp_path):
    path = tmp_path / "sub" / "runner.lock"
    with hold_run_lock(path):
        with pytest.raises(RuntimeError, match="已有另一个交易进程在运行"):
            with hold_run_lock(path):
                pass
    with hold_run_lock(path):                  # 释放后可再次获取
        pass


def test_is_held_false_for_missing_file(tmp_path):
    assert is_held(tmp_path / "no-such.lock") is False


def test_is_held_true_while_locked_then_false_after(tmp_path):
    path = tmp_path / "runner.lock"
    with hold_run_lock(path):
        assert is_held(path) is True
    assert is_held(path) is False
