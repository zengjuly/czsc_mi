"""W105: 飞书 webhook 统一出口 + minute_macd 去 Hermes 依赖测试。"""
from unittest.mock import MagicMock, patch

from mystery.services import feishu as m


def test_webhook_from_env(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://open.feishu.cn/hook/xxx")
    assert m.webhook_url() == "https://open.feishu.cn/hook/xxx"


def test_webhook_empty(monkeypatch):
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    monkeypatch.setattr(m, "_WEBHOOK_FILE", m.Path("/nonexistent/webhook"))
    assert m.webhook_url() == ""


def test_send_text_no_webhook(monkeypatch):
    monkeypatch.setattr(m, "webhook_url", lambda: "")
    assert m.send_text("hello") is False


def test_send_text_success(monkeypatch):
    monkeypatch.setattr(m, "webhook_url", lambda: "https://open.feishu.cn/hook/xxx")
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    resp.read = lambda: b'{"code":0,"msg":"success"}'
    with patch.object(m.urllib.request, "urlopen", return_value=resp) as u:
        assert m.send_text("金叉提醒", prefix="[CZSC-MI]\n") is True
    req = u.call_args[0][0]
    assert b"[CZSC-MI]" in req.data


def test_send_text_code_success(monkeypatch):
    monkeypatch.setattr(m, "webhook_url", lambda: "https://x")
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    resp.read = lambda: b'"Success"'
    with patch.object(m.urllib.request, "urlopen", return_value=resp):
        assert m.send_text("x") is True


def test_minute_macd_no_hermes_source():
    """源码级断言：minute_macd.py 不再含 hermes/subprocess 调用。"""
    from pathlib import Path
    import mystery.services.minute_macd as mm
    src = Path(mm.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in src
    assert "_HERMES" not in src
    assert "hermes send" not in src
    assert "_feishu_send" in src
