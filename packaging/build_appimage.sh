#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="1.0.1"
APP_NAME="WaylandLiveDictate"
OUTPUT_NAME="${APP_NAME}-${VERSION}-x86_64.AppImage"

echo "[1/4] Build-omgeving voorbereiden..."
if [ ! -d ".venv" ]; then
    uv venv .venv
fi
uv pip install --python .venv/bin/python -e . pyinstaller pillow

echo "[2/4] PyInstaller bundle bouwen..."
rm -rf build dist WaylandDictate.AppDir
.venv/bin/pyinstaller     --noconfirm     --onedir     --windowed     --name wayland-dictate     --collect-all webrtcvad     --add-data "src/wayland_dictate/locales:wayland_dictate/locales"     main.py

echo "[3/4] AppDir structuur en PNG icoon opbouwen..."
mkdir -p WaylandDictate.AppDir/usr/bin WaylandDictate.AppDir/usr/share/icons/hicolor/scalable/apps WaylandDictate.AppDir/usr/share/icons/hicolor/256x256/apps
cp -r dist/wayland-dictate/* WaylandDictate.AppDir/usr/bin/

# Genereer vereiste 256x256 PNG icoon in AppDir root (zodat appimagetool .DirIcon aanmaakt)
.venv/bin/python -c '
from PIL import Image, ImageDraw
img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse([12, 12, 244, 244], fill="#2ecc71")
d.rounded_rectangle([104, 60, 152, 148], radius=24, fill="#ffffff")
d.rectangle([116, 148, 140, 184], fill="#ffffff")
d.rounded_rectangle([88, 184, 168, 204], radius=8, fill="#ffffff")
img.save("WaylandDictate.AppDir/wayland-dictate.png")
img.save("WaylandDictate.AppDir/usr/share/icons/hicolor/256x256/apps/wayland-dictate.png")
'

cat << 'DESKTOP' > WaylandDictate.AppDir/wayland-dictate.desktop
[Desktop Entry]
Type=Application
Name=Wayland Live Dictate
GenericName=Dictation
Comment=Live streaming dictation and translation for Wayland powered by Lemonade AI
Exec=wayland-dictate
Icon=wayland-dictate
Categories=Utility;Accessibility;Qt;
Keywords=dictation;speech;voice;whisper;translation;
Terminal=false
StartupNotify=false
DESKTOP

cat << 'APPRUN' > WaylandDictate.AppDir/AppRun
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
export LD_LIBRARY_PATH="${HERE}/usr/bin:${LD_LIBRARY_PATH:-}"
exec "${HERE}/usr/bin/wayland-dictate" "$@"
APPRUN
chmod +x WaylandDictate.AppDir/AppRun

echo "[4/4] AppImage verpakken met .zsync..."
# Cache buiten build/ houden: 'rm -rf build' hierboven mag appimagetool niet slopen
TOOL_CACHE="$ROOT_DIR/.buildcache"
if ! command -v appimagetool >/dev/null 2>&1; then
mkdir -p "$TOOL_CACHE"
if [ ! -f "$TOOL_CACHE/appimagetool-x86_64.AppImage" ]; then
curl -fL "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" -o "$TOOL_CACHE/appimagetool-x86_64.AppImage"
chmod +x "$TOOL_CACHE/appimagetool-x86_64.AppImage"
fi
APPIMAGETOOL="$TOOL_CACHE/appimagetool-x86_64.AppImage"
else
APPIMAGETOOL="appimagetool"
fi

UPDATE_URL="gh-releases-zsync|woutervanelten|wayland-dictate|latest|WaylandLiveDictate-*-x86_64.AppImage.zsync"

# APPIMAGE_EXTRACT_AND_RUN: nodig in omgevingen zonder /dev/fuse (CI/containers)
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL" -u "$UPDATE_URL" WaylandDictate.AppDir "dist/$OUTPUT_NAME"

echo "Klaar: dist/$OUTPUT_NAME en .zsync zijn gegenereerd!"
