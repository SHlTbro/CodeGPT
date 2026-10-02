"""task24 write_binary_file 单元测试：扩展名白名单、base64 解码、10MB 预算、
冲突语义（与 write_file 同源）、路径/敏感守卫、sha256 口径=解码后 bytes。"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

import errors
import writer
from config import Config
from errors import McpError
from projects import Project
from writer import MAX_BINARY_BYTES, write_binary_file

PDF_SPECIMEN = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF"
DOCX_MAGIC = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 8 + b"docx specimen body"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def expect_err(fn, code: str) -> McpError:
    with pytest.raises(McpError) as ei:
        fn()
    assert ei.value.payload["error"]["code"] == code, f"期望 {code}，实际 {ei.value.payload['error']['code']}"
    return ei.value


@pytest.fixture()
def brepo(tmp_path) -> tuple[Config, Path]:
    """write_enabled=true 项目 + 默认关项目，docs/ 预建；write_seq 进程态测试间重置。"""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs" / "spec.pdf").write_bytes(PDF_SPECIMEN)
    off = tmp_path / "off"
    off.mkdir()
    on = Project(id="on", name="On", path=root, write_enabled=True)
    offp = Project(id="off", name="Off", path=off, write_enabled=False)
    writer._LOCKS.clear()
    writer._SEQS.clear()
    return Config(projects=(on, offp)), root


# ---------------------------------------------------------------- 扩展名白名单
@pytest.mark.parametrize("name", ["report.docx", "a.pdf", "t.xlsx", "arc.zip", "s.pptx", "old.doc", "t97.xls"])
def test_whitelisted_extensions_create_ok(brepo, name):
    """契约 v1.6 白名单七扩展名全部放行，bytes 落盘与解码结果一致。"""
    cfg, root = brepo
    r = write_binary_file(cfg, "on", f"docs/{name}", b64(DOCX_MAGIC), expected_absent=True)
    assert r["created"] is True and r["bytes_written"] == len(DOCX_MAGIC)
    assert (root / "docs" / name).read_bytes() == DOCX_MAGIC


def test_extension_case_insensitive(brepo):
    """大小写不敏感：docs/UPPER.DOCX 放行（.DOCX 归一 .docx）。"""
    cfg, root = brepo
    r = write_binary_file(cfg, "on", "docs/UPPER.DOCX", b64(b"PK\x03\x04"), expected_absent=True)
    assert r["created"] is True
    assert (root / "docs" / "UPPER.DOCX").read_bytes() == b"PK\x03\x04"


def test_extension_exe_rejected(brepo):
    """非白名单扩展名 .exe → EXTENSION_NOT_ALLOWED（recoverable=false）。"""
    cfg, root = brepo
    e = expect_err(lambda: write_binary_file(cfg, "on", "docs/evil.exe", b64(b"MZ"), expected_absent=True),
                   errors.EXTENSION_NOT_ALLOWED)
    assert e.payload["error"]["recoverable"] is False
    assert not (root / "docs" / "evil.exe").exists()


def test_extension_missing_rejected(brepo):
    """无扩展名 → EXTENSION_NOT_ALLOWED。"""
    cfg, _ = brepo
    expect_err(lambda: write_binary_file(cfg, "on", "docs/noext", b64(b"x"), expected_absent=True),
               errors.EXTENSION_NOT_ALLOWED)


def test_extension_other_rejected(brepo):
    """.txt/.py 等文本扩展名 → EXTENSION_NOT_ALLOWED（文本走 write_file）。"""
    cfg, _ = brepo
    expect_err(lambda: write_binary_file(cfg, "on", "docs/notes.txt", b64(b"x"), expected_absent=True),
               errors.EXTENSION_NOT_ALLOWED)
    expect_err(lambda: write_binary_file(cfg, "on", "docs/m.py", b64(b"x"), expected_absent=True),
               errors.EXTENSION_NOT_ALLOWED)


# ---------------------------------------------------------------- base64 解码
def test_invalid_base64_rejected(brepo):
    """非合法 base64 → INVALID_ARGUMENT，message 注明 base64。"""
    cfg, _ = brepo
    e = expect_err(lambda: write_binary_file(cfg, "on", "docs/x.docx", "!!!not-base64!!!", expected_absent=True),
                   errors.INVALID_ARGUMENT)
    assert "base64" in e.payload["error"]["message"]


def test_base64_strict_whitespace_rejected(brepo):
    """validate=True 严格口径：内嵌换行/空格的松散 base64 → INVALID_ARGUMENT。"""
    cfg, _ = brepo
    loose = base64.encodebytes(b"some binary")            # 含 \n 的 MIME 形态
    expect_err(lambda: write_binary_file(cfg, "on", "docs/x.docx", loose, expected_absent=True),
              errors.INVALID_ARGUMENT)


# ---------------------------------------------------------------- 10MB 预算（解码后口径）
def test_payload_over_10mb_rejected(brepo):
    """解码后 >10MB → PAYLOAD_TOO_LARGE（口径=解码后字节数，非 base64 串长）。"""
    cfg, root = brepo
    big = b"a" * (MAX_BINARY_BYTES + 1)
    expect_err(lambda: write_binary_file(cfg, "on", "docs/big.zip", b64(big), expected_absent=True),
               errors.PAYLOAD_TOO_LARGE)
    assert not (root / "docs" / "big.zip").exists()


def test_payload_exactly_10mb_ok(brepo):
    """恰 10MB（10485760 字节）不超限；base64 串本身 >10MB 仍放行（解码后口径）。"""
    cfg, root = brepo
    exact = b"b" * MAX_BINARY_BYTES
    assert len(b64(exact)) > MAX_BINARY_BYTES              # 证明按解码后而非串长判定
    r = write_binary_file(cfg, "on", "docs/exact.zip", b64(exact), expected_absent=True)
    assert r["bytes_written"] == MAX_BINARY_BYTES
    assert (root / "docs" / "exact.zip").stat().st_size == MAX_BINARY_BYTES


# ---------------------------------------------------------------- 新建 / 冲突 / 更新
def test_create_pdf_magic_bytes_verified(brepo):
    """新建成功：真实 base64 编码的 PDF 标本落盘，%PDF- magic 与逐字节一致。"""
    cfg, root = brepo
    r = write_binary_file(cfg, "on", "docs/09-29-JD雷达.docx", b64(PDF_SPECIMEN), expected_absent=True)
    assert r["project_id"] == "on" and r["path"] == "docs/09-29-JD雷达.docx"
    assert r["created"] is True and r["write_seq"] == 1
    disk = (root / "docs" / "09-29-JD雷达.docx").read_bytes()
    assert disk.startswith(b"%PDF-") and disk == PDF_SPECIMEN
    assert r["bytes_written"] == len(PDF_SPECIMEN)


def test_create_twice_file_already_exists(brepo):
    """重复新建（expected_absent=true 而文件在）→ FILE_ALREADY_EXISTS，原文件未被覆盖。"""
    cfg, root = brepo
    write_binary_file(cfg, "on", "docs/a.pdf", b64(b"%PDF-v1"), expected_absent=True)
    expect_err(lambda: write_binary_file(cfg, "on", "docs/a.pdf", b64(b"%PDF-v2"), expected_absent=True),
               errors.FILE_ALREADY_EXISTS)
    assert (root / "docs" / "a.pdf").read_bytes() == b"%PDF-v1"


def test_update_with_correct_sha(brepo):
    """带正确 sha 更新成功：created=False、write_seq 递增、新内容落盘。"""
    cfg, root = brepo
    r1 = write_binary_file(cfg, "on", "docs/spec.pdf", b64(b"%PDF-new"), content_sha256=sha_of(root, "docs/spec.pdf"))
    assert r1["created"] is False and r1["write_seq"] == 1
    assert (root / "docs" / "spec.pdf").read_bytes() == b"%PDF-new"
    assert r1["sha256"] == hashlib.sha256(b"%PDF-new").hexdigest()


def test_update_without_sha_rejected(brepo):
    """更新已有文件未带 content_sha256 → INVALID_ARGUMENT。"""
    cfg, _ = brepo
    e = expect_err(lambda: write_binary_file(cfg, "on", "docs/spec.pdf", b64(b"x")), errors.INVALID_ARGUMENT)
    assert "content_sha256" in e.payload["error"]["message"]


def test_update_wrong_sha_conflict_zero_disk_change(brepo):
    """带错误 sha → REVISION_CONFLICT 且零落盘（磁盘字节保持原样）。"""
    cfg, root = brepo
    before = (root / "docs" / "spec.pdf").read_bytes()
    expect_err(lambda: write_binary_file(cfg, "on", "docs/spec.pdf", b64(b"%PDF-evil"),
                                         content_sha256="0" * 64), errors.REVISION_CONFLICT)
    assert (root / "docs" / "spec.pdf").read_bytes() == before


def test_sha256_is_hash_of_decoded_bytes(brepo):
    """sha256 口径=解码后 bytes（与手工 hashlib 对照），非 base64 字符串 hash。"""
    cfg, root = brepo
    payload = bytes(range(256)) * 4
    r = write_binary_file(cfg, "on", "docs/hash.pdf", b64(payload), expected_absent=True)
    assert r["sha256"] == hashlib.sha256(payload).hexdigest()
    assert r["sha256"] != hashlib.sha256(b64(payload).encode("ascii")).hexdigest()
    assert r["sha256"] == hashlib.sha256((root / "docs" / "hash.pdf").read_bytes()).hexdigest()


# ---------------------------------------------------------------- 写开关 / 路径 / 敏感
def test_write_disabled(brepo):
    """write_enabled=false → WRITE_DISABLED，无文件产生。"""
    cfg, root = brepo
    expect_err(lambda: write_binary_file(cfg, "off", "docs/x.docx", b64(b"x"), expected_absent=True),
               errors.WRITE_DISABLED)
    assert not (root.parent / "off" / "docs").exists()


def test_path_outside_docs_forbidden(brepo):
    """docs 外路径：src/x.docx 与 docs/../evil.pdf（final 解析后不在 docs）→ WRITE_PATH_FORBIDDEN。"""
    cfg, root = brepo
    expect_err(lambda: write_binary_file(cfg, "on", "src/x.docx", b64(b"x"), expected_absent=True),
               errors.WRITE_PATH_FORBIDDEN)
    expect_err(lambda: write_binary_file(cfg, "on", "docs/../evil.pdf", b64(b"x"), expected_absent=True),
               errors.WRITE_PATH_FORBIDDEN)
    assert not (root / "evil.pdf").exists() and not (root / "src" / "x.docx").exists()


def test_path_escape_workspace(brepo):
    """../ 逃逸项目根 → PATH_OUTSIDE_WORKSPACE。"""
    cfg, _ = brepo
    expect_err(lambda: write_binary_file(cfg, "on", "../evil.pdf", b64(b"x"), expected_absent=True),
               errors.PATH_OUTSIDE_WORKSPACE)


def test_sensitive_env_pdf_not_visible(brepo):
    """docs/.env.pdf → FILE_NOT_VISIBLE：默认 .env 为字面 pattern，白名单扩展名不得包裹敏感名绕过。"""
    cfg, root = brepo
    expect_err(lambda: write_binary_file(cfg, "on", "docs/.env.pdf", b64(b"x"), expected_absent=True),
               errors.FILE_NOT_VISIBLE)
    assert not (root / "docs" / ".env.pdf").exists()


def test_sensitive_plain_env_not_visible(brepo):
    """docs/.env（扩展名白名单前置即拒）→ FILE_NOT_VISIBLE。"""
    cfg, root = brepo
    expect_err(lambda: write_binary_file(cfg, "on", "docs/.env", b64(b"K=1"), expected_absent=True),
               errors.EXTENSION_NOT_ALLOWED)


# ---------------------------------------------------------------- 参数校验 / 原子写
def test_invalid_args(brepo):
    """path 空 / content_b64 非字符串 / expected_absent 非布尔 → INVALID_ARGUMENT。"""
    cfg, _ = brepo
    expect_err(lambda: write_binary_file(cfg, "on", "", b64(b"x")), errors.INVALID_ARGUMENT)
    expect_err(lambda: write_binary_file(cfg, "on", "docs/x.docx", None), errors.INVALID_ARGUMENT)   # type: ignore[arg-type]
    expect_err(lambda: write_binary_file(cfg, "on", "docs/x.docx", b64(b"x"), expected_absent="yes"), errors.INVALID_ARGUMENT)  # type: ignore[arg-type]


def test_atomic_no_tmp_residue_and_seq(brepo):
    """原子写无 .tmp 残留；write_seq per-project 递增。"""
    cfg, root = brepo
    s1 = write_binary_file(cfg, "on", "docs/a.docx", b64(b"PK1"), expected_absent=True)
    s2 = write_binary_file(cfg, "on", "docs/b.pdf", b64(b"%PDF2"), expected_absent=True)
    assert (s1["write_seq"], s2["write_seq"]) == (1, 2)
    assert list((root / "docs").glob("*.tmp*")) == []


def sha_of(root: Path, rel: str) -> str:
    return hashlib.sha256((root / rel).read_bytes()).hexdigest()
