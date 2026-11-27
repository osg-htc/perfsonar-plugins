#!/bin/bash
# Build the CheckMK extension package (MKP) psetf_perfsonar from cmk_addons/plugins/perfsonar.
#
#   packaging/build-mkp.sh VERSION [IMAGE]      (from the repository root; needs podman or docker)
#
# Runs the pinned CheckMK image, creates a throwaway site in it, installs the plugin files and
# packages them with CheckMK's own `mkp` tool. Writes dist/psetf_perfsonar-VERSION.mkp.
set -euo pipefail
VERSION=${1:?usage: packaging/build-mkp.sh VERSION [IMAGE]}
IMAGE=${2:-docker.io/checkmk/check-mk-community:2.5.0p21}
ENGINE=$(command -v podman || command -v docker)
mkdir -p dist
"$ENGINE" run --rm --entrypoint bash -v "$PWD:/src:ro" -v "$PWD/dist:/dist" "$IMAGE" \
    /src/packaging/mkp-inside.sh "$VERSION"
ls -l "dist/psetf_perfsonar-$VERSION.mkp"
