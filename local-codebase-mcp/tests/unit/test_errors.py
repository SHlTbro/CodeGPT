"""errors 单元测试：信封结构与 11 错误码 recoverable 与契约一致（直接读 contracts JSON 断言）。"""
from __future__ import annotations

import pytest

import errors
from errors import McpError, error_payload


def test_all_11_codes_match_contract(contract):
    contract_codes = [e["code"] for e in contract["errorCodes"]]
    assert len(contract_codes) == 18
    module_codes = [getattr(errors, c) for c in contract_codes]   # 常量按名存在
    assert set(module_codes) == set(contract_codes)


def test_recoverable_flags_match_contract(contract):
    for e in contract["errorCodes"]:
        assert errors._RECOVERABLE[e["code"]] == e["recoverable"], f"{e['code']}.recoverable 与契约不一致"


def test_envelope_structure():
    p = error_payload(errors.PROJECT_NOT_FOUND, "msg text", "hint text")
    assert set(p.keys()) == {"error"}
    assert set(p["error"].keys()) == {"code", "message", "recoverable", "hint"}
    assert p["error"]["code"] == "PROJECT_NOT_FOUND"
    assert p["error"]["message"] == "msg text"
    assert p["error"]["recoverable"] is True
    assert p["error"]["hint"] == "hint text"


def test_envelope_hint_defaults_empty():
    p = error_payload(errors.INTERNAL_ERROR, "boom")
    assert p["error"]["hint"] == ""
    assert p["error"]["recoverable"] is False


def test_unknown_code_rejected():
    with pytest.raises(ValueError):
        error_payload("NOT_A_REAL_CODE", "x")


def test_mcp_error_carries_payload_and_message():
    e = McpError(errors.FILE_NOT_VISIBLE, "不可见", "放弃该文件")
    assert str(e) == "不可见"
    assert e.payload["error"]["code"] == "FILE_NOT_VISIBLE"
    assert e.payload["error"]["recoverable"] is False
    assert e.payload["error"]["hint"] == "放弃该文件"
