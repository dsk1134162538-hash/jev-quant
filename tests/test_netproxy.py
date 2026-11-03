"""国内网站直连（2026-11-03）：本机配了系统代理（127.0.0.1:8889，v2ray），代理上游间歇性抖动，pushplus、东财、同花顺、
新浪的请求被绕道代理后一抖就 ProxyError，微信推送丢了，取行情也变慢。国内网站本来就不需要代理，进程启动时统一加进 NO_PROXY。"""
import os

from jevq import netproxy


def test_domestic_hosts_are_added_to_no_proxy_without_dropping_existing_entries():
    merged = netproxy.merge_no_proxy("localhost,127.0.0.1,example.org")
    parts = merged.split(",")
    assert parts[:3] == ["localhost", "127.0.0.1", "example.org"]                  # 原有的不丢、顺序不变
    for host in ("pushplus.plus", "eastmoney.com", "10jqka.com.cn", "sina.com.cn", "gtimg.cn", "tencent.com"):
        assert host in parts
    assert len(parts) == len(set(parts))                                          # 不重复
    assert netproxy.merge_no_proxy(merged) == merged                              # 幂等


def test_empty_no_proxy_still_gets_the_domestic_hosts_and_loopback():
    parts = netproxy.merge_no_proxy("").split(",")
    assert "pushplus.plus" in parts and "localhost" in parts and "127.0.0.1" in parts


def test_import_jevq_sets_both_cases_of_the_environment_variable():
    import jevq  # noqa: F401
    for key in ("NO_PROXY", "no_proxy"):
        assert "pushplus.plus" in os.environ[key].split(",")
