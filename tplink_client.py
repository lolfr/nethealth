"""
tplink_client.py — Wrapper léger pour le routeur TP-Link M8550 (5G CPE).

Objectifs :
- Auth via Keychain macOS (jamais de mdp en clair).
- Cache courte durée pour ne pas spammer le routeur.
- Sortie normalisée prête à brancher dans le menu rumps de NetHealth.

Le M8550 (firmware 1.3.0) attend username='user' et expose une partie seulement
des endpoints de tplinkrouterc6u. On consomme uniquement ce qui marche.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("nethealth.tplink")

ROUTER_URL_DEFAULT = "http://192.168.1.1"
KEYCHAIN_SERVICE = "eu.mylastnight.nethealth.tplink"
USERNAMES_TO_TRY = ("user", "admin")
CACHE_TTL_SEC = 30.0

# Pre-check TCP : sans ça, `tplinkrouterc6u` timeout à 30 s par défaut
# (paramètre non exposé), ce qui bloque le tick monitor à chaque tentative
# quand le routeur n'est pas dans le subnet courant (Wi-Fi maison ≠ M8550).
ROUTER_REACH_TIMEOUT_SEC = 2.0

# Mapping network_type → label humain (extrait des firmwares M-series)
_NETWORK_TYPE_LABELS = {
    0: "no service",
    1: "GSM",
    2: "WCDMA",
    3: "LTE",
    4: "5G NSA",
    5: "5G SA",
    6: "4G+",
}

_CONNECT_STATUS_LABELS = {
    0: "disconnected",
    1: "connecting",
    2: "auth",
    3: "scanning",
    4: "connected",
    5: "disconnecting",
}


@dataclass
class TplinkMetrics:
    available: bool = False
    error: str | None = None
    isp: str | None = None
    network_type: str | None = None
    connect_status: str | None = None
    sim_ok: bool | None = None
    data_consumed_bytes: int | None = None
    live_down_bps: int | None = None
    live_up_bps: int | None = None
    router_cpu: float | None = None
    router_mem: float | None = None
    clients_count: int | None = None
    wan_ipv4: str | None = None
    wan_conntype: str | None = None
    firmware_model: str | None = None
    firmware_version: str | None = None
    fetched_at: float = field(default_factory=time.time)


# ───────────────────────── Keychain helpers ─────────────────────────


def keychain_get_password(account: str = "user") -> str | None:
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return out.strip() or None
    except subprocess.CalledProcessError:
        return None
    except Exception as exc:
        log.warning("keychain read failed: %s", exc)
        return None


def keychain_set_password(password: str, account: str = "user") -> bool:
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", password],
            check=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return True
    except Exception as exc:
        log.warning("keychain write failed: %s", exc)
        return False


# ───────────────────────── Client ─────────────────────────


def _humanize_router_error(exc: Exception) -> str:
    """Convertit une exception remontée par tplinkrouterc6u/requests en
    message court et lisible côté UI. Fallback sur la classe + message tronqué."""
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()
    if "timeout" in low or name.endswith("Timeout"):
        return "routeur ne répond pas (timeout)"
    if "connection refused" in low or "refused" in low:
        return "routeur refuse la connexion"
    if "name or service" in low or "name resolution" in low:
        return "DNS routeur KO"
    if "authoriz" in low or "auth" in name.lower() or "unauthorized" in low:
        return "auth routeur refusée (mdp ?)"
    if "session vide" in low or "connectionerror" in low:
        # tous les endpoints ont raté → routeur joignable TCP mais HTTP cassé
        return "session HTTP cassée"
    return f"{name}: {msg[:60]}"


def _reach_router(url: str, timeout: float = ROUTER_REACH_TIMEOUT_SEC) -> tuple[bool, str | None]:
    """Pre-check TCP rapide vers le routeur. Retourne (ok, reason).

    On essaie un connect TCP nu plutôt qu'un HEAD HTTP : ça discrimine
    'pas de route' / 'pas dans le subnet' (timeout) de 'routeur up mais
    paquets droppés'. 2 s suffisent largement en LAN, et coupent net
    le timeout 30 s caché dans tplinkrouterc6u.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False, "URL routeur invalide"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        return True, None
    except socket.timeout:
        return False, "routeur injoignable (timeout)"
    except OSError as exc:
        if exc.errno in (50, 51, 65):
            return False, "pas de route vers le routeur"
        if exc.errno == 61:        # Connection refused
            return False, "routeur refuse la connexion"
        if exc.errno == 64:
            return False, "routeur down"
        return False, f"routeur KO (errno {exc.errno})"
    finally:
        try:
            s.close()
        except Exception:
            pass


class TplinkClient:
    def __init__(self, url: str = ROUTER_URL_DEFAULT):
        self.url = url
        self._lock = threading.Lock()
        self._cache: TplinkMetrics | None = None
        self._cache_ts: float = 0.0
        self._router = None
        self._username: str | None = None

    def _build_router(self, password: str):
        from tplinkrouterc6u import TplinkRouterProvider
        last_exc: Exception | None = None
        for username in USERNAMES_TO_TRY:
            try:
                r = TplinkRouterProvider.get_client(self.url, password, username=username)
                r.authorize()
                self._username = username
                return r
            except TypeError:
                try:
                    from tplinkrouterc6u.client.ex import TPLinkEXClient
                    r = TPLinkEXClient(self.url, password, username=username)
                    r.authorize()
                    self._username = username
                    return r
                except Exception as exc:
                    last_exc = exc
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("authorize: no candidate worked")

    def _close_router(self):
        if self._router is not None:
            try:
                self._router.logout()
            except Exception:
                pass
            self._router = None

    def fetch(self, force: bool = False) -> TplinkMetrics:
        with self._lock:
            now = time.time()
            if not force and self._cache and (now - self._cache_ts) < CACHE_TTL_SEC:
                return self._cache

            password = keychain_get_password()
            if not password:
                m = TplinkMetrics(available=False, error="mdp absent (Keychain vide)")
                self._cache, self._cache_ts = m, now
                return m

            # Pre-check 2 s : évite de sécher 30 s sur le timeout tplinkrouterc6u
            # quand on n'est pas dans le subnet du routeur. On invalide la session
            # ouverte aussi, parce qu'elle ne resservira pas dans cet état.
            reach_ok, reach_err = _reach_router(self.url)
            if not reach_ok:
                self._close_router()
                m = TplinkMetrics(available=False, error=reach_err)
                self._cache, self._cache_ts = m, now
                return m

            try:
                if self._router is None:
                    self._router = self._build_router(password)
                m = self._collect(self._router)
                self._cache, self._cache_ts = m, now
                return m
            except Exception as exc:
                log.warning("tplink fetch failed (%s) — invalidate session", exc)
                self._close_router()
                m = TplinkMetrics(available=False, error=_humanize_router_error(exc))
                self._cache, self._cache_ts = m, now
                return m

    def _collect(self, r) -> TplinkMetrics:
        m = TplinkMetrics(available=True)
        errors = []

        try:
            s = r.get_status()
            m.router_cpu = getattr(s, "cpu_usage", None)
            m.router_mem = getattr(s, "mem_usage", None)
            m.clients_count = getattr(s, "clients_total", None)
            wan_ip = getattr(s, "_wan_ipv4_addr", None) or getattr(s, "wan_ipv4_addr", None)
            m.wan_ipv4 = str(wan_ip) if wan_ip else None
        except Exception as exc:
            errors.append(f"status:{type(exc).__name__}")
            log.warning("tplink get_status failed: %s", exc)

        try:
            f = r.get_firmware()
            m.firmware_model = getattr(f, "model", None)
            m.firmware_version = getattr(f, "firmware_version", None)
        except Exception as exc:
            errors.append(f"fw:{type(exc).__name__}")
            log.warning("tplink get_firmware failed: %s", exc)

        try:
            ip4 = r.get_ipv4_status()
            m.wan_conntype = getattr(ip4, "_wan_ipv4_conntype", None) or getattr(ip4, "wan_ipv4_conntype", None)
        except Exception as exc:
            errors.append(f"ip4:{type(exc).__name__}")
            log.warning("tplink get_ipv4_status failed: %s", exc)

        try:
            lte = r.get_lte_status()
            m.isp = getattr(lte, "isp_name", None)
            nt = getattr(lte, "network_type", None)
            m.network_type = _NETWORK_TYPE_LABELS.get(nt, str(nt) if nt is not None else None)
            cs = getattr(lte, "connect_status", None)
            m.connect_status = _CONNECT_STATUS_LABELS.get(cs, str(cs) if cs is not None else None)
            sim = getattr(lte, "sim_status", None)
            m.sim_ok = (sim == 3) if sim is not None else None
            m.data_consumed_bytes = getattr(lte, "total_statistics", None)
            rx = getattr(lte, "cur_rx_speed", None)
            tx = getattr(lte, "cur_tx_speed", None)
            m.live_down_bps = (rx * 8) if rx is not None else None
            m.live_up_bps = (tx * 8) if tx is not None else None
        except Exception as exc:
            errors.append(f"lte:{type(exc).__name__}")
            log.warning("tplink get_lte_status failed: %s", exc)

        # Détection de "session morte" : tous les endpoints ont raté ou tous
        # les champs lisibles sont None → on déclare la session inutilisable
        # pour que le prochain fetch force une ré-auth fraîche.
        any_field_set = any([
            m.router_cpu is not None, m.firmware_model, m.wan_conntype,
            m.isp, m.data_consumed_bytes is not None,
        ])
        if not any_field_set:
            m.available = False
            m.error = "session vide (" + ",".join(errors or ["all-none"]) + ")"
            raise RuntimeError(m.error)
        return m


# ───────────────────────── Formatters ─────────────────────────


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    units = ("o", "Ko", "Mo", "Go", "To")
    f = float(n)
    for u in units:
        if f < 1024:
            return f"{f:.1f} {u}" if u != "o" else f"{int(f)} {u}"
        f /= 1024
    return f"{f:.1f} Po"


def fmt_bps(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} Mbps"
    if n >= 1_000:
        return f"{n / 1_000:.0f} kbps"
    return f"{n} bps"
