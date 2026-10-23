"""Heuristic secret detection for config files.

Used by lint (any hit fails), by the one-time conversion (hits become placeholders) and by the
sync-back (hits block the PR). gitleaks runs in addition to this for generic token formats.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path

from .config import SCAN_ALLOWLIST_FILE, load_yaml
from .errors import McDeployError, fail_if
from .formats import detect_format
from .placeholders import TOKEN_RE

# key: value / key = value / "key": value, with optional quotes around key and value.
ASSIGN_RE = re.compile(
    r"""^(?P<indent>\s*)(?P<kq>["']?)(?P<key>[^"'=:\s#][^"'=:#]*?)(?P=kq)\s*(?P<sep>[:=])[ \t]*(?P<rest>.*?)\s*$"""
)
SECRET_KEY_RE = re.compile(
    r"(password|passwd|passphrase|secret|token|apikey|webhook|webhookurl|privatekey|clientsecret|"
    r"username|dbuser|mysqluser|license|licensekey|authkey)$"
)
SECRET_KEY_EXACT = {"user", "pass", "pw", "pwd", "auth"}
HOST_KEY_RE = re.compile(r"(host|hostname|address|ip|uri|url|jdbcurl|connectionuri|connection)$")
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
TOKEN_PATTERNS = {
    "discord bot token": re.compile(r"[MNO][A-Za-z\d_-]{23,25}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"),
    "discord webhook": re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z\d_-]+"),
    "nightbreak token": re.compile(r"nbk_[A-Za-z\d_-]{16,}"),
    "jdbc url with password": re.compile(r"jdbc:[a-z]+://[^\s\"']*password=[^&\s\"']+", re.I),
    "url with credentials": re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@\"']+:[^/\s@\"']+@", re.I),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
BENIGN_VALUES = {"", "''", '""', "n/a", "none", "null", "~", "false", "true", "changeme", "[]", "{}"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "*"}


@dataclass(frozen=True)
class Finding:
    path: str        # path relative to the scanned root
    line: int
    key: str         # key name or "" for value-only findings
    reason: str
    value: str       # the suspicious value (never printed by callers)
    start: int       # value span within the line
    end: int


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _value_span(line: str, m: re.Match) -> tuple[int, int, str]:
    """Span (start, end) and text of the assigned value, without quotes / trailing comment."""
    start = m.start("rest")
    rest = m.group("rest")
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        close = rest.find(quote, 1)
        while close != -1 and quote == '"' and rest[close - 1] == "\\":
            close = rest.find(quote, close + 1)
        if close == -1:
            return start, start + len(rest), rest
        return start + 1, start + close, rest[1:close]
    value = re.split(r"\s+#", rest, maxsplit=1)[0].rstrip().rstrip(",")
    return start, start + len(value), value


def _is_benign(value: str) -> bool:
    stripped = TOKEN_RE.sub("", value).strip()
    return stripped.lower() in BENIGN_VALUES


def _public_ips(value: str) -> list[str]:
    ips = []
    for ip in IPV4_RE.findall(value):
        try:
            addr = ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            continue
        if not (addr.is_loopback or addr.is_unspecified):
            ips.append(ip)
    return ips


def scan_text(path: str, text: str) -> list[Finding]:
    fmt = detect_format(path)
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("#", "//", "!")) and fmt != "json":
            continue
        m = ASSIGN_RE.match(line)
        if m:
            key = m.group("key").strip()
            norm = normalize_key(key.split(".")[-1])
            start, end, value = _value_span(line, m)
            if not _is_benign(value) and not TOKEN_RE.fullmatch(value.strip()):
                if SECRET_KEY_RE.search(norm) or norm in SECRET_KEY_EXACT:
                    # Skip message/description keys like "contentInvalidTokenMessage".
                    findings.append(Finding(path, lineno, key, "credential-like key", value, start, end))
                    continue
                if HOST_KEY_RE.search(norm) and value.strip().lower() not in LOCAL_HOSTS:
                    if _public_ips(value) or re.search(r"[a-z0-9-]+\.[a-z]{2,}", value, re.I) or "@" in value:
                        findings.append(Finding(path, lineno, key, "host/address value", value, start, end))
                        continue
        for ip in _public_ips(line):
            idx = line.find(ip)
            findings.append(Finding(path, lineno, m.group("key").strip() if m else "", "IP address", ip, idx, idx + len(ip)))
        for reason, pattern in TOKEN_PATTERNS.items():
            for tm in pattern.finditer(line):
                findings.append(Finding(path, lineno, m.group("key").strip() if m else "", reason, tm.group(0), tm.start(), tm.end()))
    return _dedupe(findings)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, int, int, int]] = set()
    unique = []
    for f in findings:
        k = (f.path, f.line, f.start, f.end)
        if k not in seen:
            seen.add(k)
            unique.append(f)
    return unique


@dataclass(frozen=True)
class AllowEntry:
    path: str
    key: str
    value_sha256: str  # first 12 hex chars: a changed value is flagged again
    reason: str


def value_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_scan_allowlist() -> list[AllowEntry]:
    """Reviewed false positives: {path (relative to Minecraft/), key, value_sha256, reason}."""
    if not SCAN_ALLOWLIST_FILE.is_file():
        return []
    data = load_yaml(SCAN_ALLOWLIST_FILE) or {}
    entries = []
    problems = []
    for idx, item in enumerate(data.get("allow") or []):
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item.get(k).strip() for k in ("path", "key", "value_sha256", "reason")):
            problems.append(f"entry {idx}: needs non-empty 'path', 'key', 'value_sha256' and 'reason'")
            continue
        entries.append(AllowEntry(item["path"], item["key"], item["value_sha256"], item["reason"]))
    fail_if(problems, f"{SCAN_ALLOWLIST_FILE.name} is invalid:")
    return entries


def filter_allowed(findings: list[Finding], allow: list[AllowEntry], path_prefix: str = "") -> list[Finding]:
    allowed = {(a.path, a.key, a.value_sha256) for a in allow}
    return [f for f in findings if (path_prefix + f.path, f.key, value_fingerprint(f.value)) not in allowed]


def scan_files(root: Path, rel_paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in rel_paths:
        data = (root / rel).read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary file; gitleaks still covers it
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise McDeployError(f"{rel}: not valid UTF-8 text") from None
        findings += scan_text(rel, text)
    return findings
