"""Dynamic single-account OKX bot registry discovery.

No hardcoded instance list; read-only. Manager must refuse sensitive
account-wide actions when any running bot is absent from its registry.
Never print API keys, secrets, or their digests.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class DiscoveryError(RuntimeError):
    pass


SERVICE_PATTERN = re.compile(r'^okx-grid-[a-z0-9-]+\.service$')
LEGACY_SERVICE_PATTERN = re.compile(r'^okx-bot[0-9]+\.service$')
ENGINE_SCRIPT_PATTERN = re.compile(r'^okx_bot[a-zA-Z0-9_]*\.py$')


@dataclass(frozen=True)
class BotInstance:
    display_name: str
    service: str
    root: Path
    instrument: str
    version: str
    systemd_state: str
    control_mode: str
    terminal_phase: str


def _load_dict(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryError('Cannot read valid JSON: ' + str(p)) from exc
    if not isinstance(data, dict):
        raise DiscoveryError('Expected JSON object: ' + str(p))
    return data


def _require_api_key(root: Path) -> None:
    """Check presence without revealing secrets. Several API keys may share one account."""
    env = root / '.env'
    try:
        raw = env.read_text(encoding='utf-8')
    except OSError as exc:
        raise DiscoveryError('Cannot read bot credentials file: ' + str(env)) from exc
    keys = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        if k.strip() == 'OKX_API_KEY':
            keys.append(v.strip().strip('\"').strip("'"))
    if len(keys) != 1 or not keys[0]:
        raise DiscoveryError('Missing/duplicate OKX_API_KEY field: ' + str(env))
    # Distinct API keys can belong to the SAME OKX account. Never equate
    # API key equality with account ownership. One-account VPS is a deployment
    # contract; account verification needs a separate authenticated mechanism.
    return None


def _parse_systemd_units(text: str) -> dict[str, str]:
    units = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        unit = parts[0]
        if not (SERVICE_PATTERN.fullmatch(unit) or LEGACY_SERVICE_PATTERN.fullmatch(unit)):
            continue
        if unit in units:
            raise DiscoveryError('Duplicate systemd unit: ' + unit)
        units[unit] = parts[2]  # LOAD ACTIVE SUB DESCRIPTION
    return units


def _running_engines_under(root: Path) -> list[int]:
    """Find un-managed Python engine processes launched directly from legacy root.

    No access to credentials or environment. A vanished/unreadable proc entry
    is ignored; running suspicious engine under this root blocks migration.
    """
    pids = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            args = [os.fsdecode(x) for x in (proc / 'cmdline').read_bytes().split(b'\0') if x]
        except (OSError, ValueError):
            continue
        for arg in args:
            p = Path(arg)
            if (p.is_absolute() and ENGINE_SCRIPT_PATTERN.fullmatch(p.name)
                    and p.parent.resolve() == root):
                pids.append(int(proc.name))
                break
    return pids


def _legacy_activity_on_host(service: str, root: Path) -> bool:
    """Read-only, fail closed if systemctl cannot confirm legacy service inactive."""
    proc = subprocess.run(
        ['systemctl', 'show', '--no-pager', service,
         '-p', 'LoadState', '-p', 'ActiveState', '-p', 'MainPID'],
        capture_output=True, text=True, timeout=12,
    )
    if proc.returncode:
        raise DiscoveryError('Cannot verify legacy systemd service: ' + service)
    props = dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)
    active = props.get('ActiveState')
    pid = props.get('MainPID')
    if active is None or pid is None or not pid.isdecimal():
        raise DiscoveryError('Incomplete legacy systemd service status: ' + service)
    return (active not in ('inactive', 'failed') or int(pid) > 0
            or bool(_running_engines_under(root)))


def discover_single_account(
    base: Path,
    systemd_dir: Path,
    *,
    systemd_listing: str | None = None,
    legacy_activity_check=None,
) -> list[BotInstance]:
    """Find *all* registered bots and fail closed on unregistered active ones.

    This module is intentionally read-only. Integrate into manager only after
    checking its registry semantics, service creation and lifecycle transitions.
    """
    base = Path(base).resolve()
    registry = _load_dict(base / '.okxbot_registry.json')
    listing_injected = systemd_listing is not None
    if systemd_listing is None:
        result = subprocess.run(
            ['systemctl', 'list-units', '--type=service', '--all', '--plain',
             '--no-legend', '--no-pager', 'okx-grid-*.service', 'okx-bot*.service'],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise DiscoveryError('Cannot enumerate systemd bot services')
        systemd_listing = result.stdout
    systemd = _parse_systemd_units(systemd_listing)
    all_instances: list[BotInstance] = []
    expected_services = set()
    expected_roots = set()
    for display, entry in registry.items():
        if not isinstance(display, str) or not isinstance(entry, dict):
            raise DiscoveryError('Malformed instance registry entry')
        service_value = entry.get('service')
        if not isinstance(service_value, str):
            raise DiscoveryError('Invalid instance unit for ' + display)
        service = service_value + '.service'
        if not (SERVICE_PATTERN.fullmatch(service) or LEGACY_SERVICE_PATTERN.fullmatch(service)):
            raise DiscoveryError('Invalid instance unit for ' + display)
        raw_root = Path(str(entry.get('path', '')))
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise DiscoveryError('Instance root missing/symlinked: ' + display)
        root = raw_root.resolve(strict=True)
        if root == base or base not in root.parents or not root.is_dir():
            raise DiscoveryError('Instance root outside base or missing: ' + display)
        if service in expected_services or root in expected_roots:
            raise DiscoveryError('Duplicate bot service/root in registry')
        expected_services.add(service)
        expected_roots.add(root)
        if LEGACY_SERVICE_PATTERN.fullmatch(service):
            # Old bot01/bot02-style records remain in registry, but are never
            # migrated or included in account SOFT reset. They are acceptable
            # ONLY when BOTH the unit and the engine process are inactive.
            if systemd.get(service) in ('active', 'activating', 'reloading', 'deactivating'):
                raise DiscoveryError('Active legacy trading service: ' + service)
            if legacy_activity_check is None and listing_injected:
                raise DiscoveryError('Injected listing requires legacy_activity_check: ' + service)
            check = legacy_activity_check or _legacy_activity_on_host
            if check(service, root):
                raise DiscoveryError('Active legacy trading service/process: ' + service)
            continue
        if not (Path(systemd_dir) / service).is_file():
            raise DiscoveryError('Registered bot has no systemd unit: ' + service)
        config = _load_dict(root / 'config.json')
        instrument = str(config.get('instrument', ''))
        if not re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', instrument):
            raise DiscoveryError('Unexpected OKX instrument: ' + display)
        _require_api_key(root)
        state = systemd.get(service, 'inactive')
        control_file = root / 'state' / 'control.json'
        control = _load_dict(control_file) if control_file.is_file() else {'mode':'RUN'}
        ack_file = root / 'state' / 'control_ack.json'
        ack = _load_dict(ack_file) if ack_file.is_file() else {}
        all_instances.append(BotInstance(
            display_name=display, service=service, root=root,
            instrument=instrument, version=str(config.get('version', '?')),
            systemd_state=state,
            control_mode=str(control.get('mode', '?')),
            terminal_phase=str(ack.get('phase', '')),
        ))
    unknown = sorted(unit for unit, state in systemd.items()
                     if state in ('active','activating','reloading')
                     and unit not in expected_services)
    if unknown:
        raise DiscoveryError('Unregistered active trading services: ' + ', '.join(unknown))
    return sorted(all_instances, key=lambda item: item.service)
