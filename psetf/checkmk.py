"""A small client for the parts of the CheckMK REST API that psetf uses (CheckMK 2.5, api/1.0)."""
import time

import requests


class CheckMKError(Exception):
    pass


class CheckMK(object):
    def __init__(self, url, user, secret, verify=True, timeout=120):
        """url: the site's API base, e.g. http://127.0.0.1:5010/psetf/check_mk/api/1.0"""
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer %s %s" % (user, secret),
                                     "Accept": "application/json"})
        self.session.verify = verify
        self.timeout = timeout

    def _call(self, method, path, expect=(200,), **kw):
        r = self.session.request(method, self.url + path, timeout=self.timeout, **kw)
        if r.status_code not in expect:
            try:
                body = r.json()
                detail = "%s: %s %s" % (body.get("title"), body.get("detail"), body.get("fields") or "")
            except ValueError:
                detail = r.text[:300]
            raise CheckMKError("%s %s -> %s %s" % (method, path, r.status_code, detail))
        return r

    # folders and hosts
    def ensure_folder(self, name, title):
        r = self._call("GET", "/domain-types/folder_config/collections/all", params={"parent": "/"})
        if any(f.get("id") == name or (f.get("extensions") or {}).get("path") == "/" + name
               for f in r.json().get("value", [])):
            return False
        self._call("POST", "/domain-types/folder_config/collections/all",
                   json={"name": name, "title": title, "parent": "/"})
        return True

    def hosts(self, folder):
        """{host name: attributes} of the hosts directly in `folder` (e.g. "/perfsonar")."""
        r = self._call("GET", "/domain-types/host_config/collections/all",
                       params={"effective_attributes": "false"})
        out = {}
        for h in r.json().get("value", []):
            ext = h.get("extensions") or {}
            if ext.get("folder") == folder:
                out[h["id"]] = ext.get("attributes") or {}
        return out

    def create_hosts(self, folder, hosts):
        """hosts: {name: attributes}"""
        if hosts:
            self._call("POST", "/domain-types/host_config/actions/bulk-create/invoke",
                       json={"entries": [{"host_name": n, "folder": folder, "attributes": a}
                                         for n, a in sorted(hosts.items())]})

    def update_hosts(self, hosts):
        """hosts: {name: attributes to set}"""
        if hosts:
            self._call("PUT", "/domain-types/host_config/actions/bulk-update/invoke",
                       json={"entries": [{"host_name": n, "update_attributes": a}
                                         for n, a in sorted(hosts.items())]})

    def delete_hosts(self, names):
        if names:
            self._call("POST", "/domain-types/host_config/actions/bulk-delete/invoke",
                       json={"entries": sorted(names)}, expect=(200, 204))

    # rules
    def rules(self, ruleset):
        r = self._call("GET", "/domain-types/rule/collections/all", params={"ruleset_name": ruleset})
        return r.json().get("value", [])

    def create_rule(self, ruleset, folder, value_raw, description, conditions=None):
        self._call("POST", "/domain-types/rule/collections/all",
                   json={"ruleset": ruleset, "folder": folder, "value_raw": value_raw,
                         "properties": {"description": description, "disabled": False},
                         "conditions": conditions or {}})

    def update_rule(self, rule_id, value_raw):
        r = self._call("GET", "/objects/rule/%s" % rule_id)
        ext = r.json().get("extensions") or {}
        self._call("PUT", "/objects/rule/%s" % rule_id, headers={"If-Match": r.headers.get("ETag", "*")},
                   json={"value_raw": value_raw, "properties": ext.get("properties") or {},
                         "conditions": ext.get("conditions") or {}})

    # service discovery
    def discover_services(self, hosts, wait=1800):
        """Bulk service discovery (add new, remove vanished, refresh labels) and wait for it."""
        if not hosts:
            return None
        # answers 303 See Other with the background job in Location
        r = self._call("POST", "/domain-types/discovery_run/actions/bulk-discovery-start/invoke",
                       expect=(200, 303), allow_redirects=False,
                       json={"hostnames": sorted(hosts), "do_full_scan": True, "bulk_size": 3, "ignore_errors": True,
                             "options": {"monitor_undecided_services": True, "remove_vanished_services": True,
                                         "update_service_labels": True, "update_service_parameters": True,
                                         "update_host_labels": True}})
        job = r.headers.get("Location", "").split("/objects/background_job/")[-1] or r.json().get("id")
        deadline = time.time() + wait
        while time.time() < deadline:
            s = self._call("GET", "/objects/background_job/%s" % job).json()
            # "active" is already false while a job waits for its lock: go by the job's state
            state = ((s.get("extensions") or {}).get("status") or {}).get("state")
            if state in ("finished", "stopped", "exception"):
                return s
            time.sleep(10)
        raise CheckMKError("bulk service discovery still running after %ss" % wait)

    # changes
    def pending_changes(self):
        r = self._call("GET", "/domain-types/activation_run/collections/pending_changes")
        return r.headers.get("ETag"), r.json().get("value", [])

    def activate(self, site, wait=600):
        etag, pending = self.pending_changes()
        if not pending:
            return None
        r = self._call("POST", "/domain-types/activation_run/actions/activate-changes/invoke",
                       headers={"If-Match": etag or "*"}, expect=(200, 302),
                       json={"redirect": False, "sites": [site], "force_foreign_changes": True},
                       allow_redirects=False)
        run_id = r.json().get("id")
        deadline = time.time() + wait
        while run_id and time.time() < deadline:
            s = self._call("GET", "/objects/activation_run/%s" % run_id).json()
            if not (s.get("extensions") or {}).get("is_running"):
                return s
            time.sleep(3)
        raise CheckMKError("activation %s still running after %ss" % (run_id, wait))
