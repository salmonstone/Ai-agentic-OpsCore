"""
Secrets in the OS keychain instead of plaintext .env.

.env held every credential AtlasOS uses — Anthropic key, Jenkins API token,
AWS keys, MCP auth token — as plaintext readable by any process, script, or
backup tool running as this user. `agent secrets migrate` moves them into the
OS credential store (Windows Credential Manager, encrypted with the Windows
login; macOS Keychain; Secret Service on Linux desktops) and leaves .env with
only non-secret configuration.

How values reach the code: `load_into_environ()` runs when the `agent`
package is first imported, before settings load, and copies keychain values
into os.environ. Everything downstream — pydantic Settings, the MCP server,
supervised child processes — sees ordinary environment variables and needs no
change. Precedence: a variable already set in the environment wins (so
Docker / CI can inject their own), then the keychain, then .env.

Deliberately OS-local rather than a cloud secrets manager: the credentials
needed to reach a cloud vault would themselves have to live somewhere, and the
AWS account may not be permanent. A cloud backend can slot in behind the same
load/store functions later.

Where no keychain exists (a Linux container), nothing is loaded and nothing
fails; supply secrets as environment variables, e.g. via
`agent secrets run -- docker compose up -d` on the host.
"""
from __future__ import annotations

import os
import re
import secrets as _stdlib_secrets
from pathlib import Path

SERVICE = "atlasos"
INDEX_KEY = "__index__"          # keyrings can't enumerate, so we keep our own list

# Windows Credential Manager caps a credential blob at 2560 bytes (UTF-16).
MAX_VALUE_CHARS = 1200

_SECRET_WORDS = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|"
                           r"ACCESS_KEY|ROUTING_KEY|SIGNING|CREDENTIALS?)")
_NOT_SECRET_SUFFIX = re.compile(r"_(FILE|PATH|DIR|ENABLED|METHOD|REGION|CHANNEL|MODEL|HOST|PORT|"
                                r"TOKENS|TIMEOUT|TTL|LIMIT|EXPIRY|EXPIRES)$")


class SecretsError(Exception):
    pass


def is_secret_name(name: str) -> bool:
    """Judged by the variable NAME only — values are never inspected."""
    n = name.upper()
    if n.endswith("WEBHOOK_URL"):            # the URL itself is the credential
        return True
    if _NOT_SECRET_SUFFIX.search(n):
        return False
    return bool(_SECRET_WORDS.search(n))


# ---------------------------------------------------------------------------
# keychain access
# ---------------------------------------------------------------------------

class _WinCred:
    """Windows Credential Manager via advapi32, directly.

    The `keyring` package does the same but costs ~400 ms to import (it
    probes every backend), and this lookup runs on every `agent` command.
    Credentials are generic, per-user, persisted locally (not roaming), and
    named "atlasos:<NAME>" — visible in Control Panel > Credential Manager.
    """
    name = "Windows Credential Manager"
    _GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
                ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR),
            ]

        self._ct = ctypes
        self._CRED = CREDENTIAL
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self._read = adv.CredReadW
        self._read.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self._read.restype = wt.BOOL
        self._write = adv.CredWriteW
        self._write.argtypes = [ctypes.POINTER(CREDENTIAL), wt.DWORD]
        self._write.restype = wt.BOOL
        self._delete = adv.CredDeleteW
        self._delete.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
        self._delete.restype = wt.BOOL
        self._free = adv.CredFree
        self._free.argtypes = [ctypes.c_void_p]

    @staticmethod
    def _target(service, key):
        return f"{service}:{key}"

    def get_password(self, service, key):
        ct = self._ct
        p = ct.POINTER(self._CRED)()
        if not self._read(self._target(service, key), self._GENERIC, 0, ct.byref(p)):
            if ct.get_last_error() == self._ERROR_NOT_FOUND:
                return None
            raise OSError(ct.get_last_error(), "CredReadW failed")
        try:
            c = p.contents
            return ct.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
        finally:
            self._free(p)

    def set_password(self, service, key, value):
        ct = self._ct
        blob = value.encode("utf-16-le")
        buf = (ct.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self._CRED()
        cred.Type = self._GENERIC
        cred.TargetName = self._target(service, key)
        cred.UserName = key
        cred.Comment = "AtlasOS secret (agent secrets)"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte))
        cred.Persist = self._PERSIST_LOCAL_MACHINE
        if not self._write(ct.byref(cred), 0):
            raise OSError(ct.get_last_error(), "CredWriteW failed")

    def delete_password(self, service, key):
        if not self._delete(self._target(service, key), self._GENERIC, 0):
            if self._ct.get_last_error() != self._ERROR_NOT_FOUND:
                raise OSError(self._ct.get_last_error(), "CredDeleteW failed")


def _keyring():
    """A backend with get/set/delete_password(service, key), or None."""
    if os.getenv("ATLASOS_NO_KEYRING") == "1":
        return None
    try:
        if os.name == "nt":
            return _WinCred()
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def available() -> bool:
    return _keyring() is not None


def backend_name() -> str:
    kr = _keyring()
    if kr is None:
        return "none"
    return getattr(kr, "name", None) or type(kr.get_keyring()).__name__


def stored_names() -> list[str]:
    kr = _keyring()
    if not kr:
        return []
    raw = kr.get_password(SERVICE, INDEX_KEY) or ""
    return sorted({n for n in raw.split(",") if n})


def _write_index(kr, names: set[str]) -> None:
    if names:
        kr.set_password(SERVICE, INDEX_KEY, ",".join(sorted(names)))
    else:
        try:
            kr.delete_password(SERVICE, INDEX_KEY)
        except Exception:
            pass


def get(name: str) -> str | None:
    kr = _keyring()
    return kr.get_password(SERVICE, name) if kr else None


def store(name: str, value: str) -> None:
    """Store and read back; raises if the keychain did not keep it exactly."""
    kr = _keyring()
    if not kr:
        raise SecretsError("no OS keychain available on this machine")
    if not value:
        raise SecretsError(f"{name}: refusing to store an empty value")
    if len(value) > MAX_VALUE_CHARS:
        raise SecretsError(f"{name}: value too long for the OS keychain ({len(value)} chars)")
    kr.set_password(SERVICE, name, value)
    if kr.get_password(SERVICE, name) != value:
        raise SecretsError(f"{name}: keychain read-back did not match; not trusting it")
    _write_index(kr, set(stored_names()) | {name})


def delete(name: str) -> bool:
    kr = _keyring()
    if not kr:
        raise SecretsError("no OS keychain available on this machine")
    existed = name in stored_names()
    try:
        kr.delete_password(SERVICE, name)
    except Exception:
        pass
    _write_index(kr, set(stored_names()) - {name})
    return existed


def generate_token() -> str:
    return _stdlib_secrets.token_urlsafe(32)


def load_into_environ(environ: dict | None = None) -> list[str]:
    """Copy keychain secrets into the environment; never overrides a set var.

    Must never raise: it runs on package import, and a broken keychain must
    degrade to ".env still works", not "nothing starts".
    """
    env = os.environ if environ is None else environ
    loaded = []
    try:
        kr = _keyring()
        if not kr:
            return []
        for name in stored_names():
            if name in env:
                continue
            value = kr.get_password(SERVICE, name)
            if value:
                env[name] = value
                loaded.append(name)
    except Exception:
        return loaded
    return loaded


# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
MOVED_MARKER = "# {name} is stored in the OS keychain (agent secrets status)"


def env_secret_names(env_path: Path) -> list[str]:
    """Names of secret-looking variables that have a value in the .env file."""
    if not env_path.exists():
        return []
    from dotenv import dotenv_values
    return [k for k, v in dotenv_values(env_path).items() if v and is_secret_name(k)]


def migrate(env_path: Path) -> list[str]:
    """Move secret values from .env into the keychain; returns the names moved.

    Every value is stored and read back before .env is touched. .env is then
    rewritten in one atomic replace: moved lines become a comment, every other
    line (comments, non-secret settings, ordering) is kept byte-for-byte.
    """
    if not available():
        raise SecretsError("no OS keychain available on this machine")
    if not env_path.exists():
        raise SecretsError(f"{env_path} not found")
    from dotenv import dotenv_values
    values = dotenv_values(env_path)
    names = [k for k, v in values.items() if v and is_secret_name(k)]
    for name in names:
        store(name, values[name])

    moved = set(names)
    out = []
    for line in env_path.read_text(encoding="utf-8").splitlines(keepends=True):
        m = _LINE.match(line)
        if m and m.group(1) in moved:
            out.append(MOVED_MARKER.format(name=m.group(1)) + "\n")
        else:
            out.append(line)
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    os.replace(tmp, env_path)
    return names


def restore_to_env(env_path: Path, names: list[str] | None = None) -> list[str]:
    """Undo migrate: write keychain secrets back into .env, then remove them
    from the keychain. Each marker comment is replaced in place; secrets
    without a marker are appended."""
    kr = _keyring()
    if not kr:
        raise SecretsError("no OS keychain available on this machine")
    names = names or stored_names()
    values = {n: kr.get_password(SERVICE, n) for n in names}
    values = {n: v for n, v in values.items() if v}
    if not values:
        return []

    def fmt(n, v):
        if re.fullmatch(r"[A-Za-z0-9_./:@+=-]*", v):
            return f"{n}={v}\n"
        if "'" not in v:
            return f"{n}='{v}'\n"            # single quotes: dotenv takes them literally
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'{n}="{escaped}"\n'

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True) if env_path.exists() else []
    placed = set()
    out = []
    for line in lines:
        hit = next((n for n in values if line.strip() == MOVED_MARKER.format(name=n)), None)
        if hit:
            out.append(fmt(hit, values[hit]))
            placed.add(hit)
        else:
            out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out += [fmt(n, v) for n, v in values.items() if n not in placed]
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    os.replace(tmp, env_path)
    for n in values:
        delete(n)
    return list(values)
