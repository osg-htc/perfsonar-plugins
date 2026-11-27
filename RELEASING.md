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

psetf runs three containers with podman-compose from `/root/docker-compose.yml`:

- `etf_ps`, the perfsonar-plugins image;
- `logstash_meta` and `logstash_status`, the n-stream image.

Nothing updates them automatically. To deploy a release:

1. **Back up and pin.** Copy `/root/docker-compose.yml` aside. Then set the new `X.Y.Z` in the `image:` line(s), keeping the old line as a comment. Also update `CHECK_MK_VERSION` in the `etf_ps` environment when CheckMK changes; the entrypoint only prints it, but a stale value is misleading.
2. **Tag the running images for rollback.** For example: `podman tag ghcr.io/osg-htc/perfsonar-plugins:<old> localhost/perfsonar-plugins:rollback-<date>`.
3. **Pull:** `podman-compose -f /root/docker-compose.yml pull <services>`.
4. **Recreate.**
   - `etf_ps` alone:
     ```
     podman-compose -f /root/docker-compose.yml up -d --no-deps etf_ps
     ```
   - `logstash_meta` or `logstash_status`: `etf_ps` is declared as depending on them, and podman refuses to remove a container that another container depends on. `up -d` then fails with "has dependent containers" and simply restarts the old containers. So remove `etf_ps` first and bring all three up together:
     ```
     podman stop -t 60 etf_ps logstash_meta logstash_status
     podman rm etf_ps && podman rm logstash_meta logstash_status
     podman-compose -f /root/docker-compose.yml up -d --no-deps logstash_meta logstash_status etf_ps
     ```
   - Leave caddy alone with `--no-deps`.
   - The old containers usually ignore SIGTERM, so each stop waits out its 60 s timeout before podman kills it.
5. **Check:**
   - `podman exec etf_ps omd status etf` says everything is running;
   - `omd version etf` shows the expected CheckMK version;
   - the login at https://psetf.aglt2.org/etf/ redirects to CILogon;
   - the host count is unchanged: `printf 'GET hosts\nStats: state >= 0\n' | lq`, run as the `etf` user;
   - `podman logs logstash_status` shows "messages sent to logstash" on its 5-minute runs.
6. **To roll back,** restore the saved compose file, or point the `image:` lines at the `rollback-<date>` tags, and repeat step 4.

**Expect a pause in checks after a recreate.** A new container starts a fresh CheckMK site with no Nagios retention, that is, no saved check schedule. Nagios therefore spreads the first run of every active check over `max_service_check_spread` (30 minutes):

- no new check results for the first ~5 minutes;
- every check has run once after about 30 minutes;
- n-stream has little to send until then.

Recreating a container discards anything changed inside it by hand. Put such changes in a PR and a release first.

These files come from the host, so a recreate keeps them:

| Host | In the container | What |
|---|---|---|
| `/opt/cmk/check_mk/` | `/opt/omd/sites/etf/etc/check_mk` | CheckMK config, `ncgx.cfg` (mesh, registry URLs) and `alerts.cfg` |
| `/opt/cmk/apache/auth.conf` | `.../etc/apache/conf.d/auth.conf` | CILogon/OIDC login. Holds the client secret, so it must never go into this repository. The structure it must keep, and why, is in `config/auth.conf.example`. Edit it in place: it is a single-file bind mount |
| `/usr/local/perfdata/` | `.../var/pnp4nagios/perfdata` | performance graphs |
| `/var/spool/nstream/outgoing` | same | results handed to n-stream |
| `/opt/caddy/etc/Caddyfile` | caddy container | TLS for psetf.aglt2.org. It redirects the other host names to that name, and drops any client-sent `X-Remote-User` (`header_up -X-Remote-User`) |

## psetf on CheckMK 2.5 (phase 2)

The same release also carries psetf for CheckMK 2.5. It has three parts.

- **`psetf_perfsonar-X.Y.Z.mkp`:** a CheckMK extension package, attached to the GitHub release by the MKP workflow. It contains `cmk_addons/plugins/perfsonar`: the special agent, the checks, the pScheduler diags active check and the analytics exporter. To build one locally, run `packaging/build-mkp.sh X.Y.Z`; it needs podman or docker.
- **`psetf/`:** discovery and the site rules. Both run on the container host and talk to the site's REST API: `python3 -m psetf.discovery`, `python3 -m psetf.site_setup`.
- **`deploy/install-site.sh`:** installs an MKP into a site running in a container, and sets the site-level pieces an MKP cannot carry:
  - the exporter cron file;
  - `etc/psetf/exporter.json`, mode `file` unless already configured;
  - Nagios `service_check_timeout` of 300 s, for the 240 s diags check;
  - reloads of Apache and the core.

  It is idempotent: the same version is left alone, and an older enabled version is disabled.

To install or upgrade on a site:

```
deploy/install-site.sh CONTAINER SITE psetf_perfsonar-X.Y.Z.mkp
python3 -m psetf.site_setup --config CONFIG        # rules: created, or updated if changed
python3 -m psetf.discovery  --config CONFIG        # hosts and labels
```

**The exporter sends to the analytics collector only in mode `send`.** Only the production psetf uses it; switch at the same moment n-stream stops, or the analytics get every document twice.
