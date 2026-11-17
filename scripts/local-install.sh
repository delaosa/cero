#!/usr/bin/env bash
# Builds and installs the CERO package from the local working tree (no GitHub tag needed).
# Usage: scripts/local-install.sh            -> build and install (sudo installs missing dependencies)
#        scripts/local-install.sh --build    -> build only
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../packaging/aur" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d /tmp/cero-pkg.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# Use the system Python even when a version manager (asdf, pyenv) shadows it
export PATH="/usr/bin:$PATH"

VERSION="$(sed -n 's/^pkgver=//p' "$HERE/PKGBUILD")"
tar -C "$REPO" \
    --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=packaging \
    --exclude='*.egg-info' --exclude=build --exclude=dist \
    --transform "s,^\./,cero-$VERSION/," -czf "$WORK/cero-$VERSION.tar.gz" .

cp "$HERE/cero.sh" "$HERE/cero.desktop" "$WORK/"
# Same PKGBUILD, but the first source is the local tarball and its checksum is skipped
sed 's|"\$pkgname-\$pkgver.tar.gz::[^"]*"|"$pkgname-$pkgver.tar.gz"|' "$HERE/PKGBUILD" > "$WORK/PKGBUILD"
sed -i "0,/^sha256sums=('[^']*'/s//sha256sums=('SKIP'/" "$WORK/PKGBUILD"

cd "$WORK"
if [[ "${1:-}" == "--build" ]]; then
    makepkg -fs
    cp -v ./*.pkg.tar.zst "$HERE/"
else
    makepkg -fsi
fi
