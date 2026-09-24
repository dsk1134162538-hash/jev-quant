"""微信推送（PushPlus）。原则：推送绝不能阻塞或打断交易流程——任何失败只记日志。"""
import os
import queue
import threading
from collections.abc import Callable
from datetime import date

import requests

PUSHPLUS_URL = "https://www.pushplus.plus/send"


def _redact_value(value):
    """递归脱敏：任何名字里带 "token"（大小写不敏感）的键，值一律替换成 "***"。
    响应体把请求里的 token 原样回显是最容易泄漏的路径，所以先在键名这一层挡一道，
    再由 Notifier._redact 兜底抹掉字符串里可能残留的真实 token 值。"""
    if isinstance(value, dict):
        return {k: ("***" if "token" in str(k).lower() else _redact_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


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
        if not self._reserve_quota():
            self._log(f"今日推送已达上限（{self._max_per_day}），跳过：{title}")
            return
        self._enqueue(title, content)

    def once(self, key: str, title: str, content: str) -> None:
        """同一 key 当天只发一次。被每日上限挡下的这次不算「已发」——不占用 key，
        当天配额恢复（或调用方提高了上限）后同一 key 还能再发一次。"""
        if not self.enabled:
            return
        today = self._today()
        with self._count_lock:
            if self._once_day != today:
                self._once_day, self._once_keys = today, set()
            if key in self._once_keys:
                return
        if not self._reserve_quota():
            self._log(f"今日推送已达上限（{self._max_per_day}），跳过：{title}")
            return
        with self._count_lock:
            self._once_keys.add(key)
        self._enqueue(title, content)

    def flush(self, timeout: float = 5.0) -> None:
        """测试专用：等后台线程把队列里的消息都发完（或超时）。"""
        with self._cv:
            self._cv.wait_for(lambda: self._pending == 0, timeout=timeout)

    def _reserve_quota(self) -> bool:
        """按本地日期占用一条今日配额；额度用完返回 False（调用方只 log，不发）。"""
        today = self._today()
        with self._count_lock:
            if self._sent_day != today:
                self._sent_day, self._sent_count = today, 0
            if self._sent_count >= self._max_per_day:
                return False
            self._sent_count += 1
            return True

    def _enqueue(self, title: str, content: str) -> None:
        with self._cv:
            self._pending += 1
        self._queue.put((title, content))

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
            self._log(self._redact(f"推送失败：{e!r}"))
            return
        try:
            if resp.status_code != 200:
                self._log(self._redact(f"推送失败：HTTP {resp.status_code}"))
                return
            data = resp.json()
            if data.get("code") != 200:
                self._log(self._redact(f"推送失败：接口返回 {_redact_value(data)}"))
        except Exception as e:
            self._log(self._redact(f"推送响应解析失败：{e!r}"))

    def _redact(self, text: str) -> str:
        """token 绝不进日志：把响应/异常文本里可能出现的真实 token 字符串都抹掉，
        这是最后一道防线（键名层面的脱敏见 _redact_value）。"""
        if self.token:
            text = text.replace(self.token, "***")
        return text


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
