"""Download the validated OS image for this device type and set the boot statement."""

import re
from urllib.parse import urlparse

from nautobot.dcim.models import SoftwareImageFile

from ..console import (
    copy_image,
    get_ei_creds,
    get_ei_host_and_base_path,
    get_ei_scheme,
    http_file_status_cached,
    remote_file_check,
    FILE_MISSING,
    SUPPORTED_COPY_SCHEMES,
)

_BOOT_ERROR = re.compile(r"(?m)^\s*%\s*(Error|Invalid).*$|[Nn]o such file", re.IGNORECASE)


def _resolve_os_transfer(image, file_server_ei):
    """Decide how the device pulls this OS image: (scheme, host, remote_path, user, password).

    Prefers the image's OWN download_url (scheme/host/path) and its OWN
    ExternalIntegration's SecretsGroup for credentials. Credential precedence
    on the URL path: image's external_integration -> credentials embedded in
    the URL -> the job's file server EI. When the image has no download_url,
    falls back entirely to the job's file server EI (the pre-per-image
    behavior).
    """
    if image.download_url:
        parsed = urlparse(image.download_url)
        scheme = (parsed.scheme or "scp").lower()
        if scheme not in SUPPORTED_COPY_SCHEMES:
            raise RuntimeError(
                f"Image '{image.image_file_name}' download_url scheme '{scheme}' is not a "
                f"device-pullable protocol ({', '.join(SUPPORTED_COPY_SCHEMES)})"
            )
        if not parsed.hostname:
            raise RuntimeError(f"Image '{image.image_file_name}' download_url has no host")

        if image.external_integration and image.external_integration.secrets_group:
            user, password = get_ei_creds(image.external_integration)
        elif parsed.username:
            user, password = parsed.username, parsed.password or ""
        elif file_server_ei is not None and file_server_ei.secrets_group:
            user, password = get_ei_creds(file_server_ei)
        else:
            user, password = "", ""
        # download_url path already ends in the file name — use it verbatim.
        return scheme, parsed.hostname, parsed.path, user, password

    if file_server_ei is None:
        raise RuntimeError(
            f"Image '{image.image_file_name}' has no download_url and no file server "
            f"ExternalIntegration is configured"
        )
    scheme = get_ei_scheme(file_server_ei)
    user, password = get_ei_creds(file_server_ei, required=(scheme == "scp"))
    host, base_path = get_ei_host_and_base_path(file_server_ei)
    remote_path = f"{base_path}/{image.image_file_name}" if base_path else f"/{image.image_file_name}"
    return scheme, host, remote_path, user, password


def preflight(device, settings):
    """Return blockers only — runtime advisories (no checksum, no file size) are
    surfaced as log warnings during run() since they only degrade verification,
    not the upgrade itself.
    """
    issues = []
    try:
        image, _ = _resolve_image(device)
    except RuntimeError as e:
        return [str(e)]
    if image is None:
        return issues

    if image.download_url:
        # Per-image URL — validate it's device-pullable; the file server EI is
        # not required in this path.
        parsed = urlparse(image.download_url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in SUPPORTED_COPY_SCHEMES:
            issues.append(
                f"Image download_url scheme '{scheme}' is not a device-pullable "
                f"protocol ({', '.join(SUPPORTED_COPY_SCHEMES)})"
            )
        elif not parsed.hostname:
            issues.append("Image download_url has no host")
        elif scheme in ("http", "https"):
            user = password = ""
            verify, timeout = True, 15
            if image.external_integration:
                user, password = get_ei_creds(image.external_integration, required=False)
                verify = image.external_integration.verify_ssl
                timeout = image.external_integration.timeout or 15
            status = http_file_status_cached(
                image.download_url, user=user, password=password, verify=verify, timeout=timeout
            )
            if status == FILE_MISSING:
                issues.append(
                    f"Image '{image.image_file_name}' not found at its download_url (HTTP 404)"
                )
        return issues

    # No per-image URL — fall back to the job's file server.
    if settings.get("file_server_ei") is None:
        issues.append("No file server ExternalIntegration selected and image has no download_url")
    elif remote_file_check(settings["file_server_ei"], image.image_file_name) == FILE_MISSING:
        issues.append(f"Image '{image.image_file_name}' not found on the file server")
    return issues


def run(child, device, session, settings, logger):
    """Download the target OS image to flash and configure the boot system statement."""
    settings.setdefault("reload_required", False)

    image, source = _resolve_image(device)
    if image is None:
        logger.info("No validated software image assigned to this device type — skipping")
        return "No validated software image assigned — skipped"

    image_file = image.image_file_name
    target_version = image.software_version.version
    logger.info(f"Target: {target_version} — {image_file} ({source})")

    if not image.image_file_size:
        logger.warning(f"Image '{image_file}' has no file size in Nautobot — flash space check will be skipped")
    if not image.image_file_checksum:
        logger.warning(f"Image '{image_file}' has no checksum in Nautobot — verification will be skipped")

    running = _running_version(child, logger)
    already_running = running is not None and _normalize_version(running) == _normalize_version(target_version)
    if already_running:
        logger.info(f"Running version {running} matches target")
    boot_ok = False
    if already_running:
        # Right version, but don't trust the boot config that came with it —
        # a factory unit shipped on the target version can still carry a
        # missing/stale boot statement that reverts it on the next reload
        # (and fails the validate gate).
        stmt = _boot_statement_references(child, image_file)
        # stmt None = NO boot system line in running config. That is NOT
        # skippable on routers: factory units ship with the BOOT variable
        # pre-set by manufacturing, but the variable is re-derived from
        # config at the next save — an absent statement is one 'wr mem'
        # away from an empty boot config. Absent therefore means SET IT.
        # Platforms that never render the statement (IE-3300) just get a
        # redundant, harmless re-set; validate's tri-state check keeps
        # them passing.
        boot_ok = stmt is True and _bootvar_references(child, image_file)

    # The image FILE must be on flash regardless of what's running — a boot
    # config that references a file missing from flash strands the device on
    # its next reload, and `boot system` itself is rejected for absent files.
    on_flash = _image_on_flash(child, image_file, logger)

    if already_running and boot_ok and on_flash:
        logger.info(
            f"Already on validated software ({target_version}), boot config references "
            f"{image_file}, and the image is on flash — nothing to do"
        )
        return f"Already on {target_version}; boot config and image verified"

    if already_running:
        missing = []
        if not boot_ok:
            missing.append(
                "no boot system statement in running config"
                if stmt is None
                else f"boot config does not reference {image_file}"
            )
        if not on_flash:
            missing.append("image file is not on flash")
        logger.info(
            f"Already on validated software ({target_version}) but {' and '.join(missing)} — correcting"
        )

    if on_flash:
        logger.info(f"{image_file} already on flash — skipping download")
    else:
        if image.image_file_size:
            _check_flash_space(child, image.image_file_size, image_file, logger)

        scheme, host, remote_path, user, password = _resolve_os_transfer(
            image, settings.get("file_server_ei")
        )
        src = "image download_url" if image.download_url else "file server"
        logger.info(f"Pulling {image_file} from {src} ({scheme}://{host})")

        copy_image(
            child,
            scheme=scheme,
            user=user,
            password=password,
            host=host,
            remote_path=remote_path,
            local_dest=f"flash:{image_file}",
            logger=logger,
            timeout=1800,
            where="os_copy",
        )

        _verify_image(child, image, logger)

    _set_boot_statement(child, image_file, logger)

    child.drain(quiet=0.3, max_wait=3)  # clear any post-config %SYS syslog before netmiko re-detects the prompt
    # Prove the save by reading the boot statement back from startup-config
    # rather than trusting the DBPERSIST syslog heuristic: an IR1101 that
    # logs an unrelated DMI_STATE_DB persist failure on every `write mem`
    # failed this step while its startup-config was fine.
    child.save_config(verify_contains=image_file)
    if already_running:
        logger.info(f"Boot system set to {image_file} — already running {target_version}, no reload needed")
        return f"Already on {target_version}; boot config/image corrected"
    settings["reload_required"] = True
    logger.info(f"Boot system set to {image_file} — takes effect on next reload")
    return f"{image_file} staged; {running or 'unknown'} → {target_version} on reload"


def _normalize_version(text):
    """Strip leading zeros from dotted version segments: '17.09.04a' == '17.9.4a'.

    IOS-XE prints both zero-padded and unpadded forms of the same version in
    `show version`, and Nautobot may store either — compare both normalized.
    """
    return re.sub(r"\b0+(\d)", r"\1", text.strip().lower())


# The IOS-XE version lines ("Cisco IOS XE Software, Version 17.09.04a") come
# first in `show version`, before other Version-bearing prose (e.g. the GPL
# notice's "Version 2.0") — so the FIRST match is the running version.
_SHOW_VERSION_PATTERN = re.compile(r"Version\s+([0-9]+\.[0-9][A-Za-z0-9.()]*)")


def _parse_running_version(output):
    """Extract the running version from `show version | include Version`
    output, or None if no version line parsed."""
    for line in output.splitlines():
        if "GPL" in line:
            continue
        match = _SHOW_VERSION_PATTERN.search(line)
        if match:
            return match.group(1).rstrip(",")
    return None


def _running_version(child, logger):
    """The parsed running version from `show version`, or None."""
    output = child.send_command("show version | include Version", read_timeout=30)
    running = _parse_running_version(output)
    if running is None:
        logger.warning("Couldn't parse a running version from 'show version' — proceeding with upgrade")
    return running


def _running_target_version(child, target_version, logger):
    """Check if the device is already running the target software version.

    Exact match on the parsed version (after zero-padding normalization) — a
    substring test would let 17.15.2 match 17.15.20 or 17.15.2a and silently
    skip a real upgrade.
    """
    output = child.send_command("show version | include Version", read_timeout=30)
    running = _parse_running_version(output)
    if running is None:
        logger.warning("Couldn't parse a running version from 'show version' — proceeding with upgrade")
        return False
    if _normalize_version(running) == _normalize_version(target_version):
        logger.info(f"Running version {running} matches target")
        return True
    logger.info(f"Running {running}, target {target_version} — upgrade needed")
    return False


def _dlm_validated_versions(device):
    """Currently-valid DLM ValidatedSoftwareLCM software versions applicable to
    this device, or None when the Device Lifecycle Management app isn't
    installed. An empty list means DLM is installed but nothing validated
    applies to this device."""
    try:
        from nautobot_device_lifecycle_mgmt.models import ValidatedSoftwareLCM
    except ImportError:
        return None
    entries = ValidatedSoftwareLCM.objects.get_for_object(device)
    return [entry.software for entry in entries if entry.valid]


def _resolve_image(device):
    """Find the target software image for this device.

    Returns (image, source_note) — the note says how the target was chosen so
    the job log shows WHY this version is the target. DLM validated software
    is the source of truth when present; otherwise the device type's image
    assignments. Raises RuntimeError instead of guessing when the data is
    ambiguous: an unordered arbitrary pick here could land on an old,
    still-Active image and silently skip a real upgrade.
    """
    images = SoftwareImageFile.objects.filter(
        device_types=device.device_type,
        software_version__status__name__in=["Active", "Validated"],
    )
    if not images.exists():
        return None, None

    validated = _dlm_validated_versions(device)
    if validated:
        candidates = images.filter(software_version__in=[v.pk for v in validated])
        if not candidates.exists():
            versions = ", ".join(sorted(v.version for v in validated))
            raise RuntimeError(
                f"DLM validated software for this device ({versions}) has no "
                f"SoftwareImageFile assigned to device type {device.device_type.model}"
            )
        if candidates.count() == 1:
            return candidates.first(), "DLM validated software"
        default = candidates.filter(default_image=True).first()
        if default:
            return default, "DLM validated software, default image"
        versions = ", ".join(sorted(c.software_version.version for c in candidates))
        raise RuntimeError(
            f"Multiple DLM-validated images for device type {device.device_type.model} "
            f"({versions}) and none flagged default_image — flag one to disambiguate"
        )

    # No DLM installed, or nothing validated applies to this device — fall
    # back to the device type's image assignments, but only when unambiguous.
    default = images.filter(default_image=True).first()
    if default:
        return default, "default image for device type"
    if images.count() == 1:
        return images.first(), "only image assigned to device type"
    versions = ", ".join(sorted(i.software_version.version for i in images))
    raise RuntimeError(
        f"Multiple software images assigned to device type {device.device_type.model} "
        f"({versions}) with no DLM validated entry and no default_image flag — "
        f"refusing to pick an upgrade target arbitrarily"
    )


def _check_flash_space(child, required_bytes, image_file, logger):
    logger.info("Checking flash space")
    output = child.send_command("dir flash: | include bytes free", read_timeout=30)

    match = re.search(r"(\d+)\s+bytes\s+free", output)
    if not match:
        logger.warning("Couldn't parse free space — proceeding anyway")
        return

    free_bytes = int(match.group(1))
    required_mb = required_bytes / (1024 * 1024)
    free_mb = free_bytes / (1024 * 1024)

    if free_bytes < required_bytes:
        raise RuntimeError(
            f"Not enough flash space for {image_file} — "
            f"need {required_mb:.0f}MB, only {free_mb:.0f}MB free"
        )

    logger.info(f"Flash OK — {free_mb:.0f}MB free, image needs {required_mb:.0f}MB")


def _boot_statement_references(child, image_file):
    """Tri-state check of the running-config boot statement.

    True/False when a `boot system` line exists (first-line check, not
    substring: IOS boots the first statement, so a stale image listed ahead
    of the target would still revert the device on reload). None when NO
    boot system line exists at all — some platforms (IE-3300 confirmed live)
    accept the command but never render it in running-config, storing it
    only in the BOOT variable; callers must defer to _bootvar_references.
    """
    output = child.send_command("show running-config | include boot system", read_timeout=30)
    for line in output.splitlines():
        if line.strip().startswith("boot system"):
            return image_file in line
    return None


def _bootvar_references(child, image_file):
    """True if the saved BOOT variable's first entry references the target
    image. The running-config boot statement only takes effect once written
    to NVRAM — the saved boot variable shows what the box will actually
    boot. `show boot` first (native on the switches), `show bootvar` as the
    fallback for platforms that reject it."""
    output = child.send_command("show boot", read_timeout=30)
    if "% Invalid" in output:
        output = child.send_command("show bootvar", read_timeout=30)
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("BOOT variable", "BOOT path-list")):
            continue
        if "does not exist" in stripped:
            return False
        # 'BOOT variable = flash:a.bin,12;flash:b.bin,12' — only the first
        # semicolon-separated entry is what actually boots.
        value = re.split(r"[=:]", stripped, maxsplit=1)[-1]
        return image_file in value.split(";")[0]
    return False


def _image_on_flash(child, image_file, logger):
    logger.info(f"Checking if {image_file} is already on flash")
    # netmiko strips the command echo, so a bare filename match is safe here.
    output = child.send_command(f"dir flash: | include {image_file}", read_timeout=30)
    return image_file in output


def _verify_image(child, image, logger):
    """Verify the downloaded image using the checksum from Nautobot if available."""
    algo = image.hashing_algorithm
    expected = image.image_file_checksum

    if not algo or not expected:
        logger.info("No checksum in Nautobot — skipping verification")
        return

    verify_flag = {"MD5": "/md5", "SHA256": "/sha256", "SHA512": "/sha512"}.get(algo.upper())
    if not verify_flag:
        logger.warning(f"Unsupported hashing algorithm '{algo}' — skipping verification")
        return

    logger.info(f"Verifying image ({algo})")
    output = child.send_command(
        f"verify {verify_flag} flash:{image.image_file_name}",
        read_timeout=600,
    )

    if "%Error" in output or "No such file" in output:
        raise RuntimeError(f"Image verification failed for {image.image_file_name}")

    match = re.search(r"=\s*([a-fA-F0-9]{16,})", output)
    if match and match.group(1).lower() != expected.lower():
        raise RuntimeError(
            f"Checksum mismatch: expected {expected}, got {match.group(1)}"
        )
    if not match:
        raise RuntimeError(f"Couldn't parse a checksum from verify output for {image.image_file_name}")

    logger.info("Image integrity verified")


def _set_boot_statement(child, image_file, logger):
    logger.info(f"Setting boot system to flash:{image_file}")
    output = child.send_config_set(
        ["no boot system", f"boot system flash:{image_file}"],
        read_timeout=30,
    )
    if _BOOT_ERROR.search(output):
        # The old boot statement is already gone — do NOT sail on and reload
        # against a device with no valid boot statement.
        raise RuntimeError(
            f"Device rejected 'boot system flash:{image_file}' — the image is "
            f"not on flash. Boot statement was cleared and NOT restored; fix "
            f"the image/flash state before reloading this device."
        )
