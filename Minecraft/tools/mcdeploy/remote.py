"""Remote file access to an AMP instance over its built-in SFTP server.

The host key is pinned (AMP_SFTP_HOST_KEY); an unknown or changed key aborts the connection.
`LocalRemote` implements the same interface on a local directory for tests.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import os
import posixpath
import queue
import stat
from concurrent.futures import ThreadPoolExecutor
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
# Every SFTP request waits a full round trip to the server, and a file needs several (open, stat,
# read/write, close, rename). Many files therefore go over this many SFTP channels of the one
# connection at once. MC_SFTP_CHANNELS overrides it (1 = one file after the other).
DEFAULT_SFTP_CHANNELS = 8


class RemoteBase:
    def read(self, rel: str) -> bytes | None:
        raise NotImplementedError

    def read_many(self, rels: list[str]) -> dict[str, bytes | None]:
        """read() for many files (None for missing ones); SftpRemote fetches them in parallel."""
        return {rel: self.read(rel) for rel in rels}

    def write_atomic(self, files: dict[str, bytes]) -> None:
        raise NotImplementedError

    def list_files(self, rel_dir: str) -> list[str]:
        raise NotImplementedError

    def file_stats(self, rel_dir: str) -> dict[str, tuple[int, int]]:
        """File name -> (size, mtime) for the files directly in rel_dir."""
        raise NotImplementedError

    def list_dirs(self, rel_dir: str) -> list[str]:
        raise NotImplementedError

    def open_binary(self, rel: str):
        """Seekable binary file object (e.g. to read one entry of a remote jar), or None if missing."""
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
        self._known_dirs: set[str] = set()  # remote folders already checked/created this session
        self._posix_rename = True           # until the server shows it lacks the extension
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

    def _channels(self, jobs: int) -> int:
        raw = os.environ.get("MC_SFTP_CHANNELS", str(DEFAULT_SFTP_CHANNELS))
        if not raw.isdigit() or int(raw) < 1:
            raise McDeployError("MC_SFTP_CHANNELS must be a whole number >= 1")
        return max(1, min(int(raw), jobs))

    def _parallel(self, fn, items: list) -> list:
        """fn(sftp, item) for every item, spread over several SFTP channels. Results in item order.
        Every job finishes before the first error (if any) is raised, so callers can clean up."""
        count = self._channels(len(items))
        if count == 1:
            return [fn(self.sftp, item) for item in items]
        clients: queue.Queue = queue.Queue()
        clients.put(self.sftp)
        extra: list[paramiko.SFTPClient] = []
        try:
            for i in range(count - 1):
                try:
                    extra.append(paramiko.SFTPClient.from_transport(self.transport))
                except Exception as exc:
                    raise McDeployError(
                        f"the SFTP server refused channel {i + 2} of {count} ({exc}); "
                        f"set MC_SFTP_CHANNELS lower (1 = no parallel transfers)"
                    ) from None
                clients.put(extra[-1])

            def run(item):
                sftp = clients.get()
                try:
                    return fn(sftp, item)
                finally:
                    clients.put(sftp)

            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(run, item) for item in items]
            errors = [f.exception() for f in futures if f.exception() is not None]
            if errors:
                raise errors[0]
            return [f.result() for f in futures]
        finally:
            for client in extra:
                client.close()

    def read(self, rel: str) -> bytes | None:
        return self._read_with(self.sftp, rel)

    def read_many(self, rels: list[str]) -> dict[str, bytes | None]:
        return dict(zip(rels, self._parallel(self._read_with, rels)))

    def _read_with(self, sftp: paramiko.SFTPClient, rel: str) -> bytes | None:
        try:
            with sftp.open(self._abs(rel), "rb") as fh:
                # Read-ahead: without it paramiko waits for every 32 KiB block (45 min for ~100 MB of jars).
                fh.prefetch(fh.stat().st_size)
                return fh.read()
        except FileNotFoundError:
            return None
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise McDeployError(f"SFTP read of {rel} failed: {exc}") from None

    def open_binary(self, rel: str):
        try:
            return self.sftp.open(self._abs(rel), "rb")
        except FileNotFoundError:
            return None
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise McDeployError(f"SFTP open of {rel} failed: {exc}") from None

    def exists(self, rel: str) -> bool:
        return self._exists_with(self.sftp, rel)

    def _exists_with(self, sftp: paramiko.SFTPClient, rel: str) -> bool:
        try:
            sftp.stat(self._abs(rel))
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
            if current in self._known_dirs:
                continue
            self._known_dirs.add(current)
            if not self.exists(current):
                try:
                    self.sftp.mkdir(self._abs(current))
                except IOError as exc:
                    raise McDeployError(f"SFTP mkdir {current} failed: {exc}") from None

    def _replace(self, sftp: paramiko.SFTPClient, src: str, dst: str) -> None:
        """Move src over dst. Uses posix-rename when the server supports it, else rename-aside."""
        if self._posix_rename:
            try:
                sftp.posix_rename(self._abs(src), self._abs(dst))
                return
            except IOError:
                # Unsupported by this server (AMP may not offer the extension): stop trying for
                # every file and use the explicit rename-aside below from now on.
                self._posix_rename = False
        if self._exists_with(sftp, dst):
            sftp.rename(self._abs(dst), self._abs(dst + OLD_SUFFIX))
            try:
                sftp.rename(self._abs(src), self._abs(dst))
            except IOError as exc:
                sftp.rename(self._abs(dst + OLD_SUFFIX), self._abs(dst))
                raise McDeployError(f"SFTP rename onto {dst} failed (original restored): {exc}") from None
            sftp.remove(self._abs(dst + OLD_SUFFIX))
        else:
            sftp.rename(self._abs(src), self._abs(dst))

    def write_atomic(self, files: dict[str, bytes]) -> None:
        """Upload every file to a temp name first; only when all uploads succeeded move them in place."""
        # Folders first, one after the other: parallel uploads into one new folder would race to create it.
        for rel in files:
            self._mkdirs(posixpath.dirname(rel))
        rels = list(files)

        def stage(sftp: paramiko.SFTPClient, rel: str) -> str | None:
            """Upload one temp file. Returns the error instead of raising, so every upload is attempted
            and the cleanup below knows the outcome of each."""
            try:
                with sftp.open(self._abs(rel + TMP_SUFFIX), "wb") as fh:
                    fh.write(files[rel])
                if sftp.stat(self._abs(rel + TMP_SUFFIX)).st_size != len(files[rel]):
                    return f"SFTP upload of {rel} is incomplete"
                return None
            except Exception as exc:
                return f"SFTP upload of {rel} failed: {exc}"

        failed = [err for err in self._parallel(stage, rels) if err]
        if failed:
            for rel in rels:
                try:
                    self.sftp.remove(self._abs(rel + TMP_SUFFIX))
                except IOError:
                    pass  # best effort cleanup of our own temp files (failed ones may not exist); the error is raised below
            raise McDeployError("SFTP upload failed before any file was replaced:\n" + "\n".join(f"  - {e}" for e in failed))

        def replace(sftp: paramiko.SFTPClient, rel: str) -> None:
            try:
                self._replace(sftp, rel + TMP_SUFFIX, rel)
            except McDeployError as exc:
                raise McDeployError(f"{exc}. The deploy is PARTIAL; rerun after fixing the cause.") from None
            except Exception as exc:
                raise McDeployError(
                    f"SFTP replace of {rel} failed after other files may already be replaced: {exc}. "
                    f"The deploy is PARTIAL; rerun after fixing the cause."
                ) from None

        self._parallel(replace, rels)

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


def verify_instance(target, remote: RemoteBase, values: dict[str, str]) -> None:
    """A backend's SFTP folder must belong to the server its environment describes: AMP writes
    the instance's Minecraft port into server.properties, and it must equal MC_SERVER_MC_PORT.
    Catches an MC_SERVER_SFTP_PORT that points at another AMP instance before anything happens."""
    if target.profile != "backend":
        return
    from .formats import parse_properties

    expected = require_values(["MC_SERVER_MC_PORT"], values, f"{target.name} (instance check)")["MC_SERVER_MC_PORT"]
    raw = remote.read("server.properties")
    if raw is None:
        raise McDeployError(
            f"{target.name}: no server.properties in the SFTP folder, so it is not this server's instance "
            f"(check MC_SERVER_SFTP_PORT). Nothing was changed."
        )
    port = parse_properties(raw.decode("utf-8", "replace")).get("server-port", "").strip()
    if port != expected.strip():
        raise McDeployError(
            f"{target.name}: the SFTP folder belongs to a server with a different Minecraft port than "
            f"MC_SERVER_MC_PORT, so MC_SERVER_SFTP_PORT points at another AMP instance. Nothing was changed."
        )


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

    def open_binary(self, rel: str):
        path = self.root / rel
        return path.open("rb") if path.is_file() else None
