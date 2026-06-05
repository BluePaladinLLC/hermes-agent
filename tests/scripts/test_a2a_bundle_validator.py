"""Tests for the Pons A2A bundle validator."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest


VALIDATOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "validate_pons_a2a_bundle.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_pons_a2a_bundle", VALIDATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add_bytes(tf: tarfile.TarFile, name: str, data: bytes = b"x") -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def test_safe_extract_rejects_path_traversal(tmp_path):
    validator = _load_validator()
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        _add_bytes(tf, "bundle/ok.txt")
        _add_bytes(tf, "../escape.txt")

    with tarfile.open(archive, "r:gz") as tf, pytest.raises(SystemExit, match="unsafe archive member path"):
        validator._safe_extract(tf, tmp_path / "extract")

    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_unsafe_symlink(tmp_path):
    validator = _load_validator()
    archive = tmp_path / "bad-link.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("bundle/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../escape.txt"
        tf.addfile(info)

    with tarfile.open(archive, "r:gz") as tf, pytest.raises(SystemExit, match="unsafe archive link target"):
        validator._safe_extract(tf, tmp_path / "extract")


def test_safe_extract_accepts_normal_bundle_member(tmp_path):
    validator = _load_validator()
    archive = tmp_path / "ok.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        _add_bytes(tf, "bundle/runtime/gateway/a2a_valkey.py", b"# ok\n")

    extract_root = tmp_path / "extract"
    with tarfile.open(archive, "r:gz") as tf:
        validator._safe_extract(tf, extract_root)

    assert (extract_root / "bundle" / "runtime" / "gateway" / "a2a_valkey.py").read_text() == "# ok\n"
