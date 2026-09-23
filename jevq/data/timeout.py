from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

# 超时的调用无法被强行中止，线程会在后台跑完；池子有上限，挂死的调用不会无限堆线程。
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ak-timeout")


def call_with_timeout(fn, *args, timeout: float = 20.0, **kwargs):
    future = _POOL.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout:
        future.cancel()
        raise TimeoutError(f"{getattr(fn, '__name__', repr(fn))} 超时 {timeout}s") from None
