from jevq.sync.secrets import scan_staged


class FakeResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def fake_git(diff_text):
    def run_git(args):
        assert args == ["diff", "--cached"]
        return FakeResult(diff_text)
    return run_git


def test_no_hits_on_clean_diff():
    assert scan_staged(fake_git("+def foo():\n+    return 1\n")) == []


def test_detects_sk_key():
    hits = scan_staged(fake_git("+API_KEY = 'sk-abcdefghijklmnopqrstuvwx'\n"))
    assert len(hits) == 1
    assert "sk-abc***" in hits[0]
    assert "opqrstuvwx" not in hits[0]          # 不回显完整密钥


def test_detects_apikey_prefixed_token():
    hits = scan_staged(fake_git("+x = apikey_0123456789abcdef\n"))
    assert any("apikey_" in h for h in hits)


def test_detects_github_token():
    hits = scan_staged(fake_git("+GH_TOKEN=gho_abcdefghijklmnopqrstuv\n"))
    assert any("gho_" in h for h in hits)


def test_detects_anthropic_token():
    hits = scan_staged(fake_git('+ANTHROPIC_API_KEY = "sk-ant-abcdefghijklmnop"\n'))
    assert any("ANTHROPIC" in h for h in hits)


def test_detects_anthropic_auth_token_case_insensitive():
    hits = scan_staged(fake_git("+anthropic_auth_token: abcdefghijklmnop\n"))
    assert any("ANTHROPIC" in h for h in hits)


def test_detects_pushplus_style_hex_token_near_token_word():
    hits = scan_staged(fake_git('+token = "0123456789abcdef0123456789abcdef"\n'))
    assert any("PushPlus" in h for h in hits)


def test_does_not_flag_unrelated_32_char_hex_string():
    # 没有 "token" 字样在附近，不应该命中 PushPlus 规则
    hits = scan_staged(fake_git("+sha = '0123456789abcdef0123456789abcdef'\n"))
    assert hits == []


def test_multiple_hits_all_reported():
    text = "+a = 'sk-abcdefghijklmnopqrstuvwx'\n+b = gho_abcdefghijklmnopqrstuv\n"
    hits = scan_staged(fake_git(text))
    assert len(hits) == 2
