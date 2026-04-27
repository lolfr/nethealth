# NetHealth — santé réseau multi-interface dans la menu bar macOS

Petite app menu bar Python qui mesure et affiche la qualité réseau **par interface** (Wi-Fi, Ethernet, iPhone tethering, dock USB-LAN…) et la consommation 5G d'un routeur TP-Link M8550.

L'idée centrale : sur Mac on a souvent plusieurs liens réseau actifs en même temps, mais macOS ne montre que celui de la route par défaut. NetHealth affiche les **N** liens, donne un score 0-100 par lien, et marque celui qui sort le trafic. Pratique pour décider quand basculer sur la 5G ou un dock filaire.

---

## Sommaire

- [Vue d'ensemble](#vue-densemble)
- [Comment ça marche](#comment-ça-marche)
- [Pré-requis](#pré-requis)
- [Build & installation](#build--installation)
- [Premier lancement](#premier-lancement)
- [Configuration](#configuration)
- [Mise à jour](#mise-à-jour)
- [Désinstallation](#désinstallation)
- [Emplacements utiles](#emplacements-utiles)
- [Dépannage](#dépannage)

---

## Vue d'ensemble

### Ce qui s'affiche dans le menu

```
NetHealth v1.22
Route active : Wi-Fi · Nostromo

★ ◉ Wi-Fi · 94% · 4ms · perte 0% · 45 Mbps↓
↪ ◎ DockCaseAxUSBToLAN · 78% · 8ms · perte 0% · 21 Mbps↓
↪ ✕ iPhone · 0% · 200ms · perte 100% · (skip: pas de route)

🚦 Sondes : standard
  Budget 5G : 0 Mo/h (seuil 100 Mo/h)

Signal Wi-Fi : -54 dBm · SNR 41 dB · 1201 Mbps radio
Download (route active) : 192.0 Mbps
Upload (route active)  : 90.4 Mbps

📡 TP-Link M8550 : 5G NSA · Bouygues
  Réseau : 5G NSA · WAN 80.x.x.x
  Live : 22.4 Mbps↓ / 1.8 Mbps↑
  Data consommée : 4.2 Go
  Δ depuis NetHealth : 12.3 Mo
  Configurer mot de passe routeur…
  Rafraîchir TP-Link

Rafraîchir maintenant
Lancer speedtest maintenant
Diagnostic…
Ouvrir le log
Quitter
```

Lecture en 1 seconde :

- **Glyphes par formes** (daltonien-safe) : `✕ ◌ ○ ◎ ◉` du pire au meilleur
- **★** = route active (par où sort le trafic)
- **↪** = interface UP mais en backup
- **📶** (à côté du nom) = lien mobile (iPhone tethering, Wi-Fi ≈ partage 5G)

### L'icône menu bar

Cinq barres verticales (1 par interface, max 5), hauteur = score, couleur = palette viridis (bleu profond → turquoise → jaune doré). Petit triangle blanc au-dessus de la barre de la route active. Fonds gris ténus = "ça pourrait monter jusqu'ici".

L'icône Finder reprend le même langage visuel sur fond squircle navy.

---

## Comment ça marche

### Boucle de monitoring

Un thread d'arrière-plan tourne en permanence (`_monitor_loop`), et exécute un tick toutes les 30 s :

1. **Énumère les interfaces** macOS via `networksetup -listallhardwareports` et `ifconfig`. Sépare ce qui a une IPv4 routable (`is_ready`) du standby (iPhone branché sans IP utile, Wi-Fi associé sans bail DHCP).
2. **Ping** chaque interface (4 paquets vers Cloudflare 1.1.1.1) en bindant la source via `ping -S <ip>`.
3. **Sonde "medium"** round-robin : 1 interface par cycle de 10 min, télécharge 1 MB Cloudflare via `curl --interface <dev>`. EWMA pour lisser. Précédée d'un **pre-check TCP 2 s** socket-bound (`IP_BOUND_IF=25` sur macOS) qui évite de sécher 8 s de download sur une iface sans route.
4. **TP-Link M8550** (si Keychain renseigné) : récupère via `tplinkrouterc6u` les métriques radio + data consommée. Précédé d'un **pre-check TCP 2 s** sur le port 80 du routeur — sans ça, la lib timeout à 30 s par défaut quand le M8550 est hors subnet.
5. **Speedtest Cloudflare** sur la route par défaut, toutes les 5 min, **uniquement** si la route n'est pas mobile et qu'on n'est pas en mode économique 5G.
6. **Score qualité** par interface, agrégé en un health 0-1 (cf. plus bas).
7. **Refresh icône** + menu via `_call_on_main()` (rumps + Cocoa main thread).

### Mode économique 5G

Quand le M8550 dépasse `TPLINK_BUDGET_MB_PER_HOUR` (défaut 100 Mo/h) :

- Plus de speedtest auto
- Plus de medium probe sur les interfaces mobiles

Les pings cheap restent (négligeables). Évite de cramer un forfait pendant les tests.

### Score qualité par interface

```
score = 0.40 × latence + 0.30 × perte + 0.30 × débit (+ bonus signal Wi-Fi ≤ 5%)
```

- **Latence** : 1.0 si ≤ 30 ms, 0.0 si ≥ 250 ms, linéaire au milieu.
- **Perte** : `1 - loss/20`, 0 % → 1.0, 20 %+ → 0.0.
- **Débit** : `min(1.0, mbps / 50)`. Source : EWMA medium probe en priorité (par iface), sinon speedtest Cloudflare (route active uniquement), sinon neutre 0.5.
- **Bonus Wi-Fi** : +0.025 si RSSI ≥ -55 dBm, +0.025 si SNR ≥ 30 dB.

**Court-circuits durs à 0** : si le pre-check TCP a marqué l'iface avec un de ces motifs, le score passe direct à 0 sans calcul :

- `pas de route`
- `réseau injoignable`
- `host down`
- `iface sans IP`
- `iface inutilisable`

C'est ce qui produit le `✕ · 0%` rapide quand un dock est branché à un switch sans uplink.

### Palette daltonien-safe

Inspirée viridis : bleu profond (40, 40, 120) → turquoise (60, 170, 170) → jaune doré (240, 210, 80). Zéro rouge ni vert. Voir `_interpolate_color()` dans `network_health.py`.

---

## Pré-requis

| Élément | Précision |
|---|---|
| macOS | 12 Monterey minimum (CoreWLAN + PyObjC récents). |
| Compte Apple Developer | Pour la signature Developer ID (99 €/an). Un compte gratuit signe seulement avec une cert « Apple Development » utilisable localement. |
| Outils Xcode | `xcode-select --install` (fournit `codesign`, `security`, `notarytool`, `stapler`, `iconutil`). |
| Python | 3.11 ou 3.12. 3.10 minimum. |

---

## Build & installation

### Cert Developer ID (une seule fois)

Xcode ne crée pas cette cert automatiquement.

1. **Xcode → Settings → Accounts** → ton Apple ID.
2. **Manage Certificates…** → `+` → **Developer ID Application**.

Vérifier :

```bash
security find-identity -v -p codesigning
```

Noter la chaîne complète :

```
Developer ID Application: Nom Organisation (ABCDE12345)
```

C'est la valeur de la variable `DEVELOPER_ID` ci-dessous.

### Préparer l'environnement

```bash
cd path/to/nethealth     # repo root
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install py2app
```

Dépendances installées :

- `rumps` — UI menu bar
- `Pillow` — génération dynamique des icônes PNG
- `pyobjc-framework-CoreWLAN` — API Wi-Fi native (SSID, RSSI, SNR, canal…)
- `pyobjc-framework-CoreLocation` — déclenche la permission Location Services
- `tplinkrouterc6u` — client M8550 (lecture LTE/5G + data)
- `speedtest-cli` — fallback (le module principal est vendorisé via `speedtest_vendor.py`)
- `py2app` — bundling en `.app`

### Build signé (recommandé)

```bash
DEVELOPER_ID="Developer ID Application: Nom Organisation (ABCDE12345)" \
INSTALL=1 \
./build.sh
```

Le script :

1. Supprime les builds précédents (`build/`, `dist/`, `__pycache__/`)
2. Génère l'icône Finder via `python3 generate_app_icon.py` si `icon.icns` n'existe pas
3. Construit `dist/NetHealth.app` via py2app
4. Signe le bundle avec l'identité fournie + entitlements de `entitlements.plist`
5. Copie `NetHealth.app` dans `/Applications/`

Durée typique : 30-90 s.

### Build signé + notarisé (distribution externe)

Pré-requis : un **app-specific password** créé sur [appleid.apple.com](https://appleid.apple.com) → Sécurité.

```bash
DEVELOPER_ID="Developer ID Application: Nom Organisation (ABCDE12345)" \
APPLE_ID="email@example.com" \
TEAM_ID="ABCDE12345" \
APP_SPECIFIC_PASSWORD="xxxx-xxxx-xxxx-xxxx" \
NOTARIZE=1 INSTALL=1 \
./build.sh
```

Compter 1-5 min de plus pour la notarisation (envoi à Apple, attente, staple du ticket).

### Build non signé (test local uniquement)

```bash
./build.sh
cp -R dist/NetHealth.app /Applications/
```

Gatekeeper refusera au 1er lancement → clic-droit sur l'icône → **Ouvrir**.

---

## Premier lancement

1. Ouvrir `/Applications/NetHealth.app` (double-clic Finder).
2. **Accepter la popup Location Services** quand elle apparaît. Sans cette permission, macOS retourne `<redacted>` pour le SSID et NetHealth ne peut pas distinguer les Wi-Fi entre eux.
3. L'icône (cinq barres) apparaît dans la menu bar.

### Configurer le M8550 (optionnel)

Menu **Configurer mot de passe routeur…** → saisir le mot de passe admin du M8550. Stocké dans le Keychain macOS sous `eu.mylastnight.nethealth.tplink`. NetHealth essaie successivement les usernames `user` puis `admin`.

### Si la popup Location n'apparaît pas

Le plus courant : macOS a mémorisé un refus précédent.

```bash
tccutil reset LocationServices eu.mylastnight.nethealth
open /Applications/NetHealth.app
```

Ou manuellement : **Réglages système → Confidentialité & sécurité → Services de localisation** → activer **NetHealth**.

Si la permission est refusée au runtime, le menu affiche en plus l'entrée **⚠️ Réglages Location (perm refusée)…** qui ouvre directement le panneau macOS correspondant.

### Démarrage automatique

**Réglages système → Général → Éléments d'ouverture** → `+` → `/Applications/NetHealth.app`.

L'app vit uniquement dans la menu bar (`LSUIElement = True`) — pas de Dock, pas de fenêtre.

---

## Configuration

### Style d'icône menu bar

`network_health.py` ligne ~207 :

```python
ICON_STYLE = "gauge"   # ou "radar" ou "pie"
```

- **`gauge`** (défaut) : 5 barres verticales, hauteur = score, triangle blanc sur l'iface active.
- **`radar`** : cercle + rayons partant du centre, longueur = score, disque blanc sur l'iface active.
- **`pie`** : pie-chart historique, 1 quartier par iface.

Modifier la valeur, rebuild, l'icône change.

### Seuils du score

`network_health.py` lignes ~196-199 :

```python
LATENCY_GREAT_MS = 30       # ≤ 30 ms → score latence = 1.0
LATENCY_BAD_MS = 250        # ≥ 250 ms → score latence = 0.0
LOSS_BAD_PCT = 20           # 20 % → score perte = 0.0
DOWNLOAD_GREAT_MBPS = 50    # ≥ 50 Mbps → score débit = 1.0
```

### Budget 5G

`network_health.py` ligne ~181 :

```python
TPLINK_BUDGET_MB_PER_HOUR = 100   # au-delà → mode économique
```

### Régénérer l'icône Finder

L'icône `.icns` est produite à partir de `generate_app_icon.py`. Si tu modifies les paramètres (couleurs, padding, taille du triangle), relance :

```bash
python3 generate_app_icon.py    # régénère icon.icns + icon_1024.png + icon.iconset/
```

Puis rebuild.

---

## Mise à jour

Tant que le **bundle identifier** ne change pas (`eu.mylastnight.nethealth`), macOS conserve les permissions accordées (Location, etc.).

```bash
cd path/to/nethealth     # repo root
source .venv/bin/activate

osascript -e 'tell application "NetHealth" to quit' 2>/dev/null

DEVELOPER_ID="Developer ID Application: Nom Organisation (ABCDE12345)" \
INSTALL=1 \
./build.sh

open /Applications/NetHealth.app
```

Bumper `VERSION` dans `network_health.py` ET `APP_VERSION` dans `setup.py` à chaque build (utile pour distinguer dans les logs `>>> NetHealth v1.22 STARTING <<<`).

---

## Désinstallation

```bash
osascript -e 'tell application "NetHealth" to quit' 2>/dev/null
rm -rf /Applications/NetHealth.app
rm -rf ~/Library/Logs/NetHealth
tccutil reset LocationServices eu.mylastnight.nethealth
security delete-generic-password -s eu.mylastnight.nethealth.tplink 2>/dev/null
```

Retirer aussi de **Réglages système → Général → Éléments d'ouverture** si l'auto-start était activé.

---

## Emplacements utiles

| Chemin | Contenu |
|---|---|
| `/Applications/NetHealth.app` | Bundle installé. |
| `~/Library/Logs/NetHealth/nethealth.log` | Log principal (rotatif, 1 Mo, 3 archives). |
| `~/Library/Logs/NetHealth/debug.log` | Log debug brut. |
| `<projet>/dist/NetHealth.app` | Sortie py2app avant install. |
| `<projet>/icon.icns` | Icône Finder. |
| `<projet>/icon_1024.png` | Master 1024 généré pour preview. |
| `<projet>/build/` | Artefacts intermédiaires py2app. |

Keychain : `security find-generic-password -s eu.mylastnight.nethealth.tplink`.

---

## Dépannage

### SSID `<redacted>` dans le diagnostic

Permission Location Services non accordée. Voir [Premier lancement](#premier-lancement).

### Tous les `medium probe` échouent en `curl rc=28:`

Tu tournes une version < 1.16. Rebuild en 1.22 — le pre-check TCP socket-bound + le mapping rc → label lisible (`timeout`, `pas de route`, `DNS KO`, etc.) sont absents avant.

### TP-Link `ConnectTimeout` 30 s à chaque tick

Idem : il manque le pre-check TCP routeur (1.19+). Le M8550 hors subnet bloquait toute la boucle. Rebuild résout.

### L'app est installée mais l'icône reste à l'ancienne version

Le `.app` a été remplacé pendant qu'une instance tournait. Tuer + recopier proprement :

```bash
osascript -e 'tell application "NetHealth" to quit'
rm -rf /Applications/NetHealth.app
cp -R dist/NetHealth.app /Applications/
open /Applications/NetHealth.app
```

### Speedtest auto skippé en permanence

Vérifier dans les logs `~/Library/Logs/NetHealth/nethealth.log` : si tu vois `speedtest auto skipped (mobile=True…)`, c'est normal — la route active est mobile, NetHealth s'appuie sur la sonde medium par iface (50× moins de data consommée). Pour forcer un test : menu **Lancer speedtest maintenant**.

### Gatekeeper refuse l'ouverture malgré la signature

```bash
codesign --verify --strict --verbose=2 /Applications/NetHealth.app
spctl --assess --type execute --verbose /Applications/NetHealth.app
```

Si signé Apple Development (et non Developer ID), Gatekeeper refuse hors machine de dev. Créer une cert Developer ID Application (cf. [Build](#build--installation)) et re-signer.

### `./build.sh` plante sur la signature

Re-vérifier que `DEVELOPER_ID` correspond exactement à la sortie de `security find-identity -v -p codesigning` (espaces et parenthèses inclus, et toute la chaîne entre guillemets dans la commande shell).

### py2app rate l'embarquement d'un module

Ajouter dans `setup.py` clé `includes` ou `packages`. Pour les modules critiques (speedtest), préférer la vendorisation : copier le `.py` directement dans le projet (cf. `speedtest_vendor.py`).

### Le menu est gris sombre peu lisible

Bug rumps connu : items sans `callback` rendus désactivés. Résolu depuis longtemps en passant un callback no-op aux items info — vérifier que tu n'es pas sur une vieille version.

---

## Checklist de déploiement initial

- [ ] Cert Developer ID Application créée et listée par `security find-identity -v -p codesigning`
- [ ] `xcode-select --install` exécuté
- [ ] Repo cloné, `venv` créé et activé
- [ ] `pip install -r requirements.txt && pip install py2app`
- [ ] `python3 generate_app_icon.py` (génère `icon.icns`)
- [ ] `DEVELOPER_ID=… INSTALL=1 ./build.sh` exécuté sans erreur
- [ ] Bundle présent dans `/Applications/NetHealth.app`
- [ ] Premier lancement effectué, permission Location accordée
- [ ] Mot de passe M8550 enregistré via le menu (optionnel)
- [ ] Icône cinq barres visible dans la menu bar
- [ ] Application ajoutée aux Éléments d'ouverture si démarrage auto souhaité
