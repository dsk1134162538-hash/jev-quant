from datetime import date, datetime

import requests

from jevq.models import Quote

QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
DAY_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"   # 最近 5 个交易日的分时
SINA_A50_URL = "https://hq.sinajs.cn/list=hf_CHA50CFD"
OVERSEAS = {"usDJI": "道琼斯", "usIXIC": "纳斯达克", "usINX": "标普500",
            "usHXC": "中概金龙", "hkHSI": "恒生指数"}


def symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _records(text: str):
    for line in text.split(";"):
        line = line.strip()
        if '="' not in line:
            continue
        key, body = line.split('="', 1)
        yield key.replace("v_", "", 1).strip(), body.rstrip('"').split("~")


def _safe_float(f: list[str], i: int) -> float:
    try:
        return float(f[i])
    except (ValueError, IndexError):
        return 0.0


def parse_quotes(text: str) -> dict[str, Quote]:
    out = {}
    for _, f in _records(text):
        if len(f) < 49:
            continue
        try:
            q = Quote(code=f[2], name=f[1], price=float(f[3]), pre_close=float(f[4]),
                      open=float(f[5]), high=float(f[33]), low=float(f[34]),
                      volume=int(float(f[6])), amount=float(f[37]) * 10000,
                      limit_up=float(f[47]), limit_down=float(f[48]),
                      ts=datetime.strptime(f[30], "%Y%m%d%H%M%S"),
                      turnover=_safe_float(f, 38), volume_ratio=_safe_float(f, 49),
                      float_mcap=_safe_float(f, 44), total_mcap=_safe_float(f, 45),
                      pe_ttm=_safe_float(f, 39), pe_dyn=_safe_float(f, 52),
                      pe_static=_safe_float(f, 53), pb=_safe_float(f, 46),
                      amplitude=_safe_float(f, 43), outer_vol=_safe_float(f, 7),
                      inner_vol=_safe_float(f, 8), avg_price=_safe_float(f, 51),
                      ytd_pct=_safe_float(f, 62), float_shares=_safe_float(f, 72),
                      total_shares=_safe_float(f, 73),
                      bid_vols=tuple(int(_safe_float(f, i)) for i in (10, 12, 14, 16, 18)),
                      ask_vols=tuple(int(_safe_float(f, i)) for i in (20, 22, 24, 26, 28)),
                      bid1=_safe_float(f, 9))
        except (ValueError, IndexError):
            continue
        if q.pre_close > 0:
            out[q.code] = q
    return out


def fetch_quotes(codes: list[str], session=None, chunk: int = 500) -> dict[str, Quote]:
    s = session or requests.Session()
    out: dict[str, Quote] = {}
    for i in range(0, len(codes), chunk):
        r = s.get(QT_URL + ",".join(symbol(c) for c in codes[i:i + chunk]), timeout=10)
        r.raise_for_status()
        out.update(parse_quotes(r.content.decode("gbk", errors="ignore")))
    return out


def five_day_return(rows: list[tuple[str, float]], today: date) -> float | None:
    past = [c for d, c in rows if d < today.isoformat()]
    if len(past) < 6 or past[-6] <= 0:
        return None
    return round((past[-1] / past[-6] - 1) * 100, 2)


def fetch_kline(code: str, session=None) -> list[tuple[str, float]]:
    """拉一只股票最近的日 K 线，返回 (date_str, close) 列表。接口目前被 WAF 封锁，见 Task 19。"""
    s = session or requests.Session()
    sym = symbol(code)
    r = s.get(KLINE_URL, params={"param": f"{sym},day,,,10,qfq"}, timeout=10)
    r.raise_for_status()
    data = r.json()["data"][sym]
    rows = data.get("qfqday") or data.get("day") or []
    return [(row[0], float(row[2])) for row in rows]


def parse_minutes(body: dict, sym: str) -> list[tuple[str, float, int]]:
    """分时接口的当日数据 → [("HH:MM", 价格, 累计成交量(手))]；坏行跳过。"""
    rows = (((body.get("data") or {}).get(sym) or {}).get("data") or {}).get("data") or []
    out = []
    for row in rows:
        f = str(row).split()
        try:
            out.append((f"{f[0][:2]}:{f[0][2:4]}", float(f[1]), int(float(f[2]))))
        except (ValueError, IndexError):
            continue
    return out


def fetch_minutes(code: str, timeout: float = 5.0, session=None) -> list[tuple[str, float, int]]:
    """一只股票今天到现在的分时（逐分钟）。只在持仓票要止损/低开反抽时拉一次，不进每分钟的行情批量。"""
    s = session or requests.Session()
    sym = symbol(code)
    r = s.get(MINUTE_URL, params={"code": sym}, timeout=timeout)
    r.raise_for_status()
    return parse_minutes(r.json(), sym)


def parse_day_minutes(body: dict, sym: str) -> dict[str, dict[str, int]]:
    """5 日分时 → {"YYYY-MM-DD": {"HHMM": 累计成交量(手)}}；坏行跳过。"""
    days = ((body.get("data") or {}).get(sym) or {}).get("data") or []
    out: dict[str, dict[str, int]] = {}
    for d in days:
        raw = str(d.get("date") or "")
        if len(raw) != 8:
            continue
        curve = {}
        for row in d.get("data") or []:
            f = str(row).split()
            try:
                curve[f[0]] = int(float(f[2]))
            except (ValueError, IndexError):
                continue
        out[f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"] = curve
    return out


def fetch_day_minutes(code: str, timeout: float = 4.0, session=None) -> dict[str, dict[str, int]]:
    """一只股票最近 5 个交易日（含今天）的逐分钟累计成交量；扫首板算「同时段放量倍数」用。"""
    s = session or requests.Session()
    sym = symbol(code)
    r = s.get(DAY_MINUTE_URL, params={"code": sym}, timeout=timeout)
    r.raise_for_status()
    return parse_day_minutes(r.json(), sym)


def parse_index_pcts(text: str) -> dict[str, float]:
    out = {}
    for key, f in _records(text):
        try:
            out[key] = float(f[32])
        except (ValueError, IndexError):
            continue
    return out


def parse_sina_a50(text: str) -> float:
    f = text.split('"')[1].split(",")
    return round((float(f[0]) / float(f[7]) - 1) * 100, 2)


def fetch_overseas(session=None) -> dict[str, float]:
    s = session or requests.Session()
    out = {}
    r = s.get(QT_URL + ",".join(OVERSEAS), timeout=10)
    for key, pct in parse_index_pcts(r.content.decode("gbk", errors="ignore")).items():
        out[OVERSEAS.get(key, key)] = pct
    try:
        r = s.get(SINA_A50_URL, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        out["富时A50期货"] = parse_sina_a50(r.text)
    except Exception as e:
        print(f"[overseas] 富时A50 获取失败: {e!r}")
    return out
