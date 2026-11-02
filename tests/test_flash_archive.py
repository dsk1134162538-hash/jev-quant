"""7x24 快讯存档（dj 2026-11-02）：盘中每分钟把财联社 / 东财快讯带时间戳存下来，以后回测「快讯出来几分钟内买题材股」。"""
import sqlite3
from datetime import datetime

from jevq.data.flash_archive import FlashArchiver
from jevq.data.news7x24 import Flash


def test_new_flashes_are_stored_once_with_source_and_timestamp(tmp_path):
    items = [(datetime(2026, 11, 3, 9, 41, 5), "某部门发布机器人产业政策"), (datetime(2026, 11, 3, 9, 40), "旧闻")]
    flash = Flash(sources={"财联社": lambda: items}, log=lambda s: None, clock=iter(range(0, 10000, 100)).__next__)
    db = tmp_path / "m.sqlite"
    arch = FlashArchiver(db)
    assert arch.step(flash, datetime(2026, 11, 3, 9, 42)) == 2
    assert arch.step(flash, datetime(2026, 11, 3, 9, 43)) == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT ts, source, title FROM news_flash ORDER BY ts").fetchall() == [
        ("2026-11-03T09:40:00", "财联社", "旧闻"), ("2026-11-03T09:41:05", "财联社", "某部门发布机器人产业政策")]
    conn.close()
