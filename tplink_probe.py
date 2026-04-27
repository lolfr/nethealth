"""
tplink_probe.py — Sonde de compatibilité TP-Link M8550
=======================================================

Usage :
    source .venv/bin/activate
    pip install tplinkrouterc6u
    python tplink_probe.py

Te demande le mot de passe admin du routeur, tente toutes les méthodes
connues de la lib `tplinkrouterc6u`, et affiche ce qui sort (ou les erreurs).

Si ce probe renvoie des infos utiles (débit, signal), on sait qu'on peut
intégrer. Sinon, on abandonne l'intégration TP-Link proprement.

Analogie : c'est une « prise de contact diplomatique ». On envoie un
émissaire poli qui essaie de parler au routeur en plusieurs langues,
et on note dans quelle langue il répond. Ensuite seulement on décide
d'ouvrir une ambassade (= intégrer dans l'app).
"""

import getpass
import json
import os
import sys
import traceback

ROUTER_URL = os.environ.get("TPLINK_URL", "http://192.168.1.1")
# M8550 attend "user" ; certains firmwares acceptent aussi "admin".
# On tente dans l'ordre, premier qui passe gagne.
USERNAMES_TO_TRY = [u for u in os.environ.get("TPLINK_USERS", "user,admin").split(",") if u.strip()]

# Liste de toutes les méthodes "accesseurs" potentiellement exposées par la lib.
# On les tente toutes, les échecs sont silencieux dans le log.
METHODS = [
    "get_status",
    "get_firmware",
    "get_wireless",
    "get_lte_status",
    "get_lte_statistics",
    "get_vpn_status",
    "get_ipv4_status",
    "get_ipv6_status",
    "get_ipv4_reservations",
    "get_ipv4_dhcp_leases",
    "get_parental_control_info",
    "get_smart_network",
    "get_tether_client_info",
]


def _pretty(val):
    """Essaie de sérialiser le résultat proprement."""
    try:
        return json.dumps(val.__dict__, default=str, indent=2, ensure_ascii=False)
    except Exception:
        pass
    try:
        return json.dumps(val, default=str, indent=2, ensure_ascii=False)
    except Exception:
        return repr(val)


def main():
    try:
        from tplinkrouterc6u import TplinkRouterProvider
    except ImportError:
        print("❌ La lib n'est pas installée. Fais :")
        print("   pip install tplinkrouterc6u")
        sys.exit(1)

    password = getpass.getpass(f"Mot de passe admin du routeur ({ROUTER_URL}) : ")

    print(f"\n=== Connexion à {ROUTER_URL} via TplinkRouterProvider ===")
    print(f"   Usernames à essayer : {USERNAMES_TO_TRY}")

    router = None
    last_exc = None
    for username in USERNAMES_TO_TRY:
        try:
            router = TplinkRouterProvider.get_client(ROUTER_URL, password, username=username)
            router.authorize()
            print(f"✅ Authentification OK avec username='{username}' — client : {type(router).__name__}")
            break
        except TypeError:
            # Vieille version de la lib qui ne prend pas username= en kwarg.
            # On retombe sur le client EX direct (M8550 = série EX/MR).
            try:
                from tplinkrouterc6u.client.ex import TPLinkEXClient
                router = TPLinkEXClient(ROUTER_URL, password, username=username)
                router.authorize()
                print(f"✅ Authentification OK avec username='{username}' (TPLinkEXClient direct)")
                break
            except Exception as exc:
                last_exc = exc
                print(f"   ✗ {username} → {type(exc).__name__}: {exc}")
                router = None
        except Exception as exc:
            last_exc = exc
            print(f"   ✗ {username} → {type(exc).__name__}: {exc}")
            router = None

    if router is None:
        print(f"\n❌ Échec d'authentification sur tous les usernames testés.")
        print("\nDétail du dernier échec :")
        if last_exc is not None:
            traceback.print_exception(type(last_exc), last_exc, last_exc.__traceback__)
        print("\n→ Astuce : passer d'autres usernames via la variable d'env")
        print("   TPLINK_USERS='user,admin,root' python tplink_probe.py")
        return

    hits = []
    misses = []

    for method in METHODS:
        fn = getattr(router, method, None)
        if fn is None:
            misses.append((method, "méthode absente de cette classe"))
            continue
        try:
            val = fn()
        except Exception as exc:
            misses.append((method, f"{type(exc).__name__}: {exc}"))
            continue
        hits.append((method, val))

    print(f"\n=== Résultats : {len(hits)} méthode(s) OK, {len(misses)} en échec ===\n")

    for method, val in hits:
        print(f"--- ✅ {method}() ---")
        print(_pretty(val))
        print()

    if misses:
        print("--- ❌ Méthodes qui n'ont rien donné ---")
        for method, reason in misses:
            print(f"  {method}: {reason}")

    try:
        router.logout()
    except Exception:
        pass


if __name__ == "__main__":
    main()
