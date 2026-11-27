# the cmk-etf release this image is built on (it carries the CheckMK version); see RELEASING.md
ARG CMK_ETF_VERSION=1.0.0
FROM ghcr.io/osg-htc/cmk-etf:${CMK_ETF_VERSION}
LABEL maintainer="Marian Babik <Marian.Babik@cern.ch>"
LABEL description="ETF perfSONAR"

ARG monitoring_plugins="nagios-plugins"

ENV NSTREAM_ENABLED=1

# Additional ETF config
RUN yum -y update
RUN yum -y install ${monitoring_plugins} future
RUN ln -s /omd/sites/${CHECK_MK_SITE}/etc/check_mk/ncgx.cfg /etc/ncgx/ncgx.cfg
RUN mkdir -p /etc/ncgx/conf.d/ && ln -s /omd/sites/${CHECK_MK_SITE}/etc/check_mk/alerts.cfg /etc/ncgx/conf.d/alerts.cfg

# OIDC setup
RUN yum -y install https://github.com/OpenIDC/mod_auth_openidc/releases/download/v2.4.16.10/mod_auth_openidc-2.4.16.10-1.el9.x86_64.rpm 

# perfsonar tools
# installer taken from a fixed commit of perfsonar/project (2026-05-18, "Added --no-upgrade switch")
ARG PERFSONAR_INSTALLER_REF=6252cce3e9a3e1a101ee61b5b9902302dbd8e6dd
RUN curl -fsS -o /tmp/install-perfsonar https://raw.githubusercontent.com/perfsonar/project/${PERFSONAR_INSTALLER_REF}/install-perfsonar \
    && sh /tmp/install-perfsonar tools && rm -f /tmp/install-perfsonar

# python path fix
COPY ./config/sitecustomize.py /omd/sites/etf/lib/python3.12/

# pscheduler troubleshoot fix (to use system py3.9 not cmk's 3.12)
RUN sed '1 s|^.*$|#!/usr/bin/env python3.9|' -i /usr/libexec/pscheduler/commands/troubleshoot

# Install
COPY ./src/check_ps /usr/lib64/nagios/plugins/
COPY ./src/check_ps_es /usr/lib64/nagios/plugins/
COPY ./src/check_es /usr/lib64/nagios/plugins/
COPY ./src/check_ps_psched /usr/lib64/nagios/plugins/
COPY ./src/check_rsv /usr/lib64/nagios/plugins/
COPY ./src/check_ps_report /usr/lib64/nagios/plugins/
COPY ./src/pscheduler_check /usr/local/bin/
RUN chmod 755 /usr/lib64/nagios/plugins/check* /usr/local/bin/pscheduler_check
COPY ./src/local_checks.cfg /etc/ncgx/conf.d/
COPY ./src/wlcg_ps.cfg /etc/ncgx/metrics.d/
COPY ./src/etf_ps_plugin.py /usr/lib/ncgx/x_plugins/

# Notifications
COPY ./config/service_template.tpl /etc/ncgx/templates/service_template.tpl

# Streaming
RUN mkdir -p /var/spool/nstream/outgoing && chmod 777 /var/spool/nstream/outgoing
RUN mkdir /etc/stompclt
COPY ./config/ocsp_handler.cfg /etc/nstream/

EXPOSE 80 443 6557
COPY ./docker-entrypoint.sh /
ENTRYPOINT /docker-entrypoint.sh
