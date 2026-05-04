"""
setup.py — configuration py2app pour bundler `network_health.py`
=================================================================

Usage :
    source .venv/bin/activate
    pip install py2app
    rm -rf build dist            # nettoie les builds précédents
    python setup.py py2app

Le bundle est généré dans ./dist/NetHealth.app — copie-le dans /Applications.

Analogie : py2app fait un « pack-lunch » de Python et de toutes nos libs,
ficelé dans un .app que macOS sait manipuler comme n'importe quelle autre
application (permissions, Services de localisation, démarrage au login,
désinstallation en le jetant à la corbeille).

Notes techniques importantes :
  - CFBundleIdentifier doit être stable et unique (`eu.mylastnight.nethealth`)
    → macOS attache les permissions Location Services à cet identifiant. Si
    tu le changes, tu perdras la permission et devras la redonner.
  - LSUIElement = True → l'app vit UNIQUEMENT dans la barre des menus, pas
    d'icône dans le Dock et pas d'entrée dans ⌘-Tab.
  - NSLocationUsageDescription → texte affiché dans la popup d'autorisation
    Location Services lors du 1er lancement.
  - 'includes' → forcer l'embed des modules que py2app ne détecte pas tout
    seul (CoreWLAN par exemple, qui est importé dynamiquement).
"""

from setuptools import setup

APP_NAME = "NetHealth"
APP_VERSION = "1.24"
APP_BUNDLE_ID = "eu.mylastnight.nethealth"

APP = ["network_health.py"]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": "Network Health",
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        # Menu-bar only app : pas d'icône Dock, pas dans ⌘-Tab
        "LSUIElement": True,
        # macOS 12 Monterey min (CoreWLAN + les PyObjC récents)
        "LSMinimumSystemVersion": "12.0",
        # Textes affichés dans les popups de permission macOS
        "NSLocationUsageDescription": (
            "NetHealth lit le nom du Wi-Fi auquel tu es connecté pour "
            "détecter un partage mobile."
        ),
        "NSLocationWhenInUseUsageDescription": (
            "NetHealth lit le nom du Wi-Fi auquel tu es connecté pour "
            "détecter un partage mobile."
        ),
        # Active le mode dark/light natif
        "NSRequiresAquaSystemAppearance": False,
    },
    # Modules à embarquer explicitement (certains imports dynamiques échappent
    # au scanner de py2app).
    "includes": [
        "CoreWLAN",
        "CoreLocation",
        "rumps",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "speedtest",           # version pip si dispo
        "speedtest_vendor",    # notre copie embarquée (fiable sous py2app)
        "tplink_client",       # wrapper local M8550
    ],
    "packages": [
        "rumps",
        "PIL",
        "tplinkrouterc6u",     # lib pip qui parle au routeur
    ],
    # Icône Finder/Dock générée par generate_app_icon.py — reprend les jauges
    # de la menubar (viridis) sur fond squircle navy.
    "iconfile": "icon.icns",
}

setup(
    app=APP,
    name=APP_NAME,
    version=APP_VERSION,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
