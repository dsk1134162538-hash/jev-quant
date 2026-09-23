from datetime import date, datetime

from jevq.models import Position, Quote


def _p(pre_close: float, pct: float) -> float:
    return round(pre_close * (1 + pct / 100), 2)


def quote(code="600001", pct=0.0, open_pct=None, low_pct=None, pre_close=10.0,
          name="测试股份", volume=1000, amount=1e7, ts=datetime(2026, 9, 24, 10, 0)):
    price = _p(pre_close, pct)
    op = price if open_pct is None else _p(pre_close, open_pct)
    low = min(price, op) if low_pct is None else _p(pre_close, low_pct)
    return Quote(code=code, name=name, price=price, pre_close=pre_close, open=op,
                 high=max(price, op), low=low, volume=volume, amount=amount,
                 limit_up=_p(pre_close, 10), limit_down=_p(pre_close, -10), ts=ts)


def position(code="600001", account="main", shares=1000, buy_price=10.0,
             buy_date=date(2026, 9, 23)):
    return Position(account=account, code=code, shares=shares,
                    cost=shares * buy_price + 5.1, buy_price=buy_price, buy_date=buy_date)
