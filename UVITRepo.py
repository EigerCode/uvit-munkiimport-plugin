# encoding: utf-8
#
# UVITRepo.py
#
# Munki *Python* repo plugin — pushes packages into UVIT via the /api/repo
# HTTP API. This is the twin of UVITRepo.swift (the Munki 7 Swift plugin):
# same endpoints, same headers, same configuration.
#
# Why two plugins: Munki 7's Swift tools (munkiimport etc.) load Swift dylib
# plugins from /usr/local/munki/repoplugins/, while AutoPkg's MunkiImporter
# processor loads Python plugins via munkilib.munkirepo (still shipped by
# Munki 7.x as the Munki 6.7 compatibility component). AutoPkg therefore
# needs this file, installed at /usr/local/munki/munkilib/munkirepo/UVITRepo.py.
#
# HTTP goes through /usr/bin/curl (like Munki's MWA2APIRepo): AutoPkg's and
# Munki's bundled Pythons ship without a usable CA bundle, so urllib fails
# TLS verification; curl uses the system trust store.
#
# Configuration (prefs domain ch.eigercode.uvit.munkiimport, env fallback):
#   uvitToken  / UVIT_TOKEN    — required; opaque API token (uvit_pat_...)
#   uvitTarget / UVIT_TARGET   — required; "global" or "tenant:<id>"
#   uvitRepoID / UVIT_REPO_ID  — required for writes; numeric Software Repo ID
#
# MIT License — Copyright (c) 2026 EigerCode GmbH
# See LICENSE for the full license text.

from __future__ import absolute_import, print_function

import json
import os
import subprocess
import sys
import tempfile
import urllib.parse

from munkilib.munkirepo import Repo, RepoError

PREFS_DOMAIN = "ch.eigercode.uvit.munkiimport"

CURL = "/usr/bin/curl"
CONNECT_TIMEOUT = "30"


def _pref(key):
    """Reads a value from the ch.eigercode.uvit.munkiimport prefs domain.

    Returns None if PyObjC is unavailable (e.g. a stock CI Python) so the
    caller falls through to environment variables."""
    try:
        from Foundation import CFPreferencesCopyAppValue
    except ImportError:
        return None
    value = CFPreferencesCopyAppValue(key, PREFS_DOMAIN)
    if value is None:
        return None
    return str(value)


def _parse_header_dump(path):
    """Returns (status_code, headers_dict) from a curl --dump-header file.

    The dump may contain several blocks (e.g. '100 Continue' before the
    final response); the last status line and its headers win."""
    status = 0
    headers = {}
    with open(path, encoding="utf-8", errors="replace") as fileobj:
        for line in fileobj:
            line = line.strip()
            if line.upper().startswith("HTTP/"):
                try:
                    status = int(line.split()[1])
                except (IndexError, ValueError):
                    status = 0
                headers = {}
            elif ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
    return status, headers


class UVITRepo(Repo):
    """Munki repo plugin backed by the UVIT /api/repo HTTP ingest API."""

    def __init__(self, baseurl):
        self.baseurl = baseurl.rstrip("/")
        self.token = _pref("uvitToken") or os.environ.get("UVIT_TOKEN", "")
        self.target = _pref("uvitTarget") or os.environ.get("UVIT_TARGET", "")
        self.repo_id = _pref("uvitRepoID") or os.environ.get("UVIT_REPO_ID", "")

        if not self.token:
            print(
                "[UVITRepo] WARNING: UVIT_TOKEN not found. Set uvitToken in\n"
                "[UVITRepo]   /Library/Preferences/%s.plist\n"
                "[UVITRepo] or export UVIT_TOKEN=uvit_pat_..." % PREFS_DOMAIN,
                file=sys.stderr,
            )
        if not self.target:
            # The server rejects requests without a target with 400.
            raise RepoError(
                "UVIT_TARGET is not set. Set uvitTarget in "
                "/Library/Preferences/%s.plist or export UVIT_TARGET=global "
                "(or tenant:<id>)." % PREFS_DOMAIN
            )

    # -- HTTP via curl -----------------------------------------------------

    def _run_curl(self, url, method="GET", headers=None, upload_file=None,
                  output_file=None):
        """Performs one HTTP request. Returns (status, headers, body).

        Redirects are NOT followed here: curl forwards custom -H headers to
        redirect targets, which would leak the bearer token (and break S3
        presigned auth). Callers follow redirects explicitly.

        body is None when output_file is given (curl streams to it).
        Request headers go through a temp file (--header @file) so the
        token never appears in the process list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            header_dump = os.path.join(tmpdir, "response_headers")
            header_file = os.path.join(tmpdir, "request_headers")
            body_path = output_file or os.path.join(tmpdir, "body")

            with open(header_file, "w", encoding="utf-8") as fileobj:
                for key, value in (headers or {}).items():
                    fileobj.write("%s: %s\n" % (key, value))

            cmd = [
                CURL, "--silent", "--show-error",
                "--connect-timeout", CONNECT_TIMEOUT,
                "--dump-header", header_dump,
                "--header", "@%s" % header_file,
                "--output", body_path,
            ]
            if upload_file:
                # --upload-file streams the file and implies PUT.
                cmd.extend(["--upload-file", upload_file])
            elif method != "GET":
                cmd.extend(["--request", method])
            cmd.append(url)

            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, check=False)
            except OSError as err:
                raise RepoError("Could not run curl: %s" % err) from err
            if proc.returncode != 0:
                raise RepoError(
                    "Connection error: %s" % proc.stderr.strip())

            status, response_headers = _parse_header_dump(header_dump)
            body = None
            if not output_file:
                with open(body_path, "rb") as fileobj:
                    body = fileobj.read()
            return status, response_headers, body

    def _auth_headers(self, include_repo_id=False, content_type=None,
                      accept=None):
        headers = {"X-UVIT-Target": self.target}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        if include_repo_id and self.repo_id:
            headers["X-UVIT-Repo-ID"] = self.repo_id
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
        return headers

    def _request(self, identifier, method="GET", upload_file=None,
                 output_file=None, include_repo_id=False, accept=None):
        """Issues a request against <baseurl>/<identifier>.

        GETs answered with a redirect (pkgs/* → presigned S3) are followed
        with a second, credential-free request."""
        url = self.baseurl + "/" + urllib.parse.quote(identifier)
        content_type = None
        if upload_file:
            resource_type = identifier.split("/", 1)[0]
            content_type = ("application/xml" if resource_type == "pkgsinfo"
                            else "application/octet-stream")
        headers = self._auth_headers(include_repo_id, content_type, accept)

        status, response_headers, body = self._run_curl(
            url, method, headers, upload_file, output_file)

        if method == "GET" and 300 <= status <= 399:
            location = response_headers.get("location")
            if not location:
                raise RepoError("HTTP %s redirect without Location" % status)
            status, response_headers, body = self._run_curl(
                location, "GET", {}, None, output_file)

        if status == 401:
            raise RepoError(
                "HTTP 401 Unauthorized — check that UVIT_TOKEN "
                "(uvit_pat_…) is set and valid")
        if status == 403:
            raise RepoError(
                "HTTP 403 Forbidden — token owner is not authorized "
                "for the target tenant/repo")
        if not 200 <= status <= 299:
            detail = ""
            if body:
                detail = body[:200].decode("utf-8", errors="replace")
            elif output_file and os.path.exists(output_file):
                with open(output_file, "rb") as fileobj:
                    detail = fileobj.read(200).decode(
                        "utf-8", errors="replace")
            suffix = ": %s" % detail if detail else ""
            raise RepoError("HTTP error %s%s" % (status, suffix))
        return body

    # -- Repo API ----------------------------------------------------------

    def itemlist(self, kind):
        """Returns a list of identifiers for each item of kind.

        Only pkgsinfo has a server-side list endpoint today; other kinds
        (catalogs, manifests, icons, pkgs) return an empty list so that
        makecatalogs and iconimporter degrade gracefully."""
        resource_type = kind.split("/", 1)[0]
        if resource_type != "pkgsinfo":
            return []
        body = self._request("pkgsinfo", accept="application/json")
        # Server response: {"items": [{"path": "Name/1.0.plist"}, ...]}
        decoded = json.loads(body)
        return [item["path"] for item in decoded.get("items", [])]

    def get(self, resource_identifier):
        """Returns the raw bytes of a repo item."""
        return self._request(resource_identifier)

    def get_to_local_file(self, resource_identifier, local_file_path):
        """Streams a repo item to local_file_path."""
        self._request(resource_identifier, output_file=local_file_path)

    def put(self, resource_identifier, content):
        """Uploads in-memory content to the repo. Use for pkginfo blobs."""
        with tempfile.NamedTemporaryFile(delete=False) as fileobj:
            fileobj.write(content)
            temp_path = fileobj.name
        try:
            self._request(
                resource_identifier, upload_file=temp_path,
                include_repo_id=True)
        finally:
            os.unlink(temp_path)

    def put_from_local_file(self, resource_identifier, local_file_path):
        """Streams local_file_path to the repo. Use for installer items."""
        self._request(
            resource_identifier, upload_file=local_file_path,
            include_repo_id=True)

    def delete(self, resource_identifier):
        """Deletes a repo item."""
        self._request(
            resource_identifier, method="DELETE", include_repo_id=True)
