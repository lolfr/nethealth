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
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("nethealth.tplink")

ROUTER_URL_DEFAULT = "http://192.168.1.1"
KEYCHAIN_SERVICE = "eu.mylastnight.nethealth.tplink"
USERNAMES_TO_TRY = ("user", "admin")
CACHE_TTL_SEC = 30.0

# Pre-check HTTP : sans ça, `tplinkrouterc6u` timeout à 30 s par défaut
# (paramètre non exposé), ce qui bloque le tick monitor à chaque tentative
# quand un autre hôte répond aussi sur 192.168.1.1 (Wi-Fi tiers ≠ M8550).
# (connect_timeout, read_timeout) — 2 s suffit largement en LAN.
ROUTER_VALIDATE_TIMEOUT: tuple[float, float] = (2.0, 2.0)

# Signatures distinctives d'un firmware M-series TP-Link (M8550 et cousins)
_M8550_SERVER_TOKENS = ("lighttpd", "boa")
_M8550_BODY_TOKEN = "tp-link"
_M8550_PROBE_PATH = "/cgi/getParm"
# Le probe renvoie des paires `var <ident>="<hex>"` (clé RSA + paramètres) sur
# un firmware M-series ; un hôte tiers répond du HTML générique.
_M8550_PROBE_RE = re.compile(r'var\s+(?:nn|ee|userSetting)\s*=\s*"', re.IGNORECASE)

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


def _errno_label(errno: int | None) -> str | None:
    if errno in (50, 51, 65):
        return "pas de route vers le routeur"
    if errno == 61:
        return "routeur refuse la connexion"
    if errno == 64:
        return "routeur down"
    if errno == 49:
        return "adresse routeur indisponible"
    if errno is not None:
        return f"routeur KO (errno {errno})"
    return None


def _unwrap_oserror(exc: BaseException) -> OSError | None:
    """Descend la chaîne d'exceptions et renvoie le 1er OSError qui porte un
    `errno` non-None. requests/urllib3 emballent l'erreur socket dans plusieurs
    couches, dont la plus extérieure est elle-même un OSError (mais errno=None)
    — il faut creuser jusqu'au ConnectionRefusedError/TimeoutError d'origine.
    """
    cur: BaseException | None = exc
    seen: set[int] = set()
    fallback: OSError | None = None
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, OSError):
            if cur.errno is not None:
                return cur
            if fallback is None:
                fallback = cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return fallback


def _validate_m8550(
    url: str,
    timeout: tuple[float, float] = ROUTER_VALIDATE_TIMEOUT,
) -> tuple[bool, str | None]:
    """Pre-check HTTP : valide qu'on parle bien à un M8550 (ou firmware M-series
    cousin), pas juste à un hôte qui répond sur le même IP.

    On envoie un GET court et on cherche au moins une signature distinctive :
      - header `Server` ∈ {lighttpd, boa}
      - corps de page contenant 'TP-Link'
      - endpoint `/cgi/getParm` répondant (signe propre aux firmwares M-series)

    Discrimine 'rien ne répond' (timeout/no route) de 'quelque chose répond
    mais pas mon routeur'. Les errno OS sont mappés en labels lisibles.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        return False, "URL routeur invalide"

    base = url.rstrip("/")
    try:
        resp = requests.get(base + "/", timeout=timeout, allow_redirects=False)
    except requests.exceptions.ConnectTimeout:
        return False, "routeur injoignable (timeout)"
    except requests.exceptions.ReadTimeout:
        return False, "routeur ne répond pas (read timeout)"
    except requests.exceptions.ConnectionError as exc:
        os_exc = _unwrap_oserror(exc)
        if os_exc is not None:
            label = _errno_label(os_exc.errno)
            if label:
                return False, label
        return False, "routeur injoignable"
    except requests.exceptions.RequestException as exc:
        return False, f"pre-check KO: {type(exc).__name__}"

    server = (resp.headers.get("Server") or "").lower()
    if any(tok in server for tok in _M8550_SERVER_TOKENS):
        return True, None

    body_sample = (resp.text or "")[:4096].lower() if resp.content else ""
    if _M8550_BODY_TOKEN in body_sample:
        return True, None

    try:
        probe = requests.get(
            base + _M8550_PROBE_PATH,
            timeout=timeout,
            allow_redirects=False,
        )
        if probe.status_code == 200 and _M8550_PROBE_RE.search(probe.text or ""):
            return True, None
    except requests.exceptions.RequestException:
        pass

    return False, "hôte ≠ M8550 (signature absente)"


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

            # Pre-check 2 s qui valide aussi l'identité du routeur (signature
            # M-series), pour éviter le faux positif où une box tierce répond
            # sur 192.168.1.1. Évite par la même occasion le timeout 30 s caché
            # dans tplinkrouterc6u, et invalide la session ouverte si KO.
            reach_ok, reach_err = _validate_m8550(self.url)
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
