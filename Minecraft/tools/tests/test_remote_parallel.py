import io
import threading
import types

import pytest

import mcdeploy.remote as remote_mod
from mcdeploy.errors import McDeployError
from mcdeploy.remote import TMP_SUFFIX, SftpRemote


class FakeFile(io.BytesIO):
    def __init__(self, store, path, mode):
        super().__init__(store.files.get(path, b"") if "r" in mode else b"")
        self.store, self.path, self.mode = store, path, mode

    def prefetch(self, size):
        pass

    def stat(self):
        return types.SimpleNamespace(st_size=len(self.getvalue()))

    def close(self):
        if "w" in self.mode:
            if self.store.fail_upload and self.path.endswith(self.store.fail_upload + TMP_SUFFIX):
                raise OSError("disk full")
            with self.store.lock:
                self.store.files[self.path] = self.getvalue()
        super().close()


class FakeStore:
    """The server's files, shared by every fake SFTP channel."""
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.lock = threading.Lock()
        self.fail_upload = None
        self.channels = []


class FakeSftp:
    def __init__(self, store):
        self.store = store
        self.used = 0
        store.channels.append(self)

    def open(self, path, mode):
        self.used += 1
        if "r" in mode and path not in self.store.files:
            raise FileNotFoundError(path)
        return FakeFile(self.store, path, mode)

    def stat(self, path):
        if path not in self.store.files:
            raise FileNotFoundError(path)
        return types.SimpleNamespace(st_size=len(self.store.files[path]))

    def posix_rename(self, src, dst):
        with self.store.lock:
            self.store.files[dst] = self.store.files.pop(src)

    def remove(self, path):
        with self.store.lock:
            if path not in self.store.files:
                raise FileNotFoundError(path)
            del self.store.files[path]

    def mkdir(self, path):
        pass

    def close(self):
        pass


@pytest.fixture()
def fake(monkeypatch):
    store = FakeStore({f"./plugins/f{i}.yml": f"server {i}".encode() for i in range(20)})
    monkeypatch.setattr(remote_mod.paramiko.SFTPClient, "from_transport", lambda transport: FakeSftp(store))
    monkeypatch.delenv("MC_SFTP_CHANNELS", raising=False)
    r = SftpRemote.__new__(SftpRemote)
    r.root, r._known_dirs, r._posix_rename, r.transport = ".", set(), True, object()
    r.sftp = FakeSftp(store)
    return r, store


def test_read_many_uses_several_channels_and_keeps_order(fake):
    r, store = fake
    rels = [f"plugins/f{i}.yml" for i in range(20)] + ["plugins/missing.yml"]
    result = r.read_many(rels)
    assert list(result) == rels
    assert result["plugins/f7.yml"] == b"server 7" and result["plugins/missing.yml"] is None
    assert len(store.channels) == remote_mod.DEFAULT_SFTP_CHANNELS


def test_one_channel_reads_sequentially(fake, monkeypatch):
    r, store = fake
    monkeypatch.setenv("MC_SFTP_CHANNELS", "1")
    assert r.read_many(["plugins/f1.yml"]) == {"plugins/f1.yml": b"server 1"}
    assert len(store.channels) == 1


def test_invalid_channel_setting_fails(fake, monkeypatch):
    r, _ = fake
    monkeypatch.setenv("MC_SFTP_CHANNELS", "0")
    with pytest.raises(McDeployError, match="MC_SFTP_CHANNELS"):
        r.read_many(["plugins/f1.yml", "plugins/f2.yml"])


def test_refused_channel_fails_loudly(fake, monkeypatch):
    r, _ = fake

    def refuse(transport):
        raise OSError("administratively prohibited")

    monkeypatch.setattr(remote_mod.paramiko.SFTPClient, "from_transport", refuse)
    with pytest.raises(McDeployError, match="set MC_SFTP_CHANNELS lower"):
        r.read_many(["plugins/f1.yml", "plugins/f2.yml"])


def test_write_atomic_uploads_everything_in_parallel(fake):
    r, store = fake
    files = {f"plugins/f{i}.yml": f"new {i}".encode() for i in range(20)}
    r.write_atomic(files)
    assert all(store.files[f"./{rel}"] == data for rel, data in files.items())
    assert not any(p.endswith(TMP_SUFFIX) for p in store.files)


def test_failed_upload_replaces_nothing_and_removes_temp_files(fake):
    r, store = fake
    store.fail_upload = "plugins/f13.yml"
    before = dict(store.files)
    with pytest.raises(McDeployError, match="before any file was replaced(.|\n)*f13.yml"):
        r.write_atomic({f"plugins/f{i}.yml": b"new" for i in range(20)})
    assert store.files == before
