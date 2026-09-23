from datetime import date, datetime

import requests

from jevq.models import Quote

QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
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
                      ts=datetime.strptime(f[30], "%Y%m%d%H%M%S"))
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
