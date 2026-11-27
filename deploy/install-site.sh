#!/bin/bash
# Install a psetf_perfsonar MKP into a CheckMK site running in a container, with the site-level
# pieces an MKP cannot carry. Run on the container host as root:
#
#   deploy/install-site.sh CONTAINER SITE psetf_perfsonar-X.Y.Z.mkp
#
# Then, from the repository's psetf/ package on the host (see RELEASING.md):
#   python3 -m psetf.site_setup --config CONFIG     # rules
#   python3 -m psetf.discovery  --config CONFIG     # hosts
set -euo pipefail
CONTAINER=${1:?usage: install-site.sh CONTAINER SITE MKP}
SITE=${2:?usage: install-site.sh CONTAINER SITE MKP}
MKP=${3:?usage: install-site.sh CONTAINER SITE MKP}
podman cp "$MKP" "$CONTAINER:/tmp/psetf_perfsonar.mkp"
podman exec -i "$CONTAINER" su - "$SITE" <<'SITE_SH'
set -euo pipefail
PLUGIN=local/lib/python3/cmk_addons/plugins/perfsonar
# Files copied in by hand before packaging would block the package: remove them, but only
# when no package owns them.
if [ -d "$PLUGIN" ] && mkp find 2>/dev/null | grep -E "^perfsonar/" | awk '{print $2}' | grep -q "Additional"; then
    echo "removing unpackaged plugin files in $PLUGIN"
    rm -rf "$PLUGIN"
fi
VERSION=$(mkp inspect /tmp/psetf_perfsonar.mkp | awk '/^Version:/{print $2; exit}')
if mkp list 2>/dev/null | awk '$1 == "psetf_perfsonar" {print $2}' | grep -qx "$VERSION"; then
    echo "psetf_perfsonar $VERSION is already installed"
else
    mkp add /tmp/psetf_perfsonar.mkp
fi
# disable any other enabled version first, then enable this one
for old in $(mkp list 2>/dev/null | awk '$1 == "psetf_perfsonar" && $2 != v && /Enabled/ {print $2}' v="$VERSION"); do
    mkp disable psetf_perfsonar "$old"
done
mkp enable psetf_perfsonar "$VERSION" 2>/dev/null || true     # already enabled when freshly added
# the automation helper (service discovery, activation) keeps plugins loaded: restart it, or a
# discovery right after an upgrade still runs the old plugins and finds none of the new services
omd restart automation-helper >/dev/null
mkp list | grep psetf_perfsonar

# exporter: every 5 minutes; mode "file" unless etc/psetf/exporter.json already says otherwise
mkdir -p etc/psetf
[ -f etc/psetf/exporter.json ] || echo '{"mode": "file", "measurement_agent": "psetf.aglt2.org"}' > etc/psetf/exporter.json
cat > etc/cron.d/psetf-exporter <<'CRON'
# psetf exporter: analytics documents (cmk_addons/plugins/perfsonar/lib/exporter.py); mode in etc/psetf/exporter.json
*/5 * * * * python3 -m cmk_addons.plugins.perfsonar.lib.exporter >> $OMD_ROOT/var/log/psetf-exporter.log 2>&1
CRON
omd reload crontab >/dev/null

# the pscheduler diags check stops itself at 240 s; Nagios must allow longer
current=$(sed -n 's/^service_check_timeout=//p' etc/nagios/nagios.d/tuning.cfg)
if [ "${current:-0}" -lt 300 ]; then
    sed -i 's/^service_check_timeout=.*/service_check_timeout=300/' etc/nagios/nagios.d/tuning.cfg
    echo "service_check_timeout ${current} -> 300"
fi
# graphs of the daily pscheduler diags check: the default RRD heartbeat (8460 s) stores every
# daily result as unknown. 36 h fills each day and still shows a missed run as a gap. The
# pnp4nagios setting applies to new graph files; tune the existing ones.
HEARTBEAT=129600
cat > etc/pnp4nagios/check_commands/check_mk_active-perfsonar_diags.cfg <<CFG
# psetf (deploy/install-site.sh): the pscheduler diags check runs once a day
RRD_HEARTBEAT = $HEARTBEAT
CFG
shopt -s nullglob
tuned=0
for f in var/pnp4nagios/perfdata/*/perfSONAR_services__pscheduler_diags_*.rrd; do
    if ! rrdtool info "$f" | grep -qx "ds\[1\].minimal_heartbeat = $HEARTBEAT"; then
        rrdtool tune "$f" --heartbeat "1:$HEARTBEAT" >/dev/null
        tuned=$((tuned + 1))
    fi
done
shopt -u nullglob
[ "$tuned" -eq 0 ] || echo "diags graphs: heartbeat set to $HEARTBEAT s in $tuned files"

omd reload apache >/dev/null      # the web interface loads the plugin's rulesets
cmk -R >/dev/null && echo "core configuration regenerated"
SITE_SH
podman exec "$CONTAINER" rm -f /tmp/psetf_perfsonar.mkp
echo "installed $(basename "$MKP") in $CONTAINER/$SITE"
