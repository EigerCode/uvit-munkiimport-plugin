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
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

from munkilib.munkirepo import Repo, RepoError

PREFS_DOMAIN = "ch.eigercode.uvit.munkiimport"

DEFAULT_TIMEOUT = 120  # seconds; large uploads stream, so this is per-read


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


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but drops auth headers when leaving the API host.

    GET /pkgs/<path> answers with a 307 to a presigned S3 URL; the bearer
    token and X-UVIT-* headers must not be forwarded there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for header in ("Authorization", "X-uvit-target", "X-uvit-repo-id"):
                new.headers.pop(header, None)
        return new


class UVITRepo(Repo):
    """Munki repo plugin backed by the UVIT /api/repo HTTP ingest API."""

    def __init__(self, baseurl):
        self.baseurl = baseurl.rstrip("/")
        self.token = _pref("uvitToken") or os.environ.get("UVIT_TOKEN", "")
        self.target = _pref("uvitTarget") or os.environ.get("UVIT_TARGET", "")
        self.repo_id = _pref("uvitRepoID") or os.environ.get("UVIT_REPO_ID", "")
        self._opener = urllib.request.build_opener(_AuthStrippingRedirectHandler())

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

    # -- HTTP helpers ------------------------------------------------------

    def _request(self, identifier, method="GET", body=None,
                 include_repo_id=False, accept=None):
        url = self.baseurl + "/" + urllib.parse.quote(identifier)
        request = urllib.request.Request(url, data=body, method=method)
        if self.token:
            request.add_header("Authorization", "Bearer %s" % self.token)
        request.add_header("X-UVIT-Target", self.target)
        if include_repo_id and self.repo_id:
            request.add_header("X-UVIT-Repo-ID", self.repo_id)
        if accept:
            request.add_header("Accept", accept)
        if method == "PUT":
            resource_type = identifier.split("/", 1)[0]
            content_type = ("application/xml" if resource_type == "pkgsinfo"
                            else "application/octet-stream")
            request.add_header("Content-Type", content_type)
        return request

    def _open(self, request):
        """Performs the request, mapping HTTP errors to RepoError."""
        try:
            return self._opener.open(request, timeout=DEFAULT_TIMEOUT)
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise RepoError(
                    "HTTP 401 Unauthorized — check that UVIT_TOKEN "
                    "(uvit_pat_…) is set and valid") from err
            if err.code == 403:
                raise RepoError(
                    "HTTP 403 Forbidden — token owner is not authorized "
                    "for the target tenant/repo") from err
            detail = ""
            try:
                detail = err.read(200).decode("utf-8", errors="replace")
            except OSError:
                pass
            suffix = ": %s" % detail if detail else ""
            raise RepoError("HTTP error %s%s" % (err.code, suffix)) from err
        except urllib.error.URLError as err:
            raise RepoError("Connection error: %s" % err.reason) from err

    # -- Repo API ----------------------------------------------------------

    def itemlist(self, kind):
        """Returns a list of identifiers for each item of kind.

        Only pkgsinfo has a server-side list endpoint today; other kinds
        (catalogs, manifests, icons, pkgs) return an empty list so that
        makecatalogs and iconimporter degrade gracefully."""
        resource_type = kind.split("/", 1)[0]
        if resource_type != "pkgsinfo":
            return []
        request = self._request("pkgsinfo", accept="application/json")
        with self._open(request) as response:
            decoded = json.loads(response.read())
        # Server response: {"items": [{"path": "Name/1.0.plist"}, ...]}
        return [item["path"] for item in decoded.get("items", [])]

    def get(self, resource_identifier):
        """Returns the raw bytes of a repo item.

        For pkgs/* the server issues a 307 redirect to a presigned S3 URL,
        which is followed with auth headers stripped."""
        request = self._request(resource_identifier)
        with self._open(request) as response:
            return response.read()

    def get_to_local_file(self, resource_identifier, local_file_path):
        """Streams a repo item to local_file_path."""
        request = self._request(resource_identifier)
        with self._open(request) as response, open(local_file_path, "wb") as fileobj:
            shutil.copyfileobj(response, fileobj)

    def put(self, resource_identifier, content):
        """Uploads in-memory content to the repo. Use for pkginfo blobs."""
        request = self._request(
            resource_identifier, method="PUT", body=content, include_repo_id=True)
        with self._open(request):
            pass

    def put_from_local_file(self, resource_identifier, local_file_path):
        """Streams local_file_path to the repo. Use for installer items."""
        with open(local_file_path, "rb") as fileobj:
            request = self._request(
                resource_identifier, method="PUT", body=fileobj,
                include_repo_id=True)
            request.add_header(
                "Content-Length", str(os.path.getsize(local_file_path)))
            with self._open(request):
                pass

    def delete(self, resource_identifier):
        """Deletes a repo item."""
        request = self._request(
            resource_identifier, method="DELETE", include_repo_id=True)
        with self._open(request):
            pass
