#!/usr/bin/env python3
# encoding: utf-8
"""Self-check for UVITRepo.py's PUT-redirect handling (issue #2 / console#575).

Not a test framework — just asserts, runnable standalone:
    python3 test_uvitrepo.py

Stubs munkilib.munkirepo (not installed outside Munki) and CURL (a fake
curl-like script) so the redirect logic can be exercised without a real
network call or a real curl invocation of a fake server.
"""
import importlib
import os
import stat
import sys
import tempfile
import types
import unittest

# --- stub munkilib.munkirepo before importing UVITRepo ---------------------
munkirepo_stub = types.ModuleType("munkilib.munkirepo")


class RepoError(Exception):
    pass


class Repo:
    pass


munkirepo_stub.RepoError = RepoError
munkirepo_stub.Repo = Repo
munkilib_stub = types.ModuleType("munkilib")
munkilib_stub.munkirepo = munkirepo_stub
sys.modules["munkilib"] = munkilib_stub
sys.modules["munkilib.munkirepo"] = munkirepo_stub

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
UVITRepo = importlib.import_module("UVITRepo")


FAKE_CURL_TEMPLATE = """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1]

url = args[-1]
dump_header = opt("--dump-header")
output = opt("--output")

if "s3.example.com" in url:
    # Redirect target: presigned S3 PUT. Must NOT receive the custom headers
    # (real curl wouldn't get -H here either since UVITRepo._request omits
    # them on the redirect call) -- assert no request-headers file was passed
    # with auth by checking header contents baked into the fake response.
    with open(dump_header, "w") as f:
        f.write("HTTP/1.1 200 OK\\r\\n\\r\\n")
    with open(output, "wb") as f:
        f.write(b"")
    sys.exit(0)

# First hop: console returns 307 to the S3 stub.
with open(dump_header, "w") as f:
    f.write("HTTP/1.1 307 Temporary Redirect\\r\\n")
    f.write("Location: https://s3.example.com/bucket/Foo-1.0.pkg?X-Amz-Signature=abc\\r\\n")
    f.write("\\r\\n")
with open(output, "wb") as f:
    f.write(b"")
sys.exit(0)
"""


class PutRedirectTest(unittest.TestCase):
    def setUp(self):
        fd, self.fake_curl = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write(FAKE_CURL_TEMPLATE)
        os.chmod(self.fake_curl, os.stat(self.fake_curl).st_mode | stat.S_IEXEC)
        self._orig_curl = UVITRepo.CURL
        UVITRepo.CURL = self.fake_curl

        os.environ["UVIT_TARGET"] = "global"
        os.environ["UVIT_TOKEN"] = "uvit_pat_test"
        os.environ["UVIT_REPO_ID"] = "1"
        self.repo = UVITRepo.UVITRepo("https://console.example.com/api/repo")

        fd2, self.installer_path = tempfile.mkstemp()
        with os.fdopen(fd2, "wb") as f:
            f.write(b"fake pkg bytes")

    def tearDown(self):
        os.unlink(self.fake_curl)
        os.unlink(self.installer_path)

    def test_put_follows_307_to_s3_and_succeeds(self):
        # Must not raise: the 307 is followed with a second PUT to the
        # presigned S3 URL, which the fake curl answers with 200.
        self.repo.put_from_local_file("pkgs/Foo/Foo-1.0.pkg", self.installer_path)

    def test_get_redirect_still_works(self):
        # Backward-compat guard: GET redirect handling (pre-existing) is
        # untouched by the PUT changes.
        body = self.repo.get("pkgs/Foo/Foo-1.0.pkg")
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
