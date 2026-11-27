
PKGNAME=nagios-plugins-perfsonar
SPECFILE=${PKGNAME}.spec
FILES=Makefile README.md ${SPECFILE} 
SRC=src/check_ps src/check_ps_psched
ETC=etc

PKGVERSION=$(shell grep -s '^Version:' $(SPECFILE) | sed -e 's/Version: *//')

install:
	install -D -m 755 src/check_ps ${DESTDIR}/usr/lib64/nagios/plugins/check_ps
	install -D -m 755 src/check_ps_psched ${DESTDIR}/usr/lib64/nagios/plugins/check_ps_psched

srpm: dist
	rpmbuild -ts ${PKGNAME}-${PKGVERSION}.tar.gz

rpm: dist
	rpmbuild -ta ${PKGNAME}-${PKGVERSION}.tar.gz

dist: ${FILES} ${SRC} 
	rm -rf dist
	mkdir -p dist/${PKGNAME}-${PKGVERSION}/src
	cp -pr ${FILES} dist/${PKGNAME}-${PKGVERSION}/.
	cp -pr ${SRC} dist/${PKGNAME}-${PKGVERSION}/src/
	find dist -type d -name .svn | xargs -i rm -rf {}
	find dist -type d -name .git | xargs -i rm -rf {}
	cd dist ; tar cfz ../${PKGNAME}-${PKGVERSION}.tar.gz ${PKGNAME}-${PKGVERSION}
	rm -rf dist

sources: dist

clean:
	rm -rf ${PKGNAME}-${PKGVERSION}.tar.gz
	rm -rf dist

