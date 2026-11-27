# Releasing perfsonar-plugins

The psetf image: the perfSONAR checks and host discovery (ncgx plugin) on top of [cmk-etf](https://github.com/osg-htc/cmk-etf).

## Versions

Releases are numbered `X.Y.Z` and published as `ghcr.io/osg-htc/perfsonar-plugins:X.Y.Z`.

- **Z:** fixes, and rebuilds with updated packages.
- **Y:** new checks or behaviour.
- **X:** changes that need action when deploying, such as a CheckMK minor upgrade, a site migration or new required configuration.

A release image is never rebuilt or overwritten. A fix is a new release.

Other image tags:

- `:main` is built only by running the Docker workflow by hand, for testing. Do not deploy it.
- PR builds are not pushed anywhere.
- There is no `:latest`.

## What is pinned

- **The cmk-etf base image:** `CMK_ETF_VERSION` in the `Dockerfile`. It carries the CheckMK version.
- **perfSONAR tools installer:** `install-perfsonar` is taken from a fixed commit of perfsonar/project (`PERFSONAR_INSTALLER_REF`).
- **mod_auth_openidc:** a fixed RPM URL.
- **Not pinned:** the perfSONAR and OS packages are whatever is current at build time, and an image, once released, keeps them.

Change a pin in a PR, then release.

## Cutting a release

1. Merge the changes to `main`. **Checks** and the **Docker** build run on every PR.
2. Create the release, which also creates the tag:
   ```
   gh release create X.Y.Z --repo osg-htc/perfsonar-plugins --target main --generate-notes
   ```
   You can also use the GitHub web page: Releases, then "Draft a new release", with a new tag `X.Y.Z`.
3. The tag starts the Docker workflow, which builds, pushes and signs `ghcr.io/osg-htc/perfsonar-plugins:X.Y.Z`. Wait for it to finish (`gh run watch`).
4. To move to a new cmk-etf release, set `CMK_ETF_VERSION` to it in a PR, then release this repository.

## Deploying on psetf

psetf runs the images with podman-compose from `/root/docker-compose.yml`, and nothing updates them automatically. To deploy a release:

1. Set the new `X.Y.Z` in the `image:` line(s), keeping the old line as a comment for rollback.
2. Pull and recreate:
   ```
   podman-compose -f /root/docker-compose.yml pull etf_ps
   podman-compose -f /root/docker-compose.yml up -d etf_ps
   ```
3. To roll back, restore the previous tag and run step 2 again.

Recreating a container discards anything changed inside it by hand. Put such changes in a PR and a release first.

These files come from the host, so a recreate keeps them:

| Host | In the container | What |
|---|---|---|
| `/opt/cmk/check_mk/` | `/opt/omd/sites/etf/etc/check_mk` | CheckMK config, `ncgx.cfg` (mesh, registry URLs) and `alerts.cfg` |
| `/opt/cmk/apache/auth.conf` | `.../etc/apache/conf.d/auth.conf` | CILogon/OIDC login. Holds the client secret, so it must never go into this repository |
| `/usr/local/perfdata/` | `.../var/pnp4nagios/perfdata` | performance graphs |
| `/var/spool/nstream/outgoing` | same | results handed to n-stream |
