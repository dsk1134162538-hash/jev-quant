"""国内网站直连（2026-11-03）。

本机配了系统代理（127.0.0.1:8889，v2ray），HTTP_PROXY / HTTPS_PROXY 环境变量也指向它，而代理上游间歇性抖动：
pushplus、东方财富、同花顺、新浪、腾讯行情、WorkBuddy 的请求被绕道代理，一抖就 ProxyError（「推送失败（试了 3 次）」），
取行情变慢，AI 复盘整个下午一条推送都没有。这些都是国内站点，本来就不需要代理，进程启动时统一加进 NO_PROXY
（requests 和 node 都认；子进程继承环境变量）。已有的 NO_PROXY 条目原样保留。
"""
import os

DOMESTIC_HOSTS = ("localhost", "127.0.0.1", "pushplus.plus", "eastmoney.com", "10jqka.com.cn", "sina.com.cn", "sinajs.cn",
                  "gtimg.cn", "qq.com", "tencent.com", "tencentbuddy.com", "codebuddy.cn", "xueqiu.com", "cls.cn")


def merge_no_proxy(existing: str) -> str:
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    for host in DOMESTIC_HOSTS:
        if host not in parts:
            parts.append(host)
    return ",".join(parts)


def apply() -> None:
    merged = merge_no_proxy(os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "")
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = merged
