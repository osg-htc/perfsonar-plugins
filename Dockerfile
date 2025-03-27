FROM ghcr.io/marian-babik/cmk-etf:2.3.0p29
LABEL maintainer="Marian Babik <Marian.Babik@cern.ch>"
LABEL description="ETF perfSONAR"
LABEL version="0.1"

ARG monitoring_plugins="nagios-plugins"

ENV NSTREAM_ENABLED=1

# Additional ETF config
RUN yum -y update
RUN yum -y install ${monitoring_plugins} future
RUN ln -s /omd/sites/${CHECK_MK_SITE}/etc/check_mk/ncgx.cfg /etc/ncgx/ncgx.cfg
RUN mkdir -p /etc/ncgx/conf.d/ && ln -s /omd/sites/${CHECK_MK_SITE}/etc/check_mk/alerts.cfg /etc/ncgx/conf.d/alerts.cfg

# perfsonar tools
RUN curl -s https://raw.githubusercontent.com/perfsonar/project/master/install-perfsonar | sh -s - tools

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
RUN chmod 755 /usr/lib64/nagios/plugins/check*
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
