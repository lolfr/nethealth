"""
Network Health — POC macOS menu-bar
====================================

Petite app menu-bar qui ne fait qu'UNE chose : quand tu es en partage de
connexion iPhone, elle mesure la santé du réseau (latence, perte de paquets,
débit) et affiche un cercle coloré dans la barre des menus.

- Cercle gris vide  → pas de partage iPhone détecté
- Point rouge au centre → connexion mauvaise
- Remplissage progressif rouge → orange → vert → cercle plein vert = optimal

Analogie : l'icône est un "réservoir" qui se remplit. Plus elle est pleine
et verte, plus ton réseau va bien.
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

import rumps
from PIL import Image, ImageDraw

# Dispatche un callable sur le main thread — indispensable pour toucher la
# moindre partie de l'UI depuis un thread d'arrière-plan. Sans ça, Cocoa
# finit par geler l'app (et parfois tout le système).
try:
    from PyObjCTools.AppHelper import callAfter as _call_on_main
except ImportError:
    # Fallback dev/linux : on exécute directement (pas d'UI de toute façon)
    def _call_on_main(fn, *args, **kw):
        try:
            fn(*args, **kw)
        except Exception:
            pass

# Import optionnel de CoreWLAN (API native macOS). Si absent, on retombe
# sur les méthodes CLI.
try:
    from CoreWLAN import CWWiFiClient
    HAS_COREWLAN = True
except ImportError:
    CWWiFiClient = None
    HAS_COREWLAN = False

# CoreLocation → c'est CE framework qui déclenche la popup de permission
# macOS et fait apparaître l'app dans Réglages → Services de localisation.
# Sans ça, CoreWLAN renvoie <redacted> silencieusement.
try:
    from CoreLocation import CLLocationManager
    HAS_CORELOCATION = True
except ImportError:
    CLLocationManager = None
    HAS_CORELOCATION = False

# Wrapper TP-Link M8550 (5G CPE) — optionnel : si la lib n'est pas installée
# ou si pas de mdp dans le Keychain, la section disparaît silencieusement.
try:
    import tplink_client
    HAS_TPLINK = True
    _TPLINK_IMPORT_ERR = None
except ImportError as _e:
    tplink_client = None
    HAS_TPLINK = False
    _TPLINK_IMPORT_ERR = str(_e)


# CLAuthorizationStatus values (macOS) — https://developer.apple.com/documentation/corelocation/clauthorizationstatus
_LOC_STATUS = {
    0: "non-déterminé (jamais demandé)",
    1: "restreint (politique système)",
    2: "refusé",
    3: "autorisé (always)",
    4: "autorisé (when-in-use)",
}

# Speedtest : on préfère la copie VENDORISÉE (speedtest_vendor.py à côté de
# ce fichier), sinon la version pip-installée. La version vendorisée garantit
# que py2app l'embarque quoi qu'il arrive — il avait tendance à rater l'import
# quand il était protégé par un try/except.
_speedtest_import_err = None
speedtest_py = None
HAS_SPEEDTEST_PY = False
try:
    import speedtest_vendor as speedtest_py
    HAS_SPEEDTEST_PY = True
    _speedtest_source = "vendor"
except ImportError as _e1:
    try:
        import speedtest as speedtest_py
        HAS_SPEEDTEST_PY = True
        _speedtest_source = "pip"
    except ImportError as _e2:
        _speedtest_import_err = f"vendor: {_e1} / pip: {_e2}"
        _speedtest_source = None


# -----------------------------------------------------------------------------
# Logging — fichier persistant pour debugger à froid après un plantage
# -----------------------------------------------------------------------------

LOG_DIR = Path.home() / "Library" / "Logs" / "NetHealth"
LOG_FILE = LOG_DIR / "nethealth.log"


def _setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nethealth")
    if logger.handlers:
        return logger  # déjà configuré (ex: re-import en dev)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(threadName)-10s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Fichier tournant : 1 Mo max, 3 archives → on ne pourrit pas le disque
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Stdout aussi, utile en dev quand l'app est lancée depuis iTerm
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # Capture des exceptions non-gérées — sinon elles se perdent dans le vide
    def _excepthook(exc_type, exc, tb):
        logger.error("UNCAUGHT EXCEPTION",
                     exc_info=(exc_type, exc, tb))
    sys.excepthook = _excepthook
    return logger


log = _setup_logging()


def _dbg_write(msg):
    """Écriture debug directe dans un fichier séparé, qui contourne
    entièrement le module logging. Pour quand on soupçonne que logging
    elle-même est broken (encoding, handler fermé, filter mystique…)."""
    try:
        dbg_file = LOG_DIR / "debug.log"
        with open(dbg_file, "a", encoding="utf-8") as f:
            f.write(
                f"{time.time():.3f} [{threading.current_thread().name}] {msg}\n"
            )
            f.flush()
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

VERSION = "1.23"                                          # bump à chaque modif du script

USER_CONFIG_PATH = Path.home() / ".nethealth_ssids.json"  # SSIDs ajoutés par l'utilisateur

PING_HOSTS = ["1.1.1.1", "8.8.8.8", "apple.com"]          # sources fiables
PING_INTERVAL_SEC = 30                                    # ping toutes les 30 s
SPEEDTEST_INTERVAL_SEC = 15 * 60                          # débit toutes les 15 min
SPEEDTEST_TIMEOUT_SEC = 90

# Sonde "medium" : 1 MB depuis Cloudflare via curl --interface, par interface.
# Permet d'avoir un débit estimé par lien sans déclencher un speedtest complet.
MEDIUM_PROBE_URL = "https://speed.cloudflare.com/__down?bytes=1048576"
MEDIUM_PROBE_INTERVAL_SEC = 10 * 60                       # 1 iface medium-testée toutes les 10 min
MEDIUM_PROBE_TIMEOUT_SEC = 8                              # téléchargement total max
MEDIUM_PROBE_CONNECT_TIMEOUT_SEC = 4                      # phase TCP/TLS uniquement

# Budget data sur la 5G. Si on consomme + de X MB/h sur le routeur TP-Link,
# on bascule en mode économique : plus de medium sur les interfaces mobiles,
# plus de speedtest auto. Les ping cheap restent (négligeables).
TPLINK_BUDGET_MB_PER_HOUR = 100

# Réseaux Wi-Fi considérés comme "partage mobile".
# La comparaison se fait par PRÉFIXE après normalisation (casse, apostrophes,
# espaces). Donc :
#   "Lolfr's Mobile"  → capture "Lolfr's Mobile 5GHz" et "Lolfr's Mobile 2.4GHz"
# Pas besoin de répliquer à l'identique un SSID — juste un préfixe reconnaissable.
TETHERING_SSIDS = [
"Nostromo",         # autre partage iPhone nommé "Nostromo"
    "Lolfr's Mobile",   # routeur TP-Link 5G (M8550)
]

# Seuils : ce qu'on considère "bon" / "mauvais"
LATENCY_GREAT_MS = 30       # <= 30 ms  → score latence = 1.0
LATENCY_BAD_MS = 250        # >= 250 ms → score latence = 0.0
LOSS_BAD_PCT = 20           # 20 %      → score perte   = 0.0
DOWNLOAD_GREAT_MBPS = 50    # >= 50     → score débit   = 1.0

ICON_SIZE = 44              # pixels (menu bar se redimensionne auto)

# Style d'icône menubar :
#   "pie"   : pie-chart historique (1 quartier par iface, rayon = santé)
#   "gauge" : barres verticales (1 par iface, hauteur = santé) — épuré, "tableau de bord"
#   "radar" : rayons depuis un point central (longueur = santé) — évocation "ondes/signal"
ICON_STYLE = "gauge"


# -----------------------------------------------------------------------------
# Détection du partage iPhone
# -----------------------------------------------------------------------------

def _list_iphone_devices():
    """
    Retourne la liste des devices macOS (ex: en5, en6) qui correspondent
    à un partage iPhone — USB ou personal hotspot.
    """
    try:
        out = subprocess.run(
            ["networksetup", "-listnetworkserviceorder"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []

    devices = []
    # Les lignes intéressantes ressemblent à :
    #   (Hardware Port: iPhone USB, Device: en7)
    pattern = re.compile(r"Hardware Port:\s*([^,]+),\s*Device:\s*(\w+)")
    for match in pattern.finditer(out):
        hw_port, device = match.group(1).strip(), match.group(2).strip()
        if "iPhone" in hw_port:
            devices.append(device)
    return devices


# -----------------------------------------------------------------------------
# Énumération multi-interfaces
# -----------------------------------------------------------------------------

def _hardware_ports():
    """Parse `networksetup -listallhardwareports` en liste de dicts.
    Chaque dict : {'port': 'Wi-Fi', 'device': 'en0', 'mac': 'aa:bb:cc:...'}
    """
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return []
    ports = []
    current = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            if current:
                ports.append(current)
            current = {"port": line.split(":", 1)[1].strip()}
        elif line.startswith("Device:"):
            current["device"] = line.split(":", 1)[1].strip()
        elif line.startswith("Ethernet Address:"):
            current["mac"] = line.split(":", 1)[1].strip()
    if current:
        ports.append(current)
    return ports


def _ipv4_of(device):
    """IPv4 de l'interface si active, None sinon."""
    try:
        out = subprocess.run(
            ["ifconfig", device],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return None
    m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", out)
    return m.group(1) if m else None


def _interface_is_up(device):
    """True si l'interface a 'status: active' ou est 'UP' dans ifconfig —
    autrement dit présente côté lien physique, même sans IP utile."""
    try:
        out = subprocess.run(
            ["ifconfig", device],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return False
    if "status: active" in out:
        return True
    # Certaines interfaces (iPhone USB) n'affichent pas "status:" mais ont le
    # flag RUNNING dans la première ligne quand elles sont UP.
    first_line = out.splitlines()[0] if out else ""
    return "RUNNING" in first_line


def _classify_interface(port_name):
    """Catégorise l'interface à partir de son Hardware Port."""
    p = port_name.lower()
    if "iphone" in p:
        return "iphone"
    if "wi-fi" in p or "wifi" in p or "airport" in p:
        return "wifi"
    if "ethernet" in p or "thunderbolt" in p or "usb 10" in p:
        return "ethernet"
    if "bluetooth" in p:
        return "bluetooth"
    return "other"


def default_route_device():
    """Retourne le device (ex: 'en0') de la route par défaut actuelle, ou None.
    Analogie : c'est par où ton laptop envoie vraiment ses paquets en ce moment,
    quand il n'y a pas de règle spéciale."""
    try:
        out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return None
    m = re.search(r"interface:\s*(\S+)", out)
    return m.group(1) if m else None


def list_active_interfaces():
    """
    Retourne la liste des interfaces physiques d'intérêt. Deux catégories :

      - **ready** : a une IPv4 routable → on peut y pinger, y mesurer du débit.
      - **standby** : interface physiquement présente (iPhone USB branché, mais
        iOS ne route pas encore, ou IP auto-assignée 169.254.x.x), pas d'IP
        utile → on ne peut pas mesurer, mais on la montre quand même parce
        qu'elle est « prête à être utilisée ».

    Chaque élément :
      {
        'port': 'Wi-Fi',
        'device': 'en0',
        'type': 'wifi',          # wifi / iphone / ethernet / bluetooth / other
        'ipv4': '10.0.0.5' ou None,
        'ssid': 'Nostromo',      # pour Wi-Fi uniquement
        'is_default': True,
        'is_mobile': True,       # = partage mobile (iPhone ou SSID connu)
        'is_ready': True,        # False si standby (pas d'IP utile)
      }
    """
    default_dev = default_route_device()
    result = []
    for p in _hardware_ports():
        dev = p.get("device")
        if not dev:
            continue
        ipv4 = _ipv4_of(dev)
        type_ = _classify_interface(p["port"])

        # Un IPv4 auto-assigné (169.254.x.x, APIPA) = pas utilisable pour
        # router du trafic, on le traite comme standby.
        usable_ipv4 = ipv4 if (ipv4 and not ipv4.startswith("169.254.")) else None

        # Règles d'inclusion :
        #  - ready   : a une IP utilisable → toujours inclus.
        #  - iPhone  : inclus TOUJOURS quand un Hardware Port "iPhone" est
        #    listé par macOS, même s'il n'a pas d'IP et pas de "status: active".
        #    C'est typique de l'état "Joint" jaune dans Réglages → Réseau :
        #    iPhone branché, prêt à servir d'uplink, mais pas utilisé.
        include_as_ready = bool(usable_ipv4)
        include_as_standby = (
            not include_as_ready and type_ == "iphone"
        )
        if not (include_as_ready or include_as_standby):
            continue

        iface = {
            "port": p["port"],
            "device": dev,
            "type": type_,
            "ipv4": usable_ipv4,
            "is_default": (dev == default_dev),
            "is_ready": include_as_ready,
        }
        if type_ == "wifi":
            iface["ssid"] = current_wifi_ssid()
            iface["is_mobile"] = bool(
                iface["ssid"] and is_tethering_ssid(iface["ssid"])
            )
        elif type_ == "iphone":
            iface["is_mobile"] = True
        else:
            iface["is_mobile"] = False
        result.append(iface)
    return result


def _wifi_device():
    """Retourne le device du port Wi-Fi (ex: 'en0') ou None."""
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return None
    current_port = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            current_port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and current_port == "Wi-Fi":
            return line.split(":", 1)[1].strip()
    return None


# Sur macOS 14+, si Location Services est refusé à l'app qui fait la demande,
# le système renvoie littéralement la chaîne ci-dessous au lieu du vrai SSID.
# On la détecte et on la considère comme "pas de SSID connu".
REDACTED_MARKER = "<redacted>"


def _clean_ssid(ssid):
    """Retourne le SSID si valide, None sinon (gère les <redacted> et vide)."""
    if not ssid:
        return None
    ssid = ssid.strip()
    if not ssid or ssid == REDACTED_MARKER:
        return None
    return ssid


def location_auth_status():
    """Retourne (code, label) de l'autorisation Location actuelle."""
    if not HAS_CORELOCATION:
        return (-1, "CoreLocation indispo")
    try:
        code = int(CLLocationManager.authorizationStatus())
    except Exception:
        return (-1, "statut introuvable")
    return (code, _LOC_STATUS.get(code, f"inconnu ({code})"))


def request_location_permission():
    """
    Crée un CLLocationManager et demande l'autorisation d'accès à la
    localisation. C'EST ÇA qui déclenche la popup macOS et enregistre
    l'app dans la liste Réglages → Services de localisation.

    Returns : le manager (à stocker en attribut pour tenir une strong ref ;
    sinon PyObjC le libère avant que la popup s'affiche).
    """
    if not HAS_CORELOCATION:
        log.warning("CoreLocation indisponible — popup de permission impossible")
        return None
    try:
        manager = CLLocationManager.alloc().init()
        code, label = location_auth_status()
        log.info("Statut Location avant demande : %s (%s)", code, label)
        manager.requestAlwaysAuthorization()
        log.info("requestAlwaysAuthorization() envoyé")
        return manager
    except Exception:
        log.exception("requestAlwaysAuthorization a planté")
        return None


def _corewlan_interface():
    """Retourne l'interface Wi-Fi par défaut via CoreWLAN, ou None."""
    if not HAS_COREWLAN:
        return None
    try:
        client = CWWiFiClient.sharedWiFiClient()
        return client.interface() if client else None
    except Exception:
        return None


def _ssid_via_corewlan_raw():
    """Renvoie la chaîne brute retournée par CoreWLAN (peut valoir '<redacted>'
    si Location Services est refusé)."""
    iface = _corewlan_interface()
    if iface is None:
        return None
    try:
        return iface.ssid()
    except Exception:
        return None


def _ssid_via_corewlan():
    """Méthode native macOS : CoreWLAN. Requiert Location Services. Retourne
    None si pas de Wi-Fi connecté ou permission refusée."""
    return _clean_ssid(_ssid_via_corewlan_raw())


def wifi_stats_via_corewlan():
    """
    Retourne un dict avec les stats radio Wi-Fi quand CoreWLAN est disponible
    et qu'on est connecté à un réseau :
      - rssi       : puissance du signal reçu (dBm, négatif ; -50 = excellent,
                     -80 = limite)
      - noise      : bruit de fond (dBm, plus négatif = meilleur)
      - snr        : rapport signal/bruit (dB, >40 = excellent)
      - tx_mbps    : débit négocié côté émission (ce que l'AP nous promet)
      - channel    : n° du canal + largeur de bande
      - phy_mode   : type physique (802.11ac, ax…)

    Analogie : c'est comme écouter la radio. rssi = volume du signal capté,
    noise = parasites ambiants, snr = à quel point le signal domine les
    parasites.
    """
    iface = _corewlan_interface()
    if iface is None:
        return {}
    stats = {}
    try:
        rssi = iface.rssiValue()
        if rssi and rssi != 0:
            stats["rssi"] = int(rssi)
    except Exception:
        pass
    try:
        noise = iface.noiseMeasurement()
        if noise and noise != 0:
            stats["noise"] = int(noise)
    except Exception:
        pass
    if "rssi" in stats and "noise" in stats:
        stats["snr"] = stats["rssi"] - stats["noise"]
    try:
        tx = iface.transmitRate()
        if tx:
            stats["tx_mbps"] = float(tx)
    except Exception:
        pass
    try:
        ch = iface.wlanChannel()
        if ch:
            stats["channel"] = f"{ch.channelNumber()} ({ch.channelWidth()}MHz)"
    except Exception:
        pass
    return stats


def location_permission_denied():
    """True si CoreWLAN nous renvoie <redacted> — signe que la permission
    Location Services est refusée à l'app qui tourne."""
    return _ssid_via_corewlan_raw() == REDACTED_MARKER


def current_wifi_ssid():
    """Retourne le SSID courant via CoreWLAN, ou None si pas connecté /
    permission refusée. Seule méthode utilisée : CoreWLAN."""
    return _ssid_via_corewlan()


# Toutes les variantes d'apostrophes qu'on a pu voir "dans la nature"
_APOSTROPHES = ("\u2019", "\u2018", "\u02bc", "\u00b4", "\u0060")


def _normalize_ssid(s):
    """
    Canonicalise un SSID pour la comparaison :
    - ramène toutes les variantes d'apostrophes à l'ASCII "'"
    - strip les espaces en bordure
    - casefold (~= lowercase, mais plus robuste unicode)
    """
    if not s:
        return ""
    for q in _APOSTROPHES:
        s = s.replace(q, "'")
    return s.strip().casefold()


def load_user_ssids():
    """Charge la liste des SSIDs marqués manuellement comme partage, depuis JSON."""
    try:
        data = json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
        return list(data.get("tethering_ssids", []))
    except Exception:
        return []


def save_user_ssids(ssids):
    """Persist la liste des SSIDs ajoutés par l'utilisateur."""
    USER_CONFIG_PATH.write_text(
        json.dumps({"tethering_ssids": sorted(set(ssids))}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_tethering_ssid(ssid):
    """
    True si `ssid` est reconnu comme un partage mobile, via :
      1) un match par PRÉFIXE contre TETHERING_SSIDS (hardcodé) ;
      2) un match EXACT contre un SSID ajouté manuellement via le menu.
    """
    if not ssid:
        return False
    norm = _normalize_ssid(ssid)
    # 1) Préfixes codés en dur
    for t in TETHERING_SSIDS:
        nt = _normalize_ssid(t)
        if nt and (norm == nt or norm.startswith(nt)):
            return True
    # 2) Ajouts utilisateur (match exact après normalisation)
    for u in load_user_ssids():
        if _normalize_ssid(u) == norm:
            return True
    return False


def is_on_tethered_network():
    """
    True dans 2 cas :
      - une interface iPhone USB est active ET a une IP ;
      - le Wi-Fi est connecté à un SSID listé dans TETHERING_SSIDS.

    Analogie : on a deux mouchards différents (câble iPhone d'un côté,
    nom du Wi-Fi de l'autre) et il suffit qu'un des deux dise "oui".
    """
    # 1) iPhone USB (ou Bluetooth PAN nommé iPhone)
    for dev in _list_iphone_devices():
        try:
            out = subprocess.run(
                ["ifconfig", dev], capture_output=True, text=True, timeout=3
            ).stdout
        except Exception:
            continue
        if "status: active" in out and re.search(r"\binet\s+\d", out):
            return True

    # 2) Wi-Fi sur un SSID de partage connu (tolérant casse/apostrophe)
    if is_tethering_ssid(current_wifi_ssid()):
        return True

    return False


# Compat' avec l'ancien nom
is_iphone_tethered = is_on_tethered_network


# -----------------------------------------------------------------------------
# Mesure ping
# -----------------------------------------------------------------------------

def ping_host(host, count=3, timeout=3, source_ip=None):
    """
    Retourne (latence_ms, perte_pct). Si le host ne répond pas du tout :
    (None, 100.0).

    Si `source_ip` est passé, le ping est forcé de sortir par cette IP source
    (donc par l'interface physique qui porte cette IP). C'est ce qui nous
    permet de mesurer chaque interface individuellement, indépendamment de
    la route par défaut.
    """
    cmd = ["ping", "-c", str(count), "-t", str(timeout)]
    if source_ip:
        cmd.extend(["-S", source_ip])
    cmd.append(host)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=count * timeout + 3,
        )
        out = result.stdout
    except Exception:
        return None, 100.0

    loss = 100.0
    latency = None

    m = re.search(r"([\d.]+)% packet loss", out)
    if m:
        loss = float(m.group(1))

    # Mac : "round-trip min/avg/max/stddev = 15.1/20.4/25.7/3.1 ms"
    m = re.search(r"(?:round-trip|rtt)[^=]*=\s*[\d.]+/([\d.]+)/", out)
    if m:
        latency = float(m.group(1))

    return latency, loss


def measure_network(source_ip=None):
    """
    Ping tous les hôtes (optionnellement via une IP source donnée),
    moyenne la latence, moyenne la perte.
    """
    lats, losses = [], []
    for host in PING_HOSTS:
        lat, loss = ping_host(host, source_ip=source_ip)
        if lat is not None:
            lats.append(lat)
        losses.append(loss)
    avg_lat = sum(lats) / len(lats) if lats else None
    avg_loss = sum(losses) / len(losses) if losses else 100.0
    return avg_lat, avg_loss


_CURL_ERROR_LABELS = {
    3:  "URL mal formée",
    5:  "proxy injoignable",
    6:  "DNS KO",
    7:  "pas de route",
    28: "timeout",
    35: "handshake TLS KO",
    45: "iface inutilisable",
    52: "réponse vide",
    56: "connexion coupée",
    60: "certificat invalide",
}

# macOS : socket option pour binder un socket à une iface précise
# (équivalent runtime de `curl --interface`). Permet un pre-check rapide
# avant la sonde medium, pour distinguer "iface morte" de "iface lente".
_IP_BOUND_IF = 25
_REACH_HOST = "1.1.1.1"
_REACH_PORT = 443
_REACH_TIMEOUT_SEC = 2.0


def iface_can_reach(device):
    """Pre-check : tente une connexion TCP via une iface précise.

    Une iface peut être 'is_ready' (avec un IPv4 non APIPA) MAIS sans route
    fonctionnelle vers Internet — typique d'un dock USB-Ethernet branché à
    un switch sans uplink, ou d'un Wi-Fi associé à un AP sans backhaul.
    Ce check coupe court (2 s max) au lieu de laisser un curl 8 s pourrir le
    tick suivant.

    Retourne (ok: bool, reason: str|None). reason est lisible côté UI.
    """
    if not device:
        return False, "no device"
    try:
        idx = socket.if_nametoindex(device)
    except OSError:
        return False, "iface inconnue"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, idx)
        s.settimeout(_REACH_TIMEOUT_SEC)
        s.connect((_REACH_HOST, _REACH_PORT))
        return True, None
    except socket.timeout:
        return False, "pas de route"
    except OSError as exc:
        # 51 = Network unreachable, 65 = No route to host, 64 = Host down
        # 50 = Network down, 49 = Address not available
        if exc.errno in (50, 51, 65):
            return False, "réseau injoignable"
        if exc.errno == 64:
            return False, "host down"
        if exc.errno == 49:
            return False, "iface sans IP"
        return False, f"lien KO (errno {exc.errno})"
    finally:
        try:
            s.close()
        except Exception:
            pass


def _curl_error_label(rc, stderr):
    """Traduit un returncode curl en motif court et lisible."""
    label = _CURL_ERROR_LABELS.get(rc, f"rc={rc}")
    detail = (stderr or "").strip().splitlines()
    detail_txt = detail[-1][:80] if detail else ""
    return f"{label} ({detail_txt})" if detail_txt else label


def probe_medium(device):
    """Sonde "medium" : télécharge 1 MB Cloudflare via `curl --interface DEV`.

    Bind l'interface réseau côté curl pour mesurer le lien physique demandé,
    pas la route par défaut. Retourne un dict :
        {ok, down_mbps, bytes_used, duration_sec, error}
    """
    if not device:
        return {"ok": False, "error": "no device"}
    cmd = [
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{speed_download};%{size_download};%{time_total}",
        "--interface",
        device,
        "--connect-timeout",
        str(MEDIUM_PROBE_CONNECT_TIMEOUT_SEC),
        "--max-time",
        str(MEDIUM_PROBE_TIMEOUT_SEC),
        MEDIUM_PROBE_URL,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=MEDIUM_PROBE_TIMEOUT_SEC + 3
        )
        if r.returncode != 0:
            return {
                "ok": False,
                "error": _curl_error_label(r.returncode, r.stderr),
            }
        speed_str, size_str, time_str = r.stdout.strip().split(";")
        size_b = int(float(size_str))
        if size_b < 100_000:
            return {"ok": False, "error": f"download tronqué ({size_b} B)"}
        return {
            "ok": True,
            "down_mbps": float(speed_str) * 8 / 1_000_000,
            "bytes_used": size_b,
            "duration_sec": float(time_str),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# -----------------------------------------------------------------------------
# Débit (speedtest-cli)
# -----------------------------------------------------------------------------

def run_speedtest_urllib():
    """
    Speedtest autonome, sans dépendance, via urllib et les endpoints publics
    de Cloudflare (speed.cloudflare.com).

    Analogie : plutôt que d'appeler une lib spécialisée qui parle à tout un
    réseau de serveurs Ookla, on fait juste un gros téléchargement chronométré
    puis un gros upload chronométré. Moins précis qu'Ookla, mais suffisant
    pour savoir si tu as 10 ou 100 Mbps, et toujours disponible.

    Retourne (down_mbps, up_mbps). Un des deux peut être None si un volet
    échoue — on renvoie quand même ce qu'on a.
    """
    import urllib.request

    down_mbps = None
    up_mbps = None

    # Cloudflare refuse les requêtes sans User-Agent — piège classique.
    ua_headers = {
        "User-Agent": f"NetHealth/{VERSION} (macOS; +https://github.com/)",
        "Accept": "*/*",
    }

    # --- Download : 25 Mo depuis Cloudflare ---
    try:
        url = "https://speed.cloudflare.com/__down?bytes=25000000"
        req = urllib.request.Request(url, headers=ua_headers)
        start = time.time()
        downloaded = 0
        with urllib.request.urlopen(req, timeout=20) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                downloaded += len(chunk)
        elapsed = time.time() - start
        if elapsed > 0 and downloaded > 0:
            down_mbps = (downloaded * 8.0 / 1_000_000.0) / elapsed
            log.info(
                "speedtest (urllib/down) : %.1f Mbps (%.1f Mo en %.1f s)",
                down_mbps, downloaded / 1_000_000, elapsed,
            )
    except Exception as exc:
        log.warning("speedtest (urllib/down) échec : %s", exc)

    # --- Upload : 10 Mo vers Cloudflare ---
    try:
        url = "https://speed.cloudflare.com/__up"
        payload = os.urandom(10_000_000)
        start = time.time()
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                **ua_headers,
                "Content-Type": "application/octet-stream",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        elapsed = time.time() - start
        if elapsed > 0:
            up_mbps = (len(payload) * 8.0 / 1_000_000.0) / elapsed
            log.info(
                "speedtest (urllib/up) : %.1f Mbps (%.1f Mo en %.1f s)",
                up_mbps, len(payload) / 1_000_000, elapsed,
            )
    except Exception as exc:
        log.warning("speedtest (urllib/up) échec : %s", exc)

    return down_mbps, up_mbps


def run_speedtest():
    """
    Mesure (down_mbps, up_mbps). 2 méthodes dans l'ordre :

      1. `urllib` + Cloudflare speed endpoints (primaire — rapide, fiable,
         aucune dépendance externe, marche toujours tant qu'on a internet)
      2. API Python `speedtest` (Ookla) — cassée en 2026 à cause d'un bug
         upstream (get_best_server renvoie une liste vide), mais on la
         garde en fallback au cas où Cloudflare serait inaccessible.

    Analogie : une balance de cuisine (Cloudflare) et une balance Ookla qui
    a perdu l'aiguille. On pèse à la balance de cuisine d'abord. Si elle
    est en panne, on essaie Ookla, avec peu d'espoir.
    """
    def _d(msg):
        """Double-log : via logging + via fichier debug brut."""
        log.info(msg)
        _dbg_write(msg)

    _d(f"run_speedtest START / VERSION={VERSION} HAS_SPEEDTEST_PY={HAS_SPEEDTEST_PY} "
       f"source={_speedtest_source}")

    # --- Méthode 1 : urllib + Cloudflare (primaire, fiable) ---
    _d("cloudflare: starting (primary)")
    down, up = run_speedtest_urllib()
    if down is not None and up is not None:
        _d(f"cloudflare: OK down={down:.1f} up={up:.1f}")
        return down, up
    _d(f"cloudflare: partial (down={down}, up={up}), trying fallback")

    # --- Méthode 2 : Ookla Python module (fallback) ---
    # Note : cassé upstream depuis 2024/2025. On tente quand même, pour le
    # jour où le maintainer fixera get_best_server(). En cas d'échec, on
    # garde ce que Cloudflare nous a donné (même partiel).
    if HAS_SPEEDTEST_PY:
        _d("ookla: fallback attempt")
        try:
            s = speedtest_py.Speedtest(secure=True, timeout=15)
            s.get_best_server()
            d2 = s.download() / 1_000_000.0
            u2 = s.upload(pre_allocate=False) / 1_000_000.0
            _d(f"ookla: OK down={d2:.1f} up={u2:.1f}")
            return d2, u2
        except BaseException as exc:
            _d(f"ookla: EXC {type(exc).__name__}: {exc}")
    else:
        _d("ookla: skipped (HAS_SPEEDTEST_PY=False)")

    # On retourne ce qu'on a de Cloudflare (peut-être None, peut-être partiel)
    return down, up


# -----------------------------------------------------------------------------
# Score de santé 0-1
# -----------------------------------------------------------------------------

_CRITICAL_REACH_REASONS = {
    "pas de route", "réseau injoignable", "host down",
    "iface sans IP", "iface inutilisable", "no device", "iface inconnue",
}


def compute_health(
    latency_ms, loss_pct, down_mbps,
    *,
    medium_skip=None, medium_error=None,
    wifi_rssi=None, wifi_snr=None,
):
    """
    Score synthétique 0-1 par interface.

    Pondération de base : latence 40 %, perte 30 %, débit 30 %. Le débit utilise
    en priorité le medium probe EWMA (sonde par interface) ; sinon le speedtest
    de la route active s'il est passé en argument ; sinon score neutre 0.5.

    Court-circuits :
      - last_skip ou last_error 'critique' (pas de route, réseau injoignable…) → 0
      - latence None ET perte ≥ 99 % → 0 (réseau totalement mort)

    Bonus signal Wi-Fi (cumulé avec le score, capé à 1.0) : un RSSI > -55 dBm
    et un SNR > 30 dB ajoutent jusqu'à +0.05 — récompense un bon lien radio
    sans le faire dominer la mesure transport.
    """
    # Court-circuit dur : interface marquée injoignable par le pre-check
    skip = (medium_skip or "").lower()
    err = (medium_error or "").lower()
    for reason in _CRITICAL_REACH_REASONS:
        if reason in skip or reason in err:
            return 0.0

    if latency_ms is None and loss_pct >= 99:
        return 0.0

    if latency_ms is None:
        lat_score = 0.0
    elif latency_ms <= LATENCY_GREAT_MS:
        lat_score = 1.0
    elif latency_ms >= LATENCY_BAD_MS:
        lat_score = 0.0
    else:
        lat_score = 1.0 - (latency_ms - LATENCY_GREAT_MS) / (
            LATENCY_BAD_MS - LATENCY_GREAT_MS
        )

    loss_score = max(0.0, 1.0 - loss_pct / LOSS_BAD_PCT)

    if down_mbps is None:
        dl_score = 0.5
    else:
        dl_score = min(1.0, down_mbps / DOWNLOAD_GREAT_MBPS)

    score = 0.4 * lat_score + 0.3 * loss_score + 0.3 * dl_score

    # Bonus signal radio (Wi-Fi) — petit, plafonné, pas de pénalité si absent.
    if wifi_rssi is not None and wifi_rssi >= -55:
        score += 0.025
    if wifi_snr is not None and wifi_snr >= 30:
        score += 0.025

    return max(0.0, min(1.0, score))


# -----------------------------------------------------------------------------
# Icône menu-bar
# -----------------------------------------------------------------------------

def _health_bullet(health):
    """Glyphe daltonien-safe : remplissage progressif d'un cercle.
    ✕ down → ◌ pauvre → ○ correct → ◎ bon → ◉ excellent.
    """
    if health is None or health <= 0:
        return "✕"
    if health < 0.35:
        return "◌"
    if health < 0.6:
        return "○"
    if health < 0.85:
        return "◎"
    return "◉"


def _interpolate_color(health):
    """
    health ∈ [0, 1] → (R, G, B). Palette daltonien-safe (pas de rouge/vert),
    inspirée de viridis : bleu profond → turquoise → jaune doré.
    0.0 = bleu sombre (réseau mort), 0.5 = turquoise (passable),
    1.0 = jaune doré (excellent).
    """
    health = max(0.0, min(1.0, health))
    # 3 stops : (50,40,120) → (60,170,170) → (240,210,80)
    if health < 0.5:
        t = health * 2
        r = int(50 + (60 - 50) * t)
        g = int(40 + (170 - 40) * t)
        b = int(120 + (170 - 120) * t)
    else:
        t = (health - 0.5) * 2
        r = int(60 + (240 - 60) * t)
        g = int(170 + (210 - 170) * t)
        b = int(170 + (80 - 170) * t)
    return (r, g, b)


def draw_icon(path, interfaces, default_device=None):
    """Dispatcher selon ICON_STYLE. Voir _draw_icon_pie/gauge/radar pour les
    variantes. Tous partagent la palette daltonien-safe (viridis)."""
    if ICON_STYLE == "gauge":
        return _draw_icon_gauge(path, interfaces, default_device)
    if ICON_STYLE == "radar":
        return _draw_icon_radar(path, interfaces, default_device)
    return _draw_icon_pie(path, interfaces, default_device)


def _draw_icon_gauge(path, interfaces, default_device=None):
    """
    Variante "tableau de bord" : barres verticales côte à côte.
    1 barre par interface (max 5), hauteur = santé, couleur = viridis.
    L'iface route active est surmontée d'un petit triangle blanc.
    Lecture immédiate : plus une barre est haute, plus elle est saine.
    """
    size = ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = 4
    if not interfaces:
        # Anneau gris ténu = "rien à montrer"
        draw.ellipse((pad, pad, size - pad, size - pad),
                     outline=(160, 160, 160, 180), width=2)
        img.save(path)
        return

    ifaces = interfaces[:5]
    n = len(ifaces)
    band_h = size - 2 * pad
    base_y = size - pad
    top_y = pad

    # Largeur de barre + interstice : on garde une gouttière de 2 px entre barres.
    gap = 2
    total_w = size - 2 * pad
    bar_w = max(2, (total_w - gap * (n - 1)) // n)
    block_w = bar_w * n + gap * (n - 1)
    x0 = (size - block_w) / 2

    # Rail de fond (où s'inscrivent les barres) — discret pour donner le
    # contexte "ça pourrait monter jusque là".
    rail_color = (140, 140, 140, 70)
    for i in range(n):
        bx = x0 + i * (bar_w + gap)
        draw.rectangle((bx, top_y, bx + bar_w - 1, base_y - 1), fill=rail_color)

    for i, iface in enumerate(ifaces):
        h_raw = iface.get("health")
        if h_raw is None:
            h, color = 0.35, (150, 150, 150, 220)
        else:
            h = max(0.0, min(1.0, h_raw))
            color = _interpolate_color(h) + (255,)
        bx = x0 + i * (bar_w + gap)
        bar_h = max(2, int((band_h - 2) * h))
        y_top = base_y - bar_h
        draw.rectangle((bx, y_top, bx + bar_w - 1, base_y - 1), fill=color)

        # Marqueur route active : petit triangle blanc au-dessus de la barre.
        if iface.get("device") == default_device:
            tip_y = max(top_y, y_top - 4)
            cx_bar = bx + bar_w / 2
            draw.polygon(
                [(cx_bar - 2, tip_y), (cx_bar + 2, tip_y), (cx_bar, tip_y + 3)],
                fill=(255, 255, 255, 255),
            )

    img.save(path)


def _draw_icon_radar(path, interfaces, default_device=None):
    """
    Variante "scanner" : cercle fin + rayons internes partant d'un point central.
    1 rayon par iface, longueur = santé, couleur = viridis. L'iface route active
    a un petit disque blanc à son extrémité. Évoque la portée d'un signal.
    """
    size = ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = 3
    cx = cy = size / 2
    max_r = (size - 2 * pad) / 2 - 1

    # Cercle extérieur ténu : la "portée maximale possible".
    draw.ellipse((pad, pad, size - pad, size - pad),
                 outline=(140, 140, 140, 110), width=1)

    if not interfaces:
        img.save(path)
        return

    # Petit disque central pour ancrer l'œil.
    cr = 2
    draw.ellipse((cx - cr, cy - cr, cx + cr, cy + cr), fill=(200, 200, 200, 220))

    ifaces = interfaces[:6]
    n = len(ifaces)
    import math
    # Distribution angulaire régulière, départ à 12h, sens horaire.
    for i, iface in enumerate(ifaces):
        angle_deg = -90 + i * (360.0 / n)
        h_raw = iface.get("health")
        if h_raw is None:
            h, color = 0.35, (150, 150, 150, 220)
        else:
            h = max(0.0, min(1.0, h_raw))
            color = _interpolate_color(h) + (255,)
        r = max(3, max_r * h)
        ang = math.radians(angle_deg)
        ex = cx + r * math.cos(ang)
        ey = cy + r * math.sin(ang)
        draw.line((cx, cy, ex, ey), fill=color, width=2)

        if iface.get("device") == default_device:
            draw.ellipse((ex - 2, ey - 2, ex + 2, ey + 2),
                         fill=(255, 255, 255, 255))

    img.save(path)


def _draw_icon_pie(path, interfaces, default_device=None):
    """
    Variante historique : pie-chart à N quartiers.
    Chaque quartier se remplit radialement selon la santé. L'iface route
    active est marquée par un arc blanc sur le périmètre extérieur.
    """
    size = ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    padding = 3
    outer_box = (padding, padding, size - padding, size - padding)
    cx = cy = size / 2
    max_r = (size - 2 * padding) / 2 - 1   # rayon max atteignable par un quartier
    min_r = 3                              # rayon plancher (même à 0 on voit qqch)

    # --- Cas 0 : aucune interface ---
    if not interfaces:
        draw.ellipse(outer_box, outline=(160, 160, 160, 180), width=2)
        img.save(path)
        return

    # --- Cas 1 : mode historique, pastille qui grossit ---
    if len(interfaces) == 1:
        iface = interfaces[0]
        h = max(0.0, min(1.0, iface.get("health") or 0.0))
        draw.ellipse(outer_box, outline=(130, 130, 130, 220), width=1)
        r = min_r + (max_r - min_r - 1) * h
        color = _interpolate_color(h) + (255,)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
        img.save(path)
        return

    # --- Cas N : pie-chart avec quartiers qui se remplissent radialement ---

    # Anneau extérieur gris ténu : sert de repère global pour l'œil, même
    # quand tous les quartiers sont petits.
    draw.ellipse(outer_box, outline=(130, 130, 130, 110), width=1)

    n = len(interfaces)
    slice_angle = 360.0 / n
    start = -90.0   # démarrage à 12h, sens horaire

    default_arc_range = None   # (start, end) à tracer en blanc après les quartiers

    for iface in interfaces:
        end = start + slice_angle
        h_raw = iface.get("health")

        if h_raw is None:
            # Interface standby (iPhone branché, pas d'IP utile) : on rend un
            # quartier gris moyen, à ~35 % du rayon max — suffisamment
            # présent pour dire "j'existe en backup", sans noyer la lecture.
            r = min_r + (max_r - min_r - 1) * 0.35
            color = (150, 150, 150, 200)
        else:
            h = max(0.0, min(1.0, h_raw))
            r = min_r + (max_r - min_r - 1) * h
            color = _interpolate_color(h) + (255,)

        # pieslice dans une box de rayon r centrée → le quartier ne va que
        # jusqu'à r, pas jusqu'au bord du cercle extérieur.
        slice_box = (cx - r, cy - r, cx + r, cy + r)
        draw.pieslice(slice_box, start, end, fill=color)

        if iface.get("device") == default_device:
            default_arc_range = (start, end)

        start = end

    # Marqueur de la route par défaut : uniquement un arc blanc sur la
    # portion du périmètre extérieur correspondant au quartier (pas
    # d'entourage des côtés radiaux).
    if default_arc_range:
        draw.arc(
            outer_box,
            default_arc_range[0],
            default_arc_range[1],
            fill=(255, 255, 255, 255),
            width=2,
        )

    img.save(path)


# -----------------------------------------------------------------------------
# App rumps
# -----------------------------------------------------------------------------

class NetworkHealthApp(rumps.App):
    def __init__(self):
        super().__init__("NetHealth", quit_button=None)

        self._icon_dir = Path(tempfile.gettempdir())
        self._icon_counter = 0   # pour alterner le nom du fichier à chaque tick
        self.icon_path = str(self._icon_dir / "nethealth_icon_0.png")

        # IMPORTANT : on DÉCLENCHE la popup de permission Location au démarrage
        # de l'app. La strong ref sur self._loc_manager est indispensable.
        self._loc_manager = request_location_permission()

        # État courant — multi-interfaces
        self.interfaces = []           # list[dict] : cf. list_active_interfaces()
        self.health = 0.0              # santé de la route par défaut
        self.tethered = False          # legacy : True si route par défaut = mobile
        self.download_mbps = None      # speedtest sur la route par défaut
        self.upload_mbps = None
        self.last_speedtest = 0.0
        self._lock = threading.Lock()
        self._speedtest_running = False  # garde-fou anti-double-lancement

        # TP-Link M8550 — client + dernière mesure + odomètre data au démarrage
        self._tplink = tplink_client.TplinkClient() if HAS_TPLINK else None
        self.tplink_metrics = None
        self._tplink_data_baseline = None  # bytes consommés au boot, pour delta session
        # Fenêtre glissante 1h des relevés data du M8550 → débit horaire de conso
        self._tplink_data_history = []  # list[(ts_epoch, bytes_total)]

        # Sondes medium : round-robin entre interfaces ready, métriques par device
        self._medium_rr_idx = 0
        self._last_medium_probe_at = 0.0
        # device → {"down_mbps_ewma","down_mbps_last","last_medium_at","samples","last_error","last_skip"}
        self._iface_metrics = {}
        self._eco_mode_active = False  # True quand le budget data 5G est dépassé

        # Items de menu — on garde une référence pour pouvoir updater les titres.
        # IMPORTANT : rumps rend en gris foncé les items SANS callback (jugés
        # « désactivés » par macOS). Pour rester lisible sur fond sombre, on
        # donne un no-op callback aux items info → ils passent en blanc.
        def _noop(_): pass

        self.item_version = rumps.MenuItem(f"NetHealth v{VERSION}", callback=_noop)
        self.item_default = rumps.MenuItem("Route active : —", callback=_noop)
        # Jusqu'à 5 slots d'interfaces. Chacun est cliquable pour basculer.
        self.item_iface_slots = [
            rumps.MenuItem("", callback=self._on_iface_slot_clicked)
            for _ in range(5)
        ]
        self.item_signal = rumps.MenuItem("Signal Wi-Fi : —", callback=_noop)
        self.item_download = rumps.MenuItem("Download (route active) : —", callback=_noop)
        self.item_upload = rumps.MenuItem("Upload (route active) : —", callback=_noop)

        # ---- Section sondes ----
        self.item_probe_mode = rumps.MenuItem("🚦 Sondes : standard", callback=_noop)
        self.item_probe_budget = rumps.MenuItem(
            f"  Budget 5G : 0 Mo/h (seuil {TPLINK_BUDGET_MB_PER_HOUR} Mo/h)",
            callback=_noop,
        )

        # ---- Section TP-Link M8550 (5G) ----
        self.item_tplink_header = rumps.MenuItem("📡 TP-Link M8550 : —", callback=_noop)
        self.item_tplink_isp = rumps.MenuItem("  Réseau : —", callback=_noop)
        self.item_tplink_speed = rumps.MenuItem("  Live : —", callback=_noop)
        self.item_tplink_data = rumps.MenuItem("  Data consommée : —", callback=_noop)
        self.item_tplink_session = rumps.MenuItem("  Δ depuis NetHealth : —", callback=_noop)
        self.item_tplink_setpw = rumps.MenuItem(
            "  Configurer mot de passe routeur…", callback=self._set_tplink_password
        )
        self.item_tplink_refresh = rumps.MenuItem(
            "  Rafraîchir TP-Link", callback=self._refresh_tplink_now
        )
        self.item_refresh = rumps.MenuItem(
            "Rafraîchir maintenant", callback=self._manual_refresh
        )
        self.item_speedtest = rumps.MenuItem(
            "Lancer speedtest maintenant", callback=self._manual_speedtest
        )
        self.item_diagnostic = rumps.MenuItem(
            "Diagnostic…", callback=self._show_diagnostic
        )
        self.item_open_log = rumps.MenuItem(
            "Ouvrir le log", callback=self._open_log
        )
        self.item_open_location_prefs = rumps.MenuItem(
            "⚠️  Réglages Location (perm refusée)…",
            callback=self._open_location_prefs,
        )
        self.item_quit = rumps.MenuItem("Quitter", callback=rumps.quit_application)

        menu_items = [
            self.item_version,
            self.item_default,
            None,
            *self.item_iface_slots,
            None,
            self.item_probe_mode,
            self.item_probe_budget,
            None,
            self.item_signal,
            self.item_download,
            self.item_upload,
        ]
        if HAS_TPLINK:
            menu_items += [
                None,
                self.item_tplink_header,
                self.item_tplink_isp,
                self.item_tplink_speed,
                self.item_tplink_data,
                self.item_tplink_session,
                self.item_tplink_refresh,
                self.item_tplink_setpw,
            ]
        menu_items += [
            None,
            self.item_refresh,
            self.item_speedtest,
            self.item_diagnostic,
            self.item_open_log,
        ]
        # Raccourci préférences Localisation : visible UNIQUEMENT si la perm
        # est refusée (CoreWLAN renvoie <redacted>). Sinon inutile, on n'encombre pas.
        if location_permission_denied():
            menu_items.append(self.item_open_location_prefs)
        menu_items += [
            None,
            self.item_quit,
        ]
        self.menu = menu_items

        # Icône initiale (grise, vide)
        self._refresh_icon()

        # Thread de monitoring qui tourne en permanence
        self._stop = threading.Event()
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    # --- UI update ---------------------------------------------------------

    def _refresh_icon(self):
        # On alterne entre deux fichiers pour forcer rumps/Cocoa à recharger
        # l'image (sinon NSImage cache le PNG et l'icône ne bouge plus).
        self._icon_counter = (self._icon_counter + 1) % 2
        self.icon_path = str(self._icon_dir / f"nethealth_icon_{self._icon_counter}.png")
        default_dev = next(
            (i["device"] for i in self.interfaces if i.get("is_default")), None
        )
        try:
            draw_icon(self.icon_path, self.interfaces, default_device=default_dev)
        except Exception:
            log.exception("draw_icon a planté")
            return

        try:
            self.template = False
            self.title = None
            self.icon = self.icon_path
        except Exception:
            log.exception("setter rumps sur l'icône a planté")

    def _refresh_menu(self):
        try:
            # --- Route par défaut ---
            default_iface = next(
                (i for i in self.interfaces if i.get("is_default")), None
            )
            if default_iface:
                label = default_iface["port"]
                if default_iface.get("ssid"):
                    label += f" · {default_iface['ssid']}"
                self.item_default.title = f"Route active : {label}"
            elif self.interfaces:
                self.item_default.title = "Route active : (indéterminée)"
            else:
                self.item_default.title = "Route active : aucune connexion"

            # --- Une ligne par interface (cliquable = bascule) ---
            for i, slot in enumerate(self.item_iface_slots):
                if i < len(self.interfaces):
                    iface = self.interfaces[i]
                    default_star = "★" if iface.get("is_default") else "↪"
                    mobile_tag = " 📶" if iface.get("is_mobile") else ""
                    if not iface.get("is_ready"):
                        slot.title = (
                            f"↪ ⚪ {iface['port']}{mobile_tag}"
                            f" · standby (prête en secours)"
                        )
                    else:
                        pct = int(round((iface.get("health") or 0) * 100))
                        bullet = _health_bullet(iface.get("health") or 0)
                        lat = iface.get("latency_ms")
                        loss = iface.get("loss_pct", 100.0)
                        lat_txt = f"{lat:.0f}ms" if lat is not None else "—"
                        loss_txt = f"{loss:.0f}%" if loss is not None else "—"
                        # Débit medium par interface si dispo + raison concise
                        m = self._iface_metrics.get(iface.get("device")) or {}
                        if m.get("down_mbps_ewma"):
                            dbg = f" · {m['down_mbps_ewma']:.0f} Mbps↓"
                        elif m.get("last_skip"):
                            dbg = f" · (skip: {m['last_skip']})"
                        elif m.get("last_error"):
                            err = (m["last_error"] or "")[:50]
                            dbg = f" · medium err: {err}"
                        else:
                            dbg = " · (medium en attente)"
                        slot.title = (
                            f"{default_star} {bullet} {iface['port']}{mobile_tag}"
                            f" · {pct}% · {lat_txt} · perte {loss_txt}{dbg}"
                        )
                else:
                    slot.title = ""  # slot vide

            # --- Signal Wi-Fi (reste global, sur le Wi-Fi actif peu importe default) ---
            stats = wifi_stats_via_corewlan()
            if stats:
                parts = []
                if "rssi" in stats:
                    parts.append(f"{stats['rssi']} dBm")
                if "snr" in stats:
                    parts.append(f"SNR {stats['snr']} dB")
                if "tx_mbps" in stats:
                    parts.append(f"{stats['tx_mbps']:.0f} Mbps radio")
                self.item_signal.title = (
                    "Signal Wi-Fi : " + " · ".join(parts) if parts else "Signal Wi-Fi : —"
                )
            else:
                self.item_signal.title = "Signal Wi-Fi : — (CoreWLAN indispo)"

            # --- Speedtest (sur la route par défaut) ---
            if self.download_mbps is not None:
                self.item_download.title = (
                    f"Download (route active) : {self.download_mbps:.1f} Mbps"
                )
            else:
                self.item_download.title = "Download (route active) : — (en attente)"
            if self.upload_mbps is not None:
                self.item_upload.title = (
                    f"Upload (route active) : {self.upload_mbps:.1f} Mbps"
                )
            else:
                self.item_upload.title = "Upload (route active) : —"

            # --- Mode sondes + budget 5G ---
            consumed = self._tplink_consumed_mb_per_hour()
            mode = "économique" if self._eco_mode_active else "standard"
            self.item_probe_mode.title = f"🚦 Sondes : {mode}"
            self.item_probe_budget.title = (
                f"  Budget 5G : {consumed:.0f} Mo/h "
                f"(seuil {TPLINK_BUDGET_MB_PER_HOUR} Mo/h)"
            )

            # --- TP-Link M8550 ---
            # Isolé : si la section TP-Link explose, le reste du menu reste OK.
            if HAS_TPLINK:
                try:
                    self._refresh_tplink_menu()
                except Exception:
                    log.exception("_refresh_tplink_menu a planté (isolé)")
        except Exception:
            log.exception("_refresh_menu a planté")

    def _refresh_tplink_menu(self):
        m = self.tplink_metrics
        if m is None:
            self.item_tplink_header.title = "📡 TP-Link M8550 : — (en attente)"
            self.item_tplink_isp.title = "  Réseau : —"
            self.item_tplink_speed.title = "  Live : —"
            self.item_tplink_data.title = "  Data consommée : —"
            self.item_tplink_session.title = "  Δ depuis NetHealth : —"
            return
        if not m.available:
            self.item_tplink_header.title = f"📡 TP-Link : indisponible"
            self.item_tplink_isp.title = f"  {m.error or 'erreur inconnue'}"
            self.item_tplink_speed.title = "  Live : —"
            self.item_tplink_data.title = "  Data consommée : —"
            self.item_tplink_session.title = "  Δ depuis NetHealth : —"
            return

        model = m.firmware_model or "M8550"
        status = m.connect_status or "?"
        self.item_tplink_header.title = f"📡 TP-Link {model} · {status}"

        isp_bits = []
        if m.isp:
            isp_bits.append(m.isp)
        if m.network_type:
            isp_bits.append(m.network_type)
        if m.wan_conntype:
            isp_bits.append(m.wan_conntype)
        self.item_tplink_isp.title = "  Réseau : " + (" · ".join(isp_bits) or "—")

        self.item_tplink_speed.title = (
            f"  Live : ↓ {tplink_client.fmt_bps(m.live_down_bps)}  "
            f"↑ {tplink_client.fmt_bps(m.live_up_bps)}"
        )
        self.item_tplink_data.title = (
            f"  Data consommée : {tplink_client.fmt_bytes(m.data_consumed_bytes)}"
        )
        if m.data_consumed_bytes is not None and self._tplink_data_baseline is not None:
            delta = max(0, m.data_consumed_bytes - self._tplink_data_baseline)
            self.item_tplink_session.title = (
                f"  Δ depuis NetHealth : {tplink_client.fmt_bytes(delta)}"
            )
        else:
            self.item_tplink_session.title = "  Δ depuis NetHealth : —"

    # --- TP-Link callbacks -------------------------------------------------

    def _set_tplink_password(self, _):
        if not HAS_TPLINK:
            return
        win = rumps.Window(
            title="Mot de passe routeur TP-Link",
            message="Mot de passe admin de http://192.168.1.1\n(stocké dans Keychain macOS, jamais en clair)",
            default_text="",
            ok="Enregistrer",
            cancel="Annuler",
            secure=True,
            dimensions=(280, 24),
        )
        rep = win.run()
        if not rep.clicked or not rep.text.strip():
            return
        if tplink_client.keychain_set_password(rep.text.strip()):
            rumps.notification("NetHealth", "TP-Link", "Mot de passe enregistré dans le Keychain.")
            # Force un refresh immédiat avec les nouveaux credentials
            if self._tplink is not None:
                self._tplink._close_router()
            threading.Thread(target=self._refresh_tplink_now, args=(None,), daemon=True).start()
        else:
            rumps.alert("Échec", "Impossible d'écrire dans le Keychain. Voir le log.")

    def _refresh_tplink_now(self, _):
        if self._tplink is None:
            return
        try:
            m = self._tplink.fetch(force=True)
            with self._lock:
                self.tplink_metrics = m
                if m.available and m.data_consumed_bytes is not None and self._tplink_data_baseline is None:
                    self._tplink_data_baseline = m.data_consumed_bytes
        except Exception:
            log.exception("refresh tplink manuel a planté")
        _call_on_main(self._refresh_menu)

    # --- Callbacks menu ----------------------------------------------------

    def _manual_refresh(self, _):
        # Lance un tick hors main thread. _tick() dispatche lui-même ses
        # updates UI sur le main thread via _call_on_main().
        threading.Thread(target=self._tick, daemon=True, name="manual-refresh").start()

    def _on_iface_slot_clicked(self, sender):
        """Clic sur une ligne d'interface → propose de basculer la route vers
        elle (sauf si elle est déjà active ou en standby)."""
        idx = None
        for i, slot in enumerate(self.item_iface_slots):
            if slot is sender:
                idx = i
                break
        if idx is None or idx >= len(self.interfaces):
            return
        iface = self.interfaces[idx]

        if iface.get("is_default"):
            rumps.alert(
                title="Déjà active",
                message=f"{iface['port']} est déjà la route par défaut.",
            )
            return
        if not iface.get("is_ready"):
            rumps.alert(
                title="Interface en standby",
                message=(
                    f"{iface['port']} est présente mais pas prête (pas d'IP "
                    "routable). Active son Partage de connexion côté iPhone "
                    "avant de basculer dessus."
                ),
            )
            return

        # Confirmation explicite : bascule = admin password requis
        res = rumps.alert(
            title="Basculer la route active ?",
            message=(
                f"Rendre « {iface['port']} » la route par défaut ?\n\n"
                "macOS va demander ton mot de passe administrateur "
                "(une fois par session)."
            ),
            ok="Basculer", cancel="Annuler",
        )
        if not res:
            return
        threading.Thread(
            target=self._switch_default_route,
            args=(iface["port"],),
            daemon=True, name="switch-route",
        ).start()

    def _switch_default_route(self, target_port_name):
        """Réordonne la liste des services réseau pour que `target_port_name`
        passe en 1er → macOS en fait la route par défaut automatiquement.

        On passe par osascript + `with administrator privileges` pour avoir
        la popup mdp standard. macOS garde le droit sudo ~5 min, donc une
        session de bascules multiples ne redemandera pas le mdp.
        """
        log.info("demande de bascule vers : %s", target_port_name)
        try:
            out = subprocess.run(
                ["networksetup", "-listnetworkserviceorder"],
                capture_output=True, text=True, timeout=3,
            ).stdout
        except Exception:
            log.exception("listnetworkserviceorder a planté")
            return

        services = []
        for line in out.splitlines():
            m = re.match(r"\(\d+\)\s+(.+)", line.strip())
            if m:
                name = m.group(1).strip()
                if not name.startswith("*"):   # * = désactivé
                    services.append(name)

        if target_port_name not in services:
            _call_on_main(lambda: rumps.alert(
                "Erreur",
                f"Service réseau « {target_port_name} » introuvable dans "
                f"l'ordre courant.\nServices connus : {', '.join(services)}",
            ))
            return

        services.remove(target_port_name)
        services.insert(0, target_port_name)
        quoted = " ".join(f'\\"{s}\\"' for s in services)
        apple_script = (
            f'do shell script "networksetup -ordernetworkservices {quoted}" '
            f'with administrator privileges'
        )
        try:
            res = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True, text=True, timeout=60,
            )
            if res.returncode != 0:
                log.warning("osascript bascule returncode=%d stderr=%s",
                            res.returncode, res.stderr.strip())
                _call_on_main(lambda: rumps.alert(
                    "Bascule annulée",
                    f"L'ordre n'a pas pu être modifié.\n{res.stderr.strip()}",
                ))
                return
        except Exception:
            log.exception("osascript a planté")
            return

        log.info("bascule effectuée vers %s", target_port_name)
        # Force un tick pour voir immédiatement le changement dans l'UI
        self._tick()

    def _open_log(self, _):
        """Ouvre le log dans Console.app — view en live avec suivi."""
        try:
            subprocess.run(["open", "-a", "Console", str(LOG_FILE)], timeout=3)
        except Exception:
            log.exception("impossible d'ouvrir le log")

    def _open_location_prefs(self, _):
        """Ouvre directement le panneau Localisation dans Réglages système."""
        try:
            subprocess.run(
                ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices"],
                timeout=3,
            )
        except Exception:
            log.exception("impossible d'ouvrir les réglages Location")

    def _show_diagnostic(self, _):
        dev = _wifi_device() or "(aucun)"
        via_cw = _ssid_via_corewlan()
        raw_ssid = _ssid_via_corewlan_raw()
        wifi_stats = wifi_stats_via_corewlan()
        iphone_devs = _list_iphone_devices()
        ssid_final = current_wifi_ssid()
        permission_warning = ""
        if raw_ssid == REDACTED_MARKER:
            permission_warning = (
                "\n\n⚠️  CoreWLAN renvoie '<redacted>' au lieu du SSID.\n"
                "→ Permission Location Services refusée.\n"
                "→ Réglages système → Confidentialité & sécurité →\n"
                "  Services de localisation → active NetHealth\n"
                "→ Puis relance l'app."
            )

        # Petite ligne par SSID configuré : match par préfixe après normalisation
        match_lines = []
        norm_current = _normalize_ssid(ssid_final)
        for t in TETHERING_SSIDS:
            nt = _normalize_ssid(t)
            ok = bool(nt) and bool(norm_current) and (
                norm_current == nt or norm_current.startswith(nt)
            )
            match_lines.append(f"  {'✅' if ok else '❌'} préfixe {t!r}")

        corewlan_line = (
            f"(dispo : {HAS_COREWLAN})" if not via_cw else f"→ {via_cw!r}"
        )
        stats_str = ", ".join(f"{k}={v}" for k, v in wifi_stats.items()) or "—"
        loc_code, loc_label = location_auth_status()
        default_dev = default_route_device() or "(aucun)"
        iface_list = list_active_interfaces()
        iface_lines = []
        for i in iface_list:
            tag = "★" if i.get("is_default") else " "
            mobile = " 📶" if i.get("is_mobile") else ""
            ssid_part = f" {i.get('ssid')!r}" if i.get("ssid") else ""
            iface_lines.append(
                f"  {tag} {i['device']} ({i['port']}){mobile}{ssid_part} → {i['ipv4']}"
            )
        msg = (
            f"NetHealth v{VERSION}\n"
            f"Permission Location : {loc_label} (code {loc_code})\n"
            f"Route par défaut : {default_dev}\n"
            f"Interfaces actives :\n" + "\n".join(iface_lines or ["  (aucune)"]) + "\n\n"
            f"Wi-Fi device : {dev}\n"
            f"SSID via CoreWLAN : {corewlan_line}\n"
            f"CoreWLAN brut (avant filtre) : {raw_ssid!r}\n"
            f"SSID retenu   : {ssid_final!r}\n"
            f"SSID normalisé: {norm_current!r}\n"
            f"Stats Wi-Fi   : {stats_str}\n"
            f"iPhone devices : {', '.join(iphone_devs) or '—'}\n"
            f"Tethered : {is_on_tethered_network()}\n"
            f"\nMatch avec les SSIDs configurés :\n" + "\n".join(match_lines)
            + permission_warning
        )
        rumps.alert(title="Diagnostic réseau", message=msg)

    def _manual_speedtest(self, _):
        # Anti-double-run : un speedtest à la fois suffit amplement
        if self._speedtest_running:
            rumps.notification(
                "Network Health",
                "Speedtest déjà en cours",
                "Attends la fin du speedtest actuel avant d'en lancer un autre.",
            )
            return

        if not self.interfaces:
            rumps.notification(
                "Network Health",
                "Aucune interface active",
                "Aucune connexion détectée pour lancer le speedtest.",
            )
            return

        def run():
            self._speedtest_running = True
            # Visuel "en cours" — dispatché sur main thread
            def _show_running():
                try:
                    self.item_download.title = "Download : ⏳ speedtest en cours…"
                    self.item_upload.title = "Upload : ⏳ speedtest en cours…"
                except Exception:
                    log.exception("update UI 'en cours' a planté")
            _call_on_main(_show_running)

            try:
                down, up = run_speedtest()
            except Exception:
                log.exception("speedtest manuel a planté")
                down, up = None, None
            finally:
                with self._lock:
                    self.download_mbps = down
                    self.upload_mbps = up
                    self.last_speedtest = time.time()
                self._speedtest_running = False
                _call_on_main(self._refresh_menu)

        threading.Thread(target=run, daemon=True, name="speedtest").start()

    # --- Boucle de monitoring ---------------------------------------------

    # --- Probes / budget helpers -------------------------------------------

    def _tplink_consumed_mb_per_hour(self) -> float:
        """Renvoie les MB consommés sur le M8550 dans la dernière heure
        (extrapolé linéairement si on a < 1 h d'historique)."""
        h = self._tplink_data_history
        if len(h) < 2:
            return 0.0
        t0, b0 = h[0]
        t1, b1 = h[-1]
        dt = max(1.0, t1 - t0)  # secondes
        delta_mb = max(0, b1 - b0) / 1_000_000
        return delta_mb * 3600 / dt

    def _maybe_run_medium_probe(self, interfaces):
        """Sonde medium par round-robin sur les interfaces ready.
        - Phase initiale : on probe chaque iface dans un tick consécutif
          (1 par tick = 30 s d'intervalle), pour avoir des données rapidement
          sur tout le monde.
        - Régime établi : round-robin avec MEDIUM_PROBE_INTERVAL_SEC entre chaque.
        Skippée si éco actif ET interface mobile.
        """
        ready = [i for i in interfaces if i.get("is_ready")]
        if not ready:
            return
        now = time.time()

        # Y a-t-il une iface ready encore JAMAIS probée ? → on pousse vite.
        in_initial_phase = any(
            self._iface_metrics.get(i.get("device"), {}).get("samples", 0) == 0
            for i in ready
        )
        if not in_initial_phase:
            if (now - self._last_medium_probe_at) < MEDIUM_PROBE_INTERVAL_SEC and self._last_medium_probe_at > 0:
                return
            target = ready[self._medium_rr_idx % len(ready)]
            self._medium_rr_idx = (self._medium_rr_idx + 1) % len(ready)
        else:
            # Choisit la 1re iface jamais probée (priorité à la non-default
            # pour avoir des données sur les standby tôt).
            target = next(
                i for i in ready
                if self._iface_metrics.get(i.get("device"), {}).get("samples", 0) == 0
            )

        device = target.get("device")
        if not device:
            return

        m = self._iface_metrics.setdefault(
            device,
            {
                "down_mbps_ewma": None,
                "down_mbps_last": None,
                "last_medium_at": 0.0,
                "samples": 0,
                "last_error": None,
                "last_skip": None,
            },
        )

        if self._eco_mode_active and target.get("is_mobile"):
            m["last_skip"] = "éco actif (budget 5G)"
            log.warning(
                "medium probe SKIP %s (%s) — %s",
                target.get("port"),
                device,
                m["last_skip"],
            )
            self._last_medium_probe_at = now
            return

        # Pre-check 2 s : iface vraiment joignable ? Évite de bloquer 8 s
        # sur un curl pour rien quand le dock n'a pas d'uplink, etc.
        reach_ok, reach_err = iface_can_reach(device)
        if not reach_ok:
            m["last_skip"] = reach_err
            m["last_error"] = None
            m["last_medium_at"] = now
            log.warning(
                "medium probe SKIP %s (%s) — pre-check : %s",
                target.get("port"), device, reach_err,
            )
            self._last_medium_probe_at = now
            return

        log.warning("medium probe RUN %s (%s)", target.get("port"), device)
        result = probe_medium(device)
        m["last_skip"] = None
        m["last_medium_at"] = now
        if result.get("ok"):
            new_v = result["down_mbps"]
            if m["down_mbps_ewma"] is None:
                m["down_mbps_ewma"] = new_v
            else:
                # EWMA α=0.4 : pondère le passé, lisse les pics ponctuels
                m["down_mbps_ewma"] = 0.6 * m["down_mbps_ewma"] + 0.4 * new_v
            m["down_mbps_last"] = new_v
            m["samples"] += 1
            m["last_error"] = None
            log.warning(
                "medium probe OK %s : %.1f Mbps (EWMA %.1f) %d B en %.1fs",
                device,
                new_v,
                m["down_mbps_ewma"],
                result.get("bytes_used", 0),
                result.get("duration_sec", 0),
            )
        else:
            m["last_error"] = result.get("error")
            log.warning("medium probe FAIL %s : %s", device, m["last_error"])
        self._last_medium_probe_at = now

    def _tick(self):
        """Une itération complète : énumération → ping par interface →
        (speedtest sur la route par défaut si dû) → update UI.

        IMPORTANT : ce tick tourne dans un THREAD d'arrière-plan, jamais sur
        le main thread. Donc tous les appels UI passent par _call_on_main().
        """
        try:
            interfaces = list_active_interfaces()
        except Exception:
            log.exception("list_active_interfaces a planté — tick abandonné")
            return

        ifaces_desc = ", ".join(
            f"{i.get('port')}({i.get('device')})ready={i.get('is_ready')}"
            for i in interfaces
        ) or "aucune"
        log.warning("_tick: %d interface(s) — %s", len(interfaces), ifaces_desc)

        if not interfaces:
            with self._lock:
                self.interfaces = []
                self.tethered = False
                self.health = 0.0
            _call_on_main(self._refresh_icon)
            _call_on_main(self._refresh_menu)
            return

        # Ping dédié par interface (en séquentiel — 3-5 interfaces max)
        for iface in interfaces:
            if not iface.get("is_ready"):
                # Interface standby (iPhone branché mais sans IP utile) : rien
                # à mesurer, on laisse les champs à None.
                iface["latency_ms"] = None
                iface["loss_pct"] = None
                continue
            try:
                lat, loss = measure_network(source_ip=iface["ipv4"])
            except Exception:
                log.exception("ping sur %s a planté", iface["device"])
                lat, loss = None, 100.0
            iface["latency_ms"] = lat
            iface["loss_pct"] = loss

        # ---- 1er rafraîchissement : routes + ping immédiats ----
        # On pousse l'UI MAINTENANT, AVANT le speedtest qui peut prendre 90 s.
        # Sinon le menu reste vide pendant tout le 1er tick.
        wifi_stats_now = wifi_stats_via_corewlan()
        for iface in interfaces:
            if not iface.get("is_ready"):
                iface["health"] = None
                continue
            m = self._iface_metrics.get(iface.get("device")) or {}
            is_wifi = iface.get("type") == "wifi"
            iface["health"] = compute_health(
                iface.get("latency_ms"),
                iface.get("loss_pct", 100.0),
                m.get("down_mbps_ewma"),
                medium_skip=m.get("last_skip"),
                medium_error=m.get("last_error"),
                wifi_rssi=wifi_stats_now.get("rssi") if is_wifi else None,
                wifi_snr=wifi_stats_now.get("snr") if is_wifi else None,
            )
        default_iface_now = next(
            (i for i in interfaces if i["is_default"]), interfaces[0]
        )
        with self._lock:
            self.interfaces = list(interfaces)
            self.tethered = default_iface_now.get("is_mobile", False)
            self.health = default_iface_now.get("health") or 0.0

        # TP-Link M8550 : interrogation rapide (cache 30 s) et refresh UI tôt.
        if self._tplink is not None:
            try:
                m = self._tplink.fetch()
                with self._lock:
                    self.tplink_metrics = m
                    if (
                        m.available
                        and m.data_consumed_bytes is not None
                        and self._tplink_data_baseline is None
                    ):
                        self._tplink_data_baseline = m.data_consumed_bytes
                    # Fenêtre glissante 1h pour estimer le débit horaire de conso
                    if m.available and m.data_consumed_bytes is not None:
                        now_ts = time.time()
                        self._tplink_data_history.append((now_ts, m.data_consumed_bytes))
                        cutoff = now_ts - 3600
                        self._tplink_data_history = [
                            (t, b) for t, b in self._tplink_data_history if t >= cutoff
                        ]
                log.warning("tplink fetch available=%s err=%s", m.available, m.error)
            except Exception:
                log.exception("tplink fetch a planté — on continue")

        # ---- Décision mode économique ----
        consumed_mb_h = self._tplink_consumed_mb_per_hour()
        with self._lock:
            self._eco_mode_active = consumed_mb_h > TPLINK_BUDGET_MB_PER_HOUR

        # ---- Sonde "medium" round-robin (1 iface par cycle de 10 min) ----
        self._maybe_run_medium_probe(interfaces)

        _call_on_main(self._refresh_icon)
        _call_on_main(self._refresh_menu)

        # Speedtest auto : seulement si la route par défaut n'est PAS mobile
        # ET si le mode économique n'est pas actif. Sinon on s'appuie sur la
        # sonde medium par interface (50× moins coûteuse).
        default_iface = next(
            (i for i in interfaces if i["is_default"]), interfaces[0]
        )
        now = time.time()
        skip_heavy = default_iface.get("is_mobile") or self._eco_mode_active
        if now - self.last_speedtest > SPEEDTEST_INTERVAL_SEC and not skip_heavy:
            down, up = run_speedtest()
            with self._lock:
                self.download_mbps = down
                self.upload_mbps = up
                self.last_speedtest = now
        elif now - self.last_speedtest > SPEEDTEST_INTERVAL_SEC and skip_heavy:
            log.warning(
                "speedtest auto skipped (mobile=%s eco=%s) — fallback sur medium probes",
                default_iface.get("is_mobile"),
                self._eco_mode_active,
            )
            # On ne reset pas last_speedtest → on retentera dans 30 s si
            # la situation a changé. C'est OK car run_speedtest n'est pas appelée.
            self.last_speedtest = now  # évite de reboucler sur le warning chaque tick

        # 2e passe santé : la route par défaut bénéficie du speedtest, les
        # autres ifaces gardent leur EWMA medium probe.
        wifi_stats_now = wifi_stats_via_corewlan()
        for iface in interfaces:
            if not iface.get("is_ready"):
                iface["health"] = None
                continue
            m = self._iface_metrics.get(iface.get("device")) or {}
            # Priorité au speedtest si dispo sur la route active, sinon EWMA medium
            if iface["is_default"] and self.download_mbps is not None:
                dl = self.download_mbps
            else:
                dl = m.get("down_mbps_ewma")
            is_wifi = iface.get("type") == "wifi"
            iface["health"] = compute_health(
                iface.get("latency_ms"),
                iface.get("loss_pct", 100.0),
                dl,
                medium_skip=m.get("last_skip"),
                medium_error=m.get("last_error"),
                wifi_rssi=wifi_stats_now.get("rssi") if is_wifi else None,
                wifi_snr=wifi_stats_now.get("snr") if is_wifi else None,
            )

        default_iface = next(
            (i for i in interfaces if i["is_default"]), interfaces[0]
        )
        with self._lock:
            self.interfaces = interfaces
            self.tethered = default_iface.get("is_mobile", False)
            self.health = default_iface["health"]

        _call_on_main(self._refresh_icon)
        _call_on_main(self._refresh_menu)

    def _monitor_loop(self):
        # On attend un tout petit peu avant le 1er tick pour laisser l'UI apparaître
        time.sleep(1.0)
        log.info("monitoring loop démarré (interval=%ds)", PING_INTERVAL_SEC)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # Capture toute trace complète dans le log ; le thread continue.
                log.exception("tick a planté — on continue")
            self._stop.wait(PING_INTERVAL_SEC)


def main():
    log.info("=" * 60)
    log.info(">>> NetHealth v%s STARTING <<<", VERSION)
    log.info(">>> Python: %s", sys.version.split()[0])
    log.info(">>> __file__ = %s", __file__)
    log.info(
        ">>> CoreWLAN=%s, speedtest.py=%s (source: %s)%s",
        HAS_COREWLAN,
        HAS_SPEEDTEST_PY,
        _speedtest_source,
        f" / import err: {_speedtest_import_err}" if _speedtest_import_err else "",
    )
    log.warning(
        ">>> HAS_TPLINK=%s%s",
        HAS_TPLINK,
        f" / import err: {_TPLINK_IMPORT_ERR}" if _TPLINK_IMPORT_ERR else "",
    )
    try:
        NetworkHealthApp().run()
    except Exception:
        log.exception("crash fatal de l'app")
        raise


if __name__ == "__main__":
    main()
