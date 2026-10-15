"""Console access via netmiko through an OpenGear terminal server.

Two phases per session:

  RAW — a fresh connection makes no assumptions about what's on the serial
  line (setup dialog, boot gate, login prompt). Drive it with write()/
  send_return()/expect().

  PROMOTED — once a device prompt is reachable, promote() redispatches the
  session to the platform driver's netmiko personality (cisco_ios today):
  send_command()/send_config_set() with command-echo stripping, paging
  disabled (terminal length 0), and prompt-anchored reads. Raw channel ops
  stay available for interactive state machines (image copy, RSA keygen,
  PKI dialogs, microcode reload).

The NOS-specific personality (prompt regexes, promotion target, persist
semantics, device classification) lives in jobs/drivers/; this module
re-exports the Cisco names steps have always imported.
"""

import hashlib
import io
import re
import time
from urllib.parse import urlparse

import paramiko
import requests
from netmiko import ConnectHandler, redispatch

from nautobot.dcim.models import ConsolePort
from nautobot.extras.models import SecretsGroupAssociation

from django.core.cache import cache

from ..app_settings import get_settings
from . import drivers

# The Cisco personality now lives in the driver package; these re-exports keep
# the import surface the step modules have always used ("from ..console import
# EXEC_PROMPT, ..."). The Cisco step family may import them freely — other
# NOS families get their own driver + step modules.
from .drivers.cisco_ios import (  # noqa: F401
    ANY_PROMPT,
    CFG_PROMPT,
    DBPERSIST_ERROR,
    EXEC_PROMPT,
    clear_dbpersist_db,
    dbpersist_bug_logged,
    dbpersist_log_count,
)


class ConsoleSessionLost(RuntimeError):
    """Raised when the console session drops unexpectedly."""


# Config/CLI lines whose trailing value is a secret. Mirrors config_push's
# log redaction; applied to every transcript line (config echo, running-config).
SECRET_KEYWORD_RE = re.compile(
    r"(?i)\b(password|secret|key-string|key|community|passphrase|pre-shared-key)\b\s+\S.*$"
)


# CSI sequences (ESC [ ... final), then any other ESC sequence (ESC, optional
# intermediates 0x20-0x2F, final 0x30-0x7E) — covers ESC ( B, ESC c, etc.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[0-~]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Transcript:
    """Per-session console transcript, handed to the job log one step at a time.

    netmiko's session log (channel reads AND writes) lands in an in-memory
    buffer that is shared across console reconnects; `take()` returns what
    arrived since the previous take, with every registered secret and every
    secret-keyword line masked. It lives only in the job log, so it is
    removed with the JobResult by every deletion path — including the
    cleanup system job's raw delete, which would orphan a JobResult file.
    """

    STEP_CAP = 64 * 1024  # characters kept per step (the tail)
    MIN_SECRET_LEN = 4    # don't mask trivially short strings

    def __init__(self):
        self.buffer = io.BytesIO()
        self._secrets = set()
        self._session_logs = []

    def attach(self, conn):
        """Register a netmiko connection whose session log feeds the buffer."""
        slog = getattr(conn, "session_log", None)
        if slog is None:
            return
        self._session_logs.append(slog)
        for secret in self._secrets:
            slog.no_log[secret] = secret

    def register_secret(self, value):
        """Mask `value` in everything taken from now on (netmiko masks it at
        flush time too, so a partial read can't leak it)."""
        value = str(value or "")
        if len(value) < self.MIN_SECRET_LEN:
            return
        self._secrets.add(value)
        for slog in self._session_logs:
            slog.no_log[value] = value

    def take(self):
        """Everything recorded since the last take, redacted; empties the buffer."""
        for slog in self._session_logs:
            try:
                slog.flush()
            except Exception:
                pass
        raw = self.buffer.getvalue()
        self.buffer.seek(0)
        self.buffer.truncate(0)
        return self.redact(self.clean(raw))

    @staticmethod
    def clean(raw):
        """Bytes off the serial line -> text PostgreSQL will store.

        Boot output carries NUL bytes, ANSI escapes and stray control
        characters; a NUL anywhere in the message makes the JobLogEntry
        insert fail (seen on the first bench run: the reload step's entry was
        dropped with a "Logging error" traceback). Keep newlines and tabs,
        drop everything else below 0x20 plus DEL.
        """
        text = raw.decode("utf-8", errors="replace")
        text = _ANSI_ESCAPE_RE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return _CONTROL_CHAR_RE.sub("", text)

    def redact(self, text):
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "********")
        return "\n".join(
            SECRET_KEYWORD_RE.sub(lambda m: f"{m.group(1)} ********", line) for line in text.split("\n")
        )

    @classmethod
    def clip(cls, text, cap=None):
        cap = cap or cls.STEP_CAP
        if len(text) <= cap:
            return text
        return f"[… {len(text) - cap} earlier characters omitted …]\n" + text[-cap:]


class Console:
    """A netmiko-backed console session through the OpenGear direct port."""

    def __init__(self, console_path, username, password, logger, driver=None, transcript=None):
        settings = get_settings()
        base_port = settings.get("console_port_base", 3000)
        self.host = console_path["host"]
        self.port = base_port + console_path["port_number"]
        self._logger = logger
        # Personality (prompt regexes, promotion target, persist semantics)
        # comes from the platform driver; default is Cisco IOS.
        self.driver = driver or drivers.default_driver()
        self.is_ios = False  # promoted to the driver's netmiko personality
        self._pending = ""   # channel bytes read but not yet consumed by expect()
        self._tail = ""      # last relevant output, for diagnostics
        self.transcript = transcript

        transcript_kwargs = {}
        if transcript is not None:
            transcript_kwargs = {
                "session_log": transcript.buffer,
                "session_log_record_writes": True,
            }

        logger.info(f"Opening console session to {self.host}:{self.port} (terminal server)")
        try:
            self.conn = ConnectHandler(
                device_type="terminal_server",
                host=self.host,
                port=self.port,
                username=username,
                password=password,
                # Console gates ("Press RETURN to get started") only respond to
                # a carriage return — never a bare LF.
                default_enter="\r",
                timeout=30,
                auth_timeout=30,
                banner_timeout=30,
                **transcript_kwargs,
            )
        except Exception as e:
            raise ConnectionError(
                f"Console connection to {self.host}:{self.port} failed: {e}"
            ) from e
        if transcript is not None:
            transcript.attach(self.conn)

    def register_secret(self, value):
        """Mask `value` in the console transcript (no-op without one)."""
        if self.transcript is not None:
            self.transcript.register_secret(value)

    # ------------------------------------------------------------------ raw
    def _read(self):
        try:
            return self.conn.read_channel()
        except Exception as e:
            raise ConsoleSessionLost(f"Console session lost while reading: {e}") from e

    def write(self, data):
        try:
            self.conn.write_channel(data)
        except Exception as e:
            raise ConsoleSessionLost(f"Console session lost while writing: {e}") from e

    def sendline(self, line=""):
        self.write(f"{line}\r")

    def send_return(self):
        """Press Enter the way a human does — a bare CR."""
        self.write("\r")

    def drain(self, quiet=0.5, max_wait=20):
        """Consume pending output (async syslog, stale command output) until the
        channel has been quiet for `quiet` seconds. Returns what was drained."""
        buf, self._pending = self._pending, ""
        start = last_data = time.time()
        while time.time() - start < max_wait:
            chunk = self._read()
            if chunk:
                buf += chunk
                last_data = time.time()
            elif time.time() - last_data >= quiet:
                break
            else:
                time.sleep(0.1)
        return buf

    def expect(self, patterns, timeout=30, where=""):
        """Wait for the earliest regex match in channel output.

        Returns (index, text_before_match); the match itself is consumed and
        anything after it is retained for the next call. On timeout returns
        (None, everything_seen) — callers decide whether that's fatal. Raises
        ConsoleSessionLost if the transport dies.
        """
        compiled = [re.compile(p, re.MULTILINE) for p in patterns]
        deadline = time.time() + timeout
        while True:
            best = None
            for i, rx in enumerate(compiled):
                m = rx.search(self._pending)
                if m and (best is None or m.start() < best[1].start()):
                    best = (i, m)
            if best:
                idx, m = best
                before = self._pending[: m.start()]
                self._pending = self._pending[m.end():]
                self._tail = before[-400:]
                return idx, before
            if time.time() >= deadline:
                self._tail = self._pending[-400:]
                return None, self._pending
            chunk = self._read()
            if chunk:
                self._pending += chunk
            else:
                time.sleep(0.1)

    def tail(self, n=200):
        """Last output seen, cleaned for error messages — a timeout raise
        without this hides what the console was actually showing."""
        return " ".join((self._tail or "")[-n:].split()) or "(no output received)"

    # ---------------------------------------------------------------- exec
    def promote(self):
        """Redispatch to the driver's netmiko personality once a device
        prompt is reachable.

        Netmiko's session preparation learns the prompt, sets terminal width,
        and disables --More-- paging (terminal length 0). Re-run after any
        reload via a fresh Console + a new promote."""
        self._pending = ""
        redispatch(self.conn, device_type=self.driver.netmiko_device_type)
        self.is_ios = True

    # Historical name from before the driver seam — the Cisco step family
    # still calls this.
    promote_to_ios = promote

    def send_command(self, *args, **kwargs):
        self._ensure_exec_mode()
        return self.conn.send_command(*args, **kwargs)

    def _ensure_exec_mode(self):
        """Self-heal a session stranded in config mode before an exec command.

        Netmiko's exit_config_mode has a SILENT failure path: it first runs
        check_config_mode, and if that read is corrupted it concludes "not in
        config mode", skips sending 'end', and returns success — leaving every
        later exec command erroring '% Invalid input' at (config)#. Seen live
        on an IE-3300 after time_sync. One newline round-trip per exec command
        is cheap insurance. send_command is exec-only in this app (all config
        work goes through send_config_set or raw expect machines), so forcing
        'end' here can never break an intentional config session.
        """
        if not self.is_ios:
            return
        try:
            # Drain first: check_config_mode reads the channel, and stale
            # buffered echoes from a prior config session contain "(config)#",
            # making it report config mode on a session that already exited.
            self.drain(quiet=0.3, max_wait=2)
            if self.conn.check_config_mode():
                self._logger.warning("Console was left in config mode — sending 'end' before the exec command")
                self.conn.exit_config_mode()
        except Exception:
            pass  # never let the guard mask the real command; errors surface there

    def send_command_timing(self, *args, **kwargs):
        return self.conn.send_command_timing(*args, **kwargs)

    def send_config_set(self, *args, **kwargs):
        result = self._with_config_mode_retry(lambda: self.conn.send_config_set(*args, **kwargs))
        if kwargs.get("exit_config_mode", True):
            self._verify_config_exit()
        return result

    def _verify_config_exit(self):
        """Netmiko's auto-exit can silently no-op (see _ensure_exec_mode) —
        trust but verify: if the session is still in config mode after a
        send_config_set that was supposed to exit, force the 'end' now."""
        try:
            # Same stale-buffer trap as config-mode ENTRY (see
            # _with_config_mode_retry): the just-finished config session
            # leaves "(config)#" echoes in the channel, and an undrained
            # check_config_mode reads those instead of the live prompt —
            # which made this guard warn and force 'end' on EVERY healthy
            # send_config_set. Drain so the check reflects reality; the
            # warning then only fires on genuine stuck sessions.
            self.drain(quiet=0.3, max_wait=2)
            if self.conn.check_config_mode():
                self._logger.warning("send_config_set returned with the session still in config mode — forcing 'end'")
                self.conn.exit_config_mode()
        except Exception:
            pass

    def _with_config_mode_retry(self, fn, attempts=5, delay=8):
        """Retry when the device refuses `configure terminal`.

        Netmiko raises ValueError('Failed to enter configuration mode.') with
        no device output attached. On factory SWITCHES this is routine, not
        fatal: the PnP agent keeps running after the setup dialog is declined
        and periodically holds the exclusive config lock, and boot-time
        %LINK/%LINEPROTO spam (dozens of ports) can corrupt the config-prompt
        read. Both clear on their own within seconds — retry with backoff,
        and if it never clears, raise WITH the output the console actually
        showed. Only config-mode-ENTRY failures are retried (nothing has been
        applied yet, so a retry is safe); everything else propagates.
        """
        tails = []
        # Clean channel first: netmiko verifies config entry by reading until
        # the FIRST >/# — a stale prompt echo in the buffer makes it declare
        # failure while the device is already sitting at (config)#.
        self.drain(quiet=0.3, max_wait=2)
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except ValueError as e:
                # ONLY entry failures — netmiko's exit failure ("Failed to
                # exit configuration mode") must not re-send the whole set.
                if "enter configuration mode" not in str(e).lower():
                    raise
                leftover = " ".join(self.drain(quiet=0.5, max_wait=3)[-300:].split())
                if leftover:
                    tails.append(leftover)
                if attempt == attempts:
                    raise RuntimeError(
                        f"Failed to enter configuration mode after {attempts} attempts — "
                        f"console showed: {' | '.join(tails) or '(no output captured)'}"
                    ) from e
                # First failure is usually the stale-prompt misread — the
                # drain above already resynced, retry immediately. Waiting
                # only helps real config locks (PnP), so sleep from #2 on.
                if attempt == 1:
                    self._logger.warning(
                        f"Device refused config mode (attempt 1/{attempts}"
                        f"{' — console showed: ' + leftover if leftover else ''}) — resyncing and retrying"
                    )
                else:
                    self._logger.warning(
                        f"Device refused config mode (attempt {attempt}/{attempts}"
                        f"{' — console showed: ' + leftover if leftover else ''}) — retrying in {delay}s"
                    )
                    time.sleep(delay)
                self.send_return()
                self.drain(quiet=0.3, max_wait=2)

    def sync_prompt(self):
        """Re-learn the device prompt. Netmiko caches the base prompt at
        promotion time; a later `hostname` change leaves that cache stale, so
        every subsequent config_mode() waits for the OLD hostname and times
        out. Call this right after any command that changes the hostname."""
        self._pending = ""
        return self.conn.set_base_prompt()

    def config_mode(self):
        return self._with_config_mode_retry(self.conn.config_mode)

    def exit_config_mode(self):
        return self.conn.exit_config_mode()

    def find_prompt(self):
        return self.conn.find_prompt()

    def save_config(self, **kwargs):
        """Persist the running config — semantics belong to the platform
        driver (Cisco: write memory with the IOS-XE DBPERSIST self-heal;
        a future PAN-OS driver: commit + job poll). `verify_contains=<text>`
        asks the driver to prove the save by reading the saved config back."""
        return self.driver.save_config(self, **kwargs)

    def is_alive(self):
        try:
            return self.conn.is_alive()
        except Exception:
            return False

    def close(self):
        try:
            self.conn.disconnect()
        except Exception:
            pass


def connect_console(console_path, username, password, logger, driver=None, transcript=None):
    """Open a RAW console session (no assumptions about device state)."""
    return Console(console_path, username, password, logger, driver=driver, transcript=transcript)


def resolve_console_path(device):
    """Follow a device's console port cable trace to find the OpenGear endpoint.

    Returns:
        dict: console_server (Device), port_number (int), host (str)
    """
    console_ports = ConsolePort.objects.filter(device=device)
    if not console_ports.exists():
        raise ValueError(f"{device.name} has no console ports in Nautobot")

    console_port = console_ports.first()
    if not console_port.cable:
        raise ValueError(f"{device.name} console port '{console_port.name}' has no cable")

    if console_port.cable.termination_a == console_port:
        far_end = console_port.cable.termination_b
    else:
        far_end = console_port.cable.termination_a

    if not hasattr(far_end, "device"):
        raise ValueError(f"Cable from {device.name} console terminates at unexpected endpoint: {far_end}")

    console_server = far_end.device
    port_number = _parse_port_number(far_end.name)
    if port_number is None:
        raise ValueError(f"Cannot parse port number from console server port name: '{far_end.name}'")

    mgmt_ip = _get_management_ip(console_server)

    return {
        "console_server": console_server,
        "port_number": port_number,
        "host": mgmt_ip,
    }


_LETTER_PORT_MAP = {chr(ord("A") + i): 17 + i for i in range(8)}  # A=17 .. H=24
_LETTER_PORT_MAP["UPPER"] = 25
_LETTER_PORT_MAP["LOWER"] = 26


def _parse_port_number(port_name):
    """Extract port number from names like '1', 'port1', 'Port 1', 'serial0/1', 'ttyS1', 'A', 'USB-A', 'Front Upper USB'."""
    upper = port_name.strip().upper()

    if upper in _LETTER_PORT_MAP:
        return _LETTER_PORT_MAP[upper]

    # Match whole words only: "Front Upper USB" is UPPER (25), not a name ending in the letter B (18).
    tokens = [t for t in re.split(r"[^A-Z0-9]+", upper) if t]
    for word in ("UPPER", "LOWER"):
        if word in tokens:
            return _LETTER_PORT_MAP[word]
    letters = [t for t in tokens if len(t) == 1 and t in _LETTER_PORT_MAP]
    if letters:
        return _LETTER_PORT_MAP[letters[-1]]
    # A trailing port letter glued to a number ("port1A"), never the last letter of a word ("USB").
    match = re.search(r"(?<![A-Z])([A-H])$", upper)
    if match:
        return _LETTER_PORT_MAP[match.group(1)]

    match = re.search(r"(\d+)\s*$", port_name)
    if match:
        return int(match.group(1))
    match = re.search(r"/(\d+)", port_name)
    if match:
        return int(match.group(1))
    return None


def _get_management_ip(device):
    if device.primary_ip4:
        return str(device.primary_ip4.host)
    if device.primary_ip6:
        return str(device.primary_ip6.host)
    raise ValueError(f"Console server {device.name} has no management IP")


def get_ei_creds(external_integration, required=True):
    """Return (username, password) from an ExternalIntegration's SecretsGroup.

    Mirrors the OpenGear _get_console_creds pattern: Generic/username + Generic/password.
    With required=False, a missing SecretsGroup returns ("", "") — for protocols
    that support anonymous access (http/https/tftp), where only the per-image
    ExternalIntegrations need credentials.
    """
    sg = external_integration.secrets_group
    if not sg:
        if not required:
            return "", ""
        raise ValueError(
            f"ExternalIntegration '{external_integration.name}' has no SecretsGroup"
        )
    username = SecretsGroupAssociation.objects.get(
        secrets_group=sg, access_type="Generic", secret_type="username",
    ).secret.get_value()
    password = SecretsGroupAssociation.objects.get(
        secrets_group=sg, access_type="Generic", secret_type="password",
    ).secret.get_value()
    return username, password


def get_ei_host_and_base_path(external_integration):
    """Return (host, base_path) from an ExternalIntegration.

    Prefers extra_config["host"] / extra_config["base_path"]. Falls back to
    parsing remote_url — only the host + path are taken here; the scheme (which
    selects the on-device copy protocol) is read separately by get_ei_scheme.
    """
    extra = external_integration.extra_config or {}
    host = extra.get("host")
    base_path = extra.get("base_path")
    if host and base_path is not None:
        return host, base_path.rstrip("/")

    parsed = urlparse(external_integration.remote_url or "")
    if not parsed.hostname:
        raise ValueError(
            f"ExternalIntegration '{external_integration.name}' has no host — "
            f"set extra_config.host or populate remote_url"
        )
    # Keep a nonstandard port (hostname alone silently drops it); never use
    # netloc, which would drag URL-embedded credentials into copy URLs.
    host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    return host, (parsed.path or "").rstrip("/")


# On-device transfer protocols the IOS `copy` command understands and this
# module knows how to drive interactively.
SUPPORTED_COPY_SCHEMES = ("scp", "ftp", "tftp", "http", "https")


def get_ei_scheme(external_integration, default="scp"):
    """Return the transfer protocol the device should use for this file server.

    Sourced from extra_config["scheme"] (or "protocol"), else the remote_url
    scheme, else `default`. This is what dictates the `copy <scheme>://...`
    form sent to the router — SCP is the default so file servers configured
    before this was protocol-aware keep working unchanged.
    """
    extra = external_integration.extra_config or {}
    scheme = extra.get("scheme") or extra.get("protocol")
    if not scheme:
        scheme = urlparse(external_integration.remote_url or "").scheme
    scheme = (scheme or default).lower()
    if scheme not in SUPPORTED_COPY_SCHEMES:
        raise ValueError(
            f"ExternalIntegration '{external_integration.name}' uses unsupported "
            f"transfer scheme '{scheme}' — use one of: {', '.join(SUPPORTED_COPY_SCHEMES)}"
        )
    return scheme


def list_remote_files(external_integration):
    """Check the file server and return its file listing where possible.

    Scheme-aware: SCP servers get an SFTP listdir (requires SecretsGroup
    credentials); http/https servers get a lightweight reachability probe —
    anonymous unless a SecretsGroup is set — and return None because a
    webserver can't be reliably enumerated (autoindex may be off even though
    individual files are fetchable). ftp/tftp have no cheap probe and also
    return None. None means "unknown" — preflight presence checks skip it.

    Raises on connect/auth failure. Intended for caller-level caching via
    get_file_server_files_cached — don't call this directly in tight loops.
    """
    scheme = get_ei_scheme(external_integration)
    if scheme in ("http", "https"):
        _check_http_reachable(external_integration, scheme)
        return None
    if scheme != "scp":
        return None

    user, password = get_ei_creds(external_integration)
    host, base_path = get_ei_host_and_base_path(external_integration)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        sftp = client.open_sftp()
        try:
            return set(sftp.listdir(base_path or "/"))
        finally:
            sftp.close()
    finally:
        client.close()


def _check_http_reachable(external_integration, scheme):
    """Probe an http(s) file server's base URL. Raises on connect/TLS failure.

    Any HTTP response counts as reachable — a 403/404 on the base path is
    normal when directory listing is disabled but files are still fetchable.
    """
    host, base_path = get_ei_host_and_base_path(external_integration)
    url = f"{scheme}://{host}{base_path or ''}/"
    user, password = get_ei_creds(external_integration, required=False)
    try:
        requests.get(
            url,
            auth=(user, password) if user else None,
            verify=external_integration.verify_ssl,
            timeout=external_integration.timeout or 15,
            stream=True,
        ).close()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"File server '{external_integration.name}' unreachable at {url}: {e}"
        ) from e


# Tri-state result of a per-file presence probe. Only MISSING may block a
# preflight — UNKNOWN (403/405/5xx, network error, unprobeable protocol) must
# never be treated as absence, because the router may still pull the file fine.
FILE_EXISTS = "exists"
FILE_MISSING = "missing"
FILE_UNKNOWN = "unknown"


def build_file_url(external_integration, filename):
    """Absolute URL for a file under the EI's base path."""
    scheme = get_ei_scheme(external_integration)
    host, base_path = get_ei_host_and_base_path(external_integration)
    path = f"{base_path}/{filename}" if base_path else f"/{filename}"
    return f"{scheme}://{host}{path}"


def http_file_status(url, user="", password="", verify=True, timeout=15):
    """HEAD a file URL. 2xx/3xx → exists, 404/410 → missing, else unknown.

    403/405/5xx and network errors are UNKNOWN, not missing — a server that
    forbids HEAD or anonymous browsing can still serve the file to the router.
    """
    try:
        resp = requests.head(
            url,
            auth=(user, password) if user else None,
            verify=verify,
            timeout=(5, timeout),
            allow_redirects=True,
        )
    except requests.exceptions.RequestException:
        return FILE_UNKNOWN
    if resp.status_code in (404, 410):
        return FILE_MISSING
    if resp.status_code < 400:
        return FILE_EXISTS
    return FILE_UNKNOWN


def http_file_status_cached(url, user="", password="", verify=True, timeout=15, ttl=300, unknown_ttl=30):
    """Per-URL cached wrapper around http_file_status.

    Keyed on the URL, not the device — a fleet of devices shares a handful of
    distinct files, so scrolling the device list costs at most one HEAD per
    unique file per TTL window. Unknowns are cached briefly so a transient
    server problem isn't remembered for the full window.
    """
    key = "bootstrap:remote_file:" + hashlib.sha1(url.encode()).hexdigest()
    status = cache.get(key)
    if status is not None:
        return status
    status = http_file_status(url, user=user, password=password, verify=verify, timeout=timeout)
    cache.set(key, status, timeout=unknown_ttl if status == FILE_UNKNOWN else ttl)
    return status


def remote_file_check(external_integration, filename):
    """Best-effort presence check for a file on a file-server EI.

    scp → membership in the (cached) SFTP listing; http/https → cached per-file
    HEAD; ftp/tftp or any probe failure → unknown.
    """
    if external_integration is None or not filename:
        return FILE_UNKNOWN
    try:
        scheme = get_ei_scheme(external_integration)
    except ValueError:
        return FILE_UNKNOWN

    if scheme in ("http", "https"):
        user, password = get_ei_creds(external_integration, required=False)
        return http_file_status_cached(
            build_file_url(external_integration, filename),
            user=user,
            password=password,
            verify=external_integration.verify_ssl,
            timeout=external_integration.timeout or 15,
        )

    if scheme == "scp":
        try:
            files = get_file_server_files_cached(external_integration)
        except Exception:
            return FILE_UNKNOWN
        if files is None:
            return FILE_UNKNOWN
        return FILE_EXISTS if filename in files else FILE_MISSING

    return FILE_UNKNOWN


# Cache marker for "server reachable but no file listing available" — a bare
# None in the cache is indistinguishable from a miss.
_NO_LISTING = "__no_listing__"


def get_file_server_files_cached(external_integration, ttl=60, fail_ttl=15):
    """Cached wrapper around list_remote_files.

    First call per EI within the TTL window does the probe; subsequent calls
    read from Django's cache. Also validates credentials and reachability
    as a side effect — a cache miss that raises tells you the file server is
    down or the creds are wrong. Failures are cached for a shorter window
    (fail_ttl) so a down file server isn't polled on every page click.
    Returns None when the server is reachable but can't be enumerated
    (http/https/ftp/tftp) — callers must treat None as "listing unknown".
    """
    if external_integration is None:
        return set()
    key = f"bootstrap:file_server_files:{external_integration.id}"
    fail_key = key + ":fail"

    cached_failure = cache.get(fail_key)
    if cached_failure is not None:
        raise RuntimeError(cached_failure)

    files = cache.get(key)
    if files is not None:
        return None if files == _NO_LISTING else files

    try:
        files = list_remote_files(external_integration)
    except Exception as e:
        cache.set(fail_key, str(e), timeout=fail_ttl)
        raise

    cache.set(key, _NO_LISTING if files is None else files, timeout=ttl)
    return files


def build_copy_url(scheme, user, password, host, remote_path):
    """Build the `copy` source URL for a given protocol (pure, testable).

    Credential placement follows what each IOS protocol expects:
      - scp:         user in the URL, password sent interactively (never in URL)
      - tftp:        no authentication at all
      - ftp/http(s): credentials embedded in the URL when present
    """
    scheme = (scheme or "scp").lower()
    if scheme not in SUPPORTED_COPY_SCHEMES:
        raise ValueError(
            f"Unsupported image transfer scheme '{scheme}' — "
            f"use one of: {', '.join(SUPPORTED_COPY_SCHEMES)}"
        )
    if scheme == "scp":
        return f"scp://{user}@{host}{remote_path}"
    if scheme == "tftp":
        return f"tftp://{host}{remote_path}"
    if user:
        return f"{scheme}://{user}:{password}@{host}{remote_path}"
    return f"{scheme}://{host}{remote_path}"


def copy_image(console, scheme, user, password, host, remote_path, local_dest, logger, timeout=1800, where="copy"):
    """Drive a Cisco IOS `copy <scheme>://... local_dest` transfer interactively.

    The protocol is dictated by `scheme` (from the file server's configured
    URL/scheme), not hardcoded. Handles the interactive prompts each protocol
    presents — destination filename (all), password (SCP, and any protocol
    that prompts for one), and transfer completion. Raises RuntimeError on
    failure.
    """
    scheme = (scheme or "scp").lower()
    url = build_copy_url(scheme, user, password, host, remote_path)

    log_url = url
    if password and password in log_url:
        log_url = log_url.replace(password, "********")
    logger.info(f"{scheme.upper()} copy: {log_url} -> {local_dest}")
    if password and hasattr(console, "register_secret"):
        console.register_secret(password)

    console.drain(quiet=0.3, max_wait=5)
    console.sendline(f"copy {url} {local_dest}")

    # Prompt-driven state machine. Timeouts are ceilings, not fixed waits. The
    # iteration cap stops a device that re-prompts forever (e.g. bad password).
    for _ in range(8):
        idx, _text = console.expect(
            [
                r"Destination filename",   # 0  accept default with Enter
                r"[Pp]assword:",           # 1  send password (blank if none)
                r"bytes copied",           # 2  success
                r"%Error",                 # 3
                r"Not enough space",       # 4
                r"timed out",              # 5
                r"Authentication failed",  # 6
                r"Permission denied",      # 7
                r"Host key",               # 8
                r"No such file",           # 9
            ],
            timeout=timeout,
            where=f"{where}_xfer",
        )
        if idx == 0:
            console.send_return()
            continue
        if idx == 1:
            console.sendline(password or "")
            continue
        if idx == 2:
            console.expect([EXEC_PROMPT], timeout=15, where=f"{where}_return")
            logger.info(f"{scheme.upper()} transfer complete")
            return
        if idx is None:
            raise RuntimeError(
                f"{scheme.upper()} transfer produced no result within {timeout}s — "
                f"console showed: {console.tail()}"
            )

        # Any explicit error — drain back to the prompt and classify.
        console.expect([EXEC_PROMPT], timeout=15, where=f"{where}_err_return")
        if idx in (6, 7):
            raise RuntimeError(f"{scheme.upper()} authentication failed to {host} — check credentials")
        if idx == 4:
            raise RuntimeError(f"Not enough flash space copying to {local_dest}")
        if idx == 8:
            raise RuntimeError(f"{scheme.upper()} host key rejected for {host}")
        if idx == 5:
            raise RuntimeError(f"{scheme.upper()} transfer to {host} timed out on the device")
        raise RuntimeError(f"{scheme.upper()} transfer failed copying {remote_path} from {host}")

    raise RuntimeError(f"{scheme.upper()} transfer kept re-prompting — aborted after 8 prompts")
