"""微信推送（PushPlus）。原则：推送绝不能阻塞或打断交易流程——任何失败只记日志。"""
import os
import queue
import threading
from collections.abc import Callable
from datetime import date

import requests

PUSHPLUS_URL = "https://www.pushplus.plus/send"


class Notifier:
    """token 为空则 enabled=False，所有方法静默返回。

    发送放进单 worker 的后台线程处理（daemon 线程：进程退出不等待它），send() 本身
    只入队就返回，不等网络请求。每天条数上限按本地日期计数，超出后当天只 log 不发。
    """

    def __init__(self, token: str | None, post=None, log: Callable[[str], None] = print,
                 max_per_day: int = 50, today: Callable[[], date] = date.today):
        self.token = token
        self.enabled = bool(token)
        self._post = post or requests.post
        self._log = log
        self._max_per_day = max_per_day
        self._today = today

        self._count_lock = threading.Lock()
        self._sent_day: date | None = None
        self._sent_count = 0
        self._once_day: date | None = None
        self._once_keys: set[str] = set()

        self._queue: queue.Queue = queue.Queue()
        self._pending = 0
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name="pushplus-notifier")
            self._thread.start()

    def send(self, title: str, content: str) -> None:
        if not self.enabled:
            return
        today = self._today()
        with self._count_lock:
            if self._sent_day != today:
                self._sent_day, self._sent_count = today, 0
            if self._sent_count >= self._max_per_day:
                self._log(f"今日推送已达上限（{self._max_per_day}），跳过：{title}")
                return
            self._sent_count += 1
        with self._cv:
            self._pending += 1
        self._queue.put((title, content))

    def once(self, key: str, title: str, content: str) -> None:
        if not self.enabled:
            return
        today = self._today()
        with self._count_lock:
            if self._once_day != today:
                self._once_day, self._once_keys = today, set()
            if key in self._once_keys:
                return
            self._once_keys.add(key)
        self.send(title, content)

    def flush(self, timeout: float = 5.0) -> None:
        """测试专用：等后台线程把队列里的消息都发完（或超时）。"""
        with self._cv:
            self._cv.wait_for(lambda: self._pending == 0, timeout=timeout)

    def _worker(self) -> None:
        while True:
            title, content = self._queue.get()
            try:
                self._do_send(title, content)
            finally:
                with self._cv:
                    self._pending -= 1
                    self._cv.notify_all()

    def _do_send(self, title: str, content: str) -> None:
        try:
            resp = self._post(PUSHPLUS_URL, json={"token": self.token, "title": title,
                                                    "content": content, "template": "txt"},
                              timeout=5)
        except Exception as e:
            self._log(f"推送失败：{e!r}")
            return
        try:
            if resp.status_code != 200:
                self._log(f"推送失败：HTTP {resp.status_code}")
                return
            data = resp.json()
            if data.get("code") != 200:
                self._log(f"推送失败：接口返回 {data}")
        except Exception as e:
            self._log(f"推送响应解析失败：{e!r}")


def _windows_env_fallback(name: str) -> str | None:
    """后台进程可能在 setx 落盘（写入 HKCU\\Environment）之前就已经启动，
    这时 os.environ 里还没有这个变量，退而读注册表；任何失败都忽略。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value or None
    except Exception:
        return None


def notifier_from_env(env=os.environ, **kw) -> Notifier:
    token = env.get("PUSHPLUS_TOKEN") or _windows_env_fallback("PUSHPLUS_TOKEN")
    return Notifier(token, **kw)
