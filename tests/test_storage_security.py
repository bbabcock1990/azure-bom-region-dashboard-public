"""Security regression tests for the local storage backend."""
import os

import pytest

from _shared import storage


@pytest.fixture
def container(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    return storage.get_blob_container("snapshots")


def test_blob_roundtrip_nested_name(container):
    container.upload_blob(name="abc123/run-1.json", data=b"{}", overwrite=True)
    assert container.download_blob("abc123/run-1.json").readall() == b"{}"


@pytest.mark.parametrize("bad", [
    "../escape.json",
    "abc/../../escape.json",
    "..\\escape.json",
    "a/../../b",
])
def test_blob_path_rejects_traversal(container, bad):
    with pytest.raises(ValueError):
        container._path(bad)


def test_blob_absolute_path_is_contained_not_traversal(container, tmp_path):
    # A leading slash is stripped, so "/etc/passwd" maps to a nested blob
    # INSIDE the container — safe, no raise, no escape.
    p = container._path("/etc/passwd")
    base = os.path.realpath(os.path.join(str(tmp_path), "blobs", "snapshots"))
    assert os.path.realpath(p).startswith(base + os.sep)


def test_blob_upload_rejects_traversal(container):
    with pytest.raises(ValueError):
        container.upload_blob(name="../escape.json", data=b"x", overwrite=True)


def test_blob_path_stays_within_container(container, tmp_path):
    p = container._path("sub/run.json")
    base = os.path.realpath(os.path.join(str(tmp_path), "blobs", "snapshots"))
    assert os.path.realpath(p).startswith(base + os.sep)
