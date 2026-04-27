#!/usr/bin/env bash
#
# build.sh — bundle + signature + install dans /Applications
#
# Usage :
#   ./build.sh                    # build + signature si DEVELOPER_ID défini
#   DEVELOPER_ID="Developer ID Application: Prénom Nom (ABCDE12345)" ./build.sh
#   INSTALL=1 ./build.sh          # copie aussi dans /Applications
#   NOTARIZE=1 ./build.sh         # + notarisation (nécessite APP_SPECIFIC_PASSWORD)
#
# Pour trouver ton identité de signature :
#   security find-identity -v -p codesigning
# Cherche la ligne "Developer ID Application: ...", copie la partie entre
# guillemets (y compris le team ID à la fin).
#
# Si tu ne définis pas DEVELOPER_ID, l'app est tout de même buildée, mais
# non signée (Gatekeeper râlera au 1er lancement → clic-droit → Ouvrir).

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="NetHealth"
APP_PATH="dist/${APP_NAME}.app"

# Active le venv si présent
if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

echo "==> 1/4 Nettoyage des builds précédents"
rm -rf build dist

echo "==> 2/4 py2app build"
python setup.py py2app

if [ ! -d "$APP_PATH" ]; then
    echo "❌ Build a échoué : $APP_PATH introuvable"
    exit 1
fi

if [ -n "${DEVELOPER_ID:-}" ]; then
    echo "==> 3/4 Signature avec '$DEVELOPER_ID'"
    codesign \
        --deep \
        --force \
        --options=runtime \
        --timestamp \
        --entitlements entitlements.plist \
        --sign "$DEVELOPER_ID" \
        "$APP_PATH"

    echo "==> Vérification de la signature"
    codesign --verify --strict --verbose=2 "$APP_PATH"

    # spctl --assess va refuser tant que ce n'est pas notarisé, mais au moins
    # on vérifie que la signature est bien présente et valide.
    spctl --assess --type execute --verbose "$APP_PATH" || true

    if [ "${NOTARIZE:-0}" = "1" ]; then
        if [ -z "${APPLE_ID:-}" ] || [ -z "${TEAM_ID:-}" ] || [ -z "${APP_SPECIFIC_PASSWORD:-}" ]; then
            echo "❌ Notarisation demandée mais il manque APPLE_ID / TEAM_ID / APP_SPECIFIC_PASSWORD"
            exit 1
        fi
        echo "==> Notarisation (peut prendre 1-5 min)"
        ZIP="dist/${APP_NAME}.zip"
        /usr/bin/ditto -c -k --keepParent "$APP_PATH" "$ZIP"
        xcrun notarytool submit "$ZIP" \
            --apple-id "$APPLE_ID" \
            --team-id "$TEAM_ID" \
            --password "$APP_SPECIFIC_PASSWORD" \
            --wait
        xcrun stapler staple "$APP_PATH"
        rm -f "$ZIP"
        echo "==> Notarisation OK et staplée"
    fi
else
    echo "⚠️  DEVELOPER_ID non défini → app NON signée."
    echo "    Pour signer : DEVELOPER_ID='Developer ID Application: ...' ./build.sh"
fi

echo "==> 4/4 Build terminé → $APP_PATH"

if [ "${INSTALL:-0}" = "1" ]; then
    echo "==> Installation dans /Applications"
    rm -rf "/Applications/${APP_NAME}.app"
    cp -R "$APP_PATH" /Applications/
    echo "==> Installée. Lance-la via Finder au moins une fois pour donner la permission Location."
fi

echo "✅ Fait."
