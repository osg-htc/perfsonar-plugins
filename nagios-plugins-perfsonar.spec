%if 0%{?rhel} == 7
  %define dist .el7
%endif

Summary: Nagios plugins to test perfSONAR infrastructure
Name: nagios-plugins-perfsonar
Version: 1.0.2
Release: 20%{?dist}
License: GPL
Group: Network
Source0: %{name}-%{version}.tar.gz
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-root
BuildArch: noarch
Requires: python >= 2.6
Requires: python-argparse

%description
Nagios plugins to test perfSONAR infrastructure

%prep
%setup -q

%build

%install
rm -rf $RPM_BUILD_ROOT
make install DESTDIR=${RPM_BUILD_ROOT}

%clean
rm -rf $RPM_BUILD_ROOT


%files
%defattr(-,root,root,-)
%attr(0755,root,root) /usr/lib64/nagios/plugins/check_ps
%attr(0755,root,root) /usr/lib64/nagios/plugins/check_ps_psched

%changelog
* Wed Mar 27 2019 Marian Babik <marian.babik@cern.ch> - 1.0.2-20
- Initial version
