from pathlib import Path

import pytest

from subbench.tasks.dataset import fetch, sha256

REV = "0123456789abcdef0123456789abcdef01234567"


def test_fetch_rejects_non_40_char_revision(tmp_path):
    with pytest.raises(ValueError, match="pin an exact 40-char commit sha"):
        fetch("main", cache_dir=tmp_path)


def test_fetch_downloads_and_writes_sidecar(monkeypatch, tmp_path):
    content = b"fake-parquet-content-12345"

    def fake_retrieve(url, tmp):
        Path(tmp).write_bytes(content)

    monkeypatch.setattr("urllib.request.urlretrieve", fake_retrieve)

    dest = fetch(REV, cache_dir=tmp_path)
    assert dest.exists()
    assert dest.name == f"verified-{REV[:12]}.parquet"
    assert dest.read_bytes() == content

    sidecar = dest.with_suffix(".sha256")
    assert sidecar.exists()
    expected_digest = sha256(dest)
    assert sidecar.read_text().strip() == expected_digest

    # Calling fetch again re-verifies without error
    dest2 = fetch(REV, cache_dir=tmp_path)
    assert dest2 == dest


def test_fetch_sha256_mismatch_raises_error(monkeypatch, tmp_path):
    content = b"fake-parquet-content-12345"

    def fake_retrieve(url, tmp):
        Path(tmp).write_bytes(content)

    monkeypatch.setattr("urllib.request.urlretrieve", fake_retrieve)

    dest = fetch(REV, cache_dir=tmp_path)
    sidecar = dest.with_suffix(".sha256")
    # Tamper with the sidecar
    sidecar.write_text("corrupted-sha256\n")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetch(REV, cache_dir=tmp_path)


def test_fetch_creates_sidecar_if_existing_parquet_lacks_it(tmp_path):
    dest = tmp_path / f"verified-{REV[:12]}.parquet"
    dest.write_bytes(b"existing-content")
    sidecar = dest.with_suffix(".sha256")
    assert not sidecar.exists()

    res = fetch(REV, cache_dir=tmp_path)
    assert res == dest
    assert sidecar.exists()
    assert sidecar.read_text().strip() == sha256(dest)
