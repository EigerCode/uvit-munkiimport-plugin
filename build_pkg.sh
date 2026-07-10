#!/bin/sh
# build_pkg.sh — builds UVITRepo.plugin and wraps it in a component .pkg.
#
# Usage: ./build_pkg.sh
#
# The .pkg installs UVITRepo.plugin into /usr/local/munki/repoplugins/
# (Swift plugin, used by Munki 7's munkiimport) and UVITRepo.py into
# /usr/local/munki/munkilib/munkirepo/ (Python plugin, used by AutoPkg's
# MunkiImporter via the munkilib compatibility libraries).
# Signing and notarization are handled by the CI release workflow; this
# script is for local/manual builds.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

check_exit_code() {
    if [ "$1" != "0" ]; then
        echo "$2: exit code $1" 1>&2
        exit 1
    fi
}

TOOL="UVITRepo"
VERSION="1.0.0"

THISDIR=$(dirname "$0")
PROJ="${THISDIR}/${TOOL}.xcodeproj"
if [ ! -e "${PROJ}" ] ; then
    check_exit_code 1 "${PROJ} doesn't exist"
fi

# Derive a build revision from the git commit count.
REVISION_BASE=1000
GITREV=$(git log -n1 --format="%H" -- "${THISDIR}")
GITREVINDEX=$(git rev-list --count "$GITREV")
REV=$((GITREVINDEX + REVISION_BASE))
VERSION="${VERSION}.${REV}"

# Clean build directory.
BUILD_DIR="${THISDIR}/build"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

echo "Building ${TOOL}.plugin..."
xcodebuild build \
    -project "${PROJ}" \
    -configuration Release \
    -scheme "${TOOL}" \
    -destination "generic/platform=macOS" \
    -derivedDataPath "${BUILD_DIR}" \
    1>/dev/null

check_exit_code "$?" "xcodebuild failed"

# Assemble package root.
PKG_ROOT="${THISDIR}/payload"
rm -rf "${PKG_ROOT}"
mkdir -p "${PKG_ROOT}/usr/local/munki/repoplugins"
chmod -R 755 "${PKG_ROOT}"

cp "${BUILD_DIR}/Build/Products/Release/${TOOL}.plugin" \
   "${PKG_ROOT}/usr/local/munki/repoplugins/"

mkdir -p "${PKG_ROOT}/usr/local/munki/munkilib/munkirepo"
cp "${THISDIR}/${TOOL}.py" \
   "${PKG_ROOT}/usr/local/munki/munkilib/munkirepo/"

echo "Building pkg for ${TOOL} v${VERSION}..."
pkgbuild \
    --root "${PKG_ROOT}" \
    --identifier "ch.eigercode.uvit.${TOOL}" \
    --version "${VERSION}" \
    --ownership recommended \
    "${THISDIR}/${TOOL}-${VERSION}.pkg"

check_exit_code "$?" "pkgbuild failed"

echo "Done: ${TOOL}-${VERSION}.pkg"
