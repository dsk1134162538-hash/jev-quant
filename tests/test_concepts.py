import threading
import time
from datetime import date, datetime, timedelta

from jevq import db
from jevq.data.concepts import load_concepts, needs_refresh, refresh_concepts

TODAY = date(2026, 9, 28)


def test_refresh_replaces_table_and_loads():
    conn = db.connect(":memory:")
    n = refresh_concepts(conn, TODAY, fetch_list=lambda: [("gn_a", "机器人"), ("gn_b", "华为汽车")],
                         fetch_members=lambda label: {"gn_a": ["600001", "600002"], "gn_b": ["600001"]}[label],
                         pause=0)
    assert n == 3
    assert load_concepts(conn) == {"600001": ["华为汽车", "机器人"], "600002": ["机器人"]}
    assert not needs_refresh(conn, TODAY)
    assert needs_refresh(conn, date(2026, 10, 5))


def test_refresh_keeps_old_data_when_most_boards_fail():
    conn = db.connect(":memory:")
    refresh_concepts(conn, TODAY, fetch_list=lambda: [("gn_a", "机器人")], fetch_members=lambda l: ["600001"], pause=0)

    def boom(label):
        raise RuntimeError("blocked")
    n = refresh_concepts(conn, TODAY, fetch_list=lambda: [("gn_a", "机器人"), ("gn_b", "芯片")], fetch_members=boom,
                         pause=0, max_consecutive_failures=10)
    assert n == 0
    assert load_concepts(conn) == {"600001": ["机器人"]}


def test_refresh_stops_after_consecutive_failures():
    conn = db.connect(":memory:")
    calls = []

    def boom(label):
        calls.append(label)
        raise RuntimeError("x")
    refresh_concepts(conn, TODAY, fetch_list=lambda: [(f"gn_{i}", f"c{i}") for i in range(20)], fetch_members=boom,
                     pause=0, max_consecutive_failures=3)
    assert len(calls) == 3


def test_list_failure_returns_zero():
    conn = db.connect(":memory:")

    def boom():
        raise RuntimeError("x")
    assert refresh_concepts(conn, TODAY, fetch_list=boom, pause=0) == 0
    assert needs_refresh(conn, TODAY)


def test_deadline_already_passed_stops_before_any_member_fetch():
    conn = db.connect(":memory:")
    calls = []
    n = refresh_concepts(conn, TODAY, fetch_list=lambda: [(f"gn_{i}", f"c{i}") for i in range(5)],
                         fetch_members=lambda label: calls.append(label) or ["600001"],
                         pause=0, deadline=datetime.now() - timedelta(seconds=1))
    assert calls == []
    assert n == 0
    assert needs_refresh(conn, TODAY)   # 保留旧数据（这里是没数据）


def test_stop_event_set_stops_before_any_member_fetch():
    conn = db.connect(":memory:")
    calls = []
    stop = threading.Event()
    stop.set()
    n = refresh_concepts(conn, TODAY, fetch_list=lambda: [(f"gn_{i}", f"c{i}") for i in range(5)],
                         fetch_members=lambda label: calls.append(label) or ["600001"],
                         pause=0, stop=stop)
    assert calls == []
    assert n == 0


def test_deadline_stops_remaining_boards_but_keeps_progress_so_far():
    conn = db.connect(":memory:")
    deadline = datetime.now() + timedelta(seconds=0.05)

    def members(label):
        time.sleep(0.08)   # 保证拉完第一块板时已经过了 deadline
        return ["600001"]

    n = refresh_concepts(conn, TODAY, fetch_list=lambda: [("gn_0", "板块0"), ("gn_1", "板块1")],
                         fetch_members=members, pause=0, deadline=deadline)
    # 只拉到 1/2，刚好够一半，照常写库；第二块板因为到点被跳过，从没调用过
    assert n == 1
    assert load_concepts(conn) == {"600001": ["板块0"]}
