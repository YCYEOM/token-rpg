#!/bin/zsh
set -euo pipefail

# Xcode 없이 Command Line Tools만으로 .app과 드래그 설치용 DMG를 만든다.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist-macos"
APP="$OUT/Token RPG.app"

rm -rf "$OUT"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -parse-as-library "$ROOT/macos/TokenRPGMenuBar.swift" -framework Cocoa -o "$APP/Contents/MacOS/TokenRPG"
cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"
cp "$ROOT/token_rpg.py" "$APP/Contents/Resources/token_rpg.py"

# Finder에서 드래그 설치할 수 있는 DMG. 서명/공증은 릴리스 CI에서 별도로 한다.
STAGE="$OUT/stage"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Token RPG" -srcfolder "$STAGE" -ov -format UDZO "$OUT/Token-RPG-macOS.dmg"
rm -rf "$STAGE"

echo "앱: $APP"
echo "설치 파일: $OUT/Token-RPG-macOS.dmg"
