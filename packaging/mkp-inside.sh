#!/bin/bash
# Runs inside the CheckMK image (see build-mkp.sh): a throwaway site packages the plugin.
set -euo pipefail
VERSION=$1
SITE=mkpbuild
omd create --no-tmpfs "$SITE" >/dev/null
PLUGINS=/omd/sites/$SITE/local/lib/python3/cmk_addons/plugins
mkdir -p "$PLUGINS"
cp -r /src/cmk_addons/plugins/perfsonar "$PLUGINS/"
find "$PLUGINS" -name __pycache__ -prune -exec rm -rf {} +
chown -R "$SITE:$SITE" "/omd/sites/$SITE/local"
# refuse to package plugins CheckMK cannot load (argument names, rulesets, server-side calls);
# the validator asks the site's automation service, so the site must be running
omd start "$SITE" >/dev/null
su - "$SITE" -c "cmk-validate-plugins"
omd stop "$SITE" >/dev/null
su - "$SITE" -c "mkp template psetf_perfsonar > /tmp/manifest.raw"
VERSION="$VERSION" python3 - <<'PY'
import ast, os
m = ast.literal_eval(open("/tmp/manifest.raw").read())
m.update({
    "version": os.environ["VERSION"],
    "title": "psetf: perfSONAR toolkit monitoring",
    "author": "OSG networking (osg-htc/perfsonar-plugins)",
    "description": "Special agent, checks, pScheduler diagnostics and the analytics exporter for psetf, "
                   "the monitoring of the WLCG/OSG perfSONAR deployment. Discovery and site rules are "
                   "in the repository's psetf/ package; see RELEASING.md.",
    "download_url": "https://github.com/osg-htc/perfsonar-plugins/releases",
    "version.min_required": "2.5.0",
})
open("/tmp/manifest", "w").write(repr(m))
PY
chmod 644 /tmp/manifest
su - "$SITE" -c "mkp package /tmp/manifest"
install -m 644 "/omd/sites/$SITE/var/check_mk/packages_local/psetf_perfsonar-$VERSION.mkp" /dist/
