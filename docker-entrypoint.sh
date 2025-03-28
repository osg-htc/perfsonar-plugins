#!/bin/bash
set -e

_term() {
  if [[ -f /var/run/crond.pid ]]; then
    kill -9 `cat /var/run/crond.pid`
    rm -f /var/run/crond.pid
  fi
  rm -rf /opt/omd/sites/etf/etc/nagios/conf.d/wlcg/
  omd stop
  /usr/sbin/httpd -k stop
}

trap _term SIGINT SIGTERM

cat << "EOF"
 _____ _____ _____   ____  ____
| ____|_   _|  ___| |  _ \/ ___|
|  _|   | | | |_    | |_) \___ \
| |___  | | |  _|   |  __/ ___) |
|_____| |_| |_|     |_|   |____/
=================================
EOF
ncgx_version=`rpm -q --qf "%{VERSION}-%{RELEASE}" ncgx`
echo "ETF version: ${ncgx_version} Copyright CERN 2016"
echo "License: https://gitlab.cern.ch/etf/ncgx/blob/master/LICENSE"
echo "Check_MK version: $CHECK_MK_VERSION"
echo "Copyright by Mathias Kettner (https://mathias-kettner.de/check_mk.html)"
plugins=`rpm -qa | grep nagios-plugins`
echo "Plugins:" 
echo "${plugins}"
echo ""
echo "Starting xinetd ..."
export XINETD_LANG="en_US" && /opt/omd/versions/default/bin/xinetd -stayalive -pidfile /var/run/xinetd.pid
if [[ -n $CHECK_MK_USER_ID ]] ; then
   if id "saslauth" >/dev/null 2>&1; then
      echo /usr/sbin/usermod -u 1250 saslauth
   echo "Changing $CHECK_MK_SITE uid to $CHECK_MK_USER_ID"
   /usr/sbin/usermod -u $CHECK_MK_USER_ID $CHECK_MK_SITE
   chown -R $CHECK_MK_SITE /etc/ncgx /var/cache/ncgx /var/cache/nap
fi
if [[ -n $CHECK_MK_GROUP_ID ]] ; then
   echo "Creating group with gid $CHECK_MK_GROUP_ID"
   /usr/sbin/groupadd -g $CHECK_MK_GROUP_ID sec
   /usr/sbin/groupmems -g sec -a $CHECK_MK_SITE
fi

echo "Initialising ..."
/usr/bin/omd stop

if [[ -d /opt/omd/sites/etf/etc/nagios/conf.d/wlcg/ ]]; then
    rm -rf /opt/omd/sites/etf/etc/nagios/conf.d/wlcg/
fi

cp /etc/ncgx/templates/generic/handlers.cfg /opt/omd/sites/etf/etc/nagios/conf.d/
omd start
rm -f /opt/omd/sites/etf/etc/nagios/conf.d/handlers.cfg 
su etf -c "ncgx"
#su - etf -c "cmk -II; cmk -O"
su - etf -c "cmk -O"
if [ "${NSTREAM_ENABLED}" -eq "1" ] ; then
    echo "Nagios stream enabled ..."
else
    echo "Nagios stream disabled ..."
    /usr/bin/disable_nstream
fi
echo "Starting crond ..."
sed -i -n '/pam_loginuid/!p' /etc/pam.d/crond
/usr/sbin/crond -m off -p -n
