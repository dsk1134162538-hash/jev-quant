import pytest

from jevq.runlock import hold_run_lock


def test_second_holder_fails_until_released(tmp_path):
    path = tmp_path / "sub" / "runner.lock"
    with hold_run_lock(path):
        with pytest.raises(RuntimeError, match="已有另一个交易进程在运行"):
            with hold_run_lock(path):
                pass
    with hold_run_lock(path):                  # 释放后可再次获取
        pass
