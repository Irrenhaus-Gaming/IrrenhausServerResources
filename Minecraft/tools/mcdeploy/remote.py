"""Remote file access to an AMP instance over its built-in SFTP server.

The host key is pinned (AMP_SFTP_HOST_KEY); an unknown or changed key aborts the connection.
`LocalRemote` implements the same interface on a local directory for tests.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import posixpath
import stat
from pathlib import Path

import paramiko

from .config import require_values
from .errors import McDeployError

# The SFTP endpoint is the server's own host (MC_SERVER_HOST, also what Velocity uses unless
# MC_SERVER_PROXY_HOST overrides it) plus the AMP instance's SFTP port. AMP_SFTP_USER/PASSWORD are
# the one deploy account (repository secrets). An AMP instance's SFTP login lands in its main
# folder (where the server jar lives, plugins/ below it), so all paths are relative to the login folder.
SFTP_KEYS = ["MC_SERVER_HOST", "MC_SERVER_SFTP_PORT", "AMP_SFTP_USER", "AMP_SFTP_PASSWORD", "AMP_SFTP_HOST_KEY"]
TMP_SUFFIX = ".mcdeploy-tmp"
OLD_SUFFIX = ".mcdeploy-old"


class RemoteBase:
    def read(self, rel: str) -> bytes | None:
        raise NotImplementedError

    def write_atomic(self, files: dict[str, bytes]) -> None:
        raise NotImplementedError

    def list_files(self, rel_dir: str) -> list[str]:
        raise NotImplementedError

    def file_stats(self, rel_dir: str) -> dict[str, tuple[int, int]]:
        """File name -> (size, mtime) for the files directly in rel_dir."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def _parse_host_key(line: str) -> paramiko.PKey:
    parts = line.strip().split()
    # Accept "type base64" or a full known_hosts line "host type base64".
    if len(parts) >= 3 and not parts[0].startswith(("ssh-", "ecdsa-")):
        parts = parts[1:]
    if len(parts) < 2:
        raise McDeployError("AMP_SFTP_HOST_KEY must look like 'ssh-ed25519 AAAA...'")
    try:
        return paramiko.PKey.from_type_string(parts[0], base64.b64decode(parts[1]))
    except Exception as exc:
        raise McDeployError(f"AMP_SFTP_HOST_KEY could not be parsed: {exc}") from None


def fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class SftpRemote(RemoteBase):
    def __init__(self, values: dict[str, str]):
        v = require_values(SFTP_KEYS, values, "the SFTP connection")
        try:
            port = int(v["MC_SERVER_SFTP_PORT"])
        except ValueError:
            raise McDeployError("MC_SERVER_SFTP_PORT must be a number") from None
        self.root = "."
        pinned = _parse_host_key(v["AMP_SFTP_HOST_KEY"])
        self.transport = paramiko.Transport((v["MC_SERVER_HOST"], port))
        try:
            self.transport.start_client(timeout=30)
            server_key = self.transport.get_remote_server_key()
            if server_key.get_name() != pinned.get_name() or server_key.asbytes() != pinned.asbytes():
                raise McDeployError(
                    f"SFTP host key mismatch: server presented {server_key.get_name()} {fingerprint(server_key)}, "
                    f"pinned is {pinned.get_name()} {fingerprint(pinned)}. Refusing to connect."
                )
            self.transport.auth_password(v["AMP_SFTP_USER"], v["AMP_SFTP_PASSWORD"])
            if not self.transport.is_authenticated():
                raise McDeployError("SFTP authentication failed")
            self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        except McDeployError:
            self.transport.close()
            raise
        except Exception as exc:
            self.transport.close()
            raise McDeployError(f"SFTP connection failed: {exc}") from None

    def _abs(self, rel: str) -> str:
        if rel.startswith("/") or ".." in rel.split("/"):
            raise McDeployError(f"refusing unsafe remote path {rel!r}")
        return posixpath.join(self.root, rel) if rel else self.root

    def read(self, rel: str) -> bytes | None:
        try:
            with self.sftp.open(self._abs(rel), "rb") as fh:
                # Read-ahead: without it paramiko waits for every 32 KiB block (45 min for ~100 MB of jars).
                fh.prefetch(fh.stat().st_size)
                return fh.read()
        except FileNotFoundError:
            return None
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise McDeployError(f"SFTP read of {rel} failed: {exc}") from None

    def exists(self, rel: str) -> bool:
        try:
            self.sftp.stat(self._abs(rel))
            return True
        except FileNotFoundError:
            return False
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                return False
            raise McDeployError(f"SFTP stat of {rel} failed: {exc}") from None

    def _mkdirs(self, rel_dir: str) -> None:
        if not rel_dir:
            return
        current = ""
        for part in rel_dir.split("/"):
            current = f"{current}/{part}" if current else part
            if not self.exists(current):
                try:
                    self.sftp.mkdir(self._abs(current))
                except IOError as exc:
                    raise McDeployError(f"SFTP mkdir {current} failed: {exc}") from None

    def _replace(self, src: str, dst: str) -> None:
        """Move src over dst. Uses posix-rename when the server supports it, else rename-aside."""
        try:
            self.sftp.posix_rename(self._abs(src), self._abs(dst))
            return
        except IOError:
            pass  # extension unsupported or failed; fall back below and verify explicitly
        if self.exists(dst):
            self.sftp.rename(self._abs(dst), self._abs(dst + OLD_SUFFIX))
            try:
                self.sftp.rename(self._abs(src), self._abs(dst))
            except IOError as exc:
                self.sftp.rename(self._abs(dst + OLD_SUFFIX), self._abs(dst))
                raise McDeployError(f"SFTP rename onto {dst} failed (original restored): {exc}") from None
            self.sftp.remove(self._abs(dst + OLD_SUFFIX))
        else:
            self.sftp.rename(self._abs(src), self._abs(dst))

    def write_atomic(self, files: dict[str, bytes]) -> None:
        """Upload every file to a temp name first; only when all uploads succeeded move them in place."""
        staged: list[str] = []
        try:
            for rel, data in files.items():
                self._mkdirs(posixpath.dirname(rel))
                with self.sftp.open(self._abs(rel + TMP_SUFFIX), "wb") as fh:
                    fh.write(data)
                if self.sftp.stat(self._abs(rel + TMP_SUFFIX)).st_size != len(data):
                    raise McDeployError(f"SFTP upload of {rel} is incomplete")
                staged.append(rel)
        except Exception as exc:
            for rel in staged:
                try:
                    self.sftp.remove(self._abs(rel + TMP_SUFFIX))
                except IOError:
                    pass  # best effort cleanup of our own temp files; the original error is raised below
            if isinstance(exc, McDeployError):
                raise
            raise McDeployError(f"SFTP upload failed before any file was replaced: {exc}") from None
        for rel in staged:
            try:
                self._replace(rel + TMP_SUFFIX, rel)
            except McDeployError:
                raise
            except Exception as exc:
                raise McDeployError(
                    f"SFTP replace of {rel} failed after earlier files were already replaced: {exc}. "
                    f"The deploy is PARTIAL; rerun after fixing the cause."
                ) from None

    def list_files(self, rel_dir: str) -> list[str]:
        try:
            entries = self.sftp.listdir_attr(self._abs(rel_dir))
        except IOError as exc:
            raise McDeployError(f"SFTP listing of {rel_dir or '/'} failed: {exc}") from None
        return sorted(e.filename for e in entries if not stat.S_ISDIR(e.st_mode or 0))

    def file_stats(self, rel_dir: str) -> dict[str, tuple[int, int]]:
        try:
            entries = self.sftp.listdir_attr(self._abs(rel_dir))
        except IOError as exc:
            raise McDeployError(f"SFTP listing of {rel_dir or '/'} failed: {exc}") from None
        return {e.filename: (e.st_size or 0, int(e.st_mtime or 0)) for e in entries if not stat.S_ISDIR(e.st_mode or 0)}

    def list_dirs(self, rel_dir: str) -> list[str]:
        try:
            entries = self.sftp.listdir_attr(self._abs(rel_dir))
        except IOError as exc:
            raise McDeployError(f"SFTP listing of {rel_dir or '/'} failed: {exc}") from None
        return sorted(e.filename for e in entries if stat.S_ISDIR(e.st_mode or 0))

    def close(self) -> None:
        self.transport.close()


def fetch_host_key(host: str, port: int) -> paramiko.PKey:
    """Read the server's host key without authenticating (for the initial pinning)."""
    transport = paramiko.Transport((host, port))
    try:
        transport.start_client(timeout=30)
        return transport.get_remote_server_key()
    except Exception as exc:
        raise McDeployError(f"could not read host key from {host}:{port}: {exc}") from None
    finally:
        transport.close()


class LocalRemote(RemoteBase):
    """Directory-backed stand-in for SftpRemote (tests, local dry runs)."""

    def __init__(self, root: Path):
        self.root = root

    def read(self, rel: str) -> bytes | None:
        path = self.root / rel
        return path.read_bytes() if path.is_file() else None

    def write_atomic(self, files: dict[str, bytes]) -> None:
        for rel, data in files.items():
            tmp = self.root / (rel + TMP_SUFFIX)
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
        for rel in files:
            (self.root / (rel + TMP_SUFFIX)).replace(self.root / rel)

    def list_files(self, rel_dir: str) -> list[str]:
        base = self.root / rel_dir
        return sorted(p.name for p in base.iterdir() if p.is_file()) if base.is_dir() else []

    def file_stats(self, rel_dir: str) -> dict[str, tuple[int, int]]:
        base = self.root / rel_dir
        if not base.is_dir():
            return {}
        return {p.name: (p.stat().st_size, int(p.stat().st_mtime)) for p in base.iterdir() if p.is_file()}

    def list_dirs(self, rel_dir: str) -> list[str]:
        base = self.root / rel_dir
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
