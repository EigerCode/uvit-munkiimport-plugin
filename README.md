# UVITRepo — Munki 7 repo plugin for UVIT

A [Munki 7](https://github.com/munki/munki) repo plugin that lets admins push
packages directly into [UVIT](https://uvit.eigercode.ch) using `munkiimport`
and AutoPkg's `MunkiImporter` processor.

Requires Munki 7.0 or later (the Swift plugin API introduced in Munki 7).

## Installation

Download the latest `UVITRepo-<version>.pkg` from the
[Releases](https://github.com/EigerCode/uvit-munkiimport-plugin/releases) page
and install it:

```
sudo installer -pkg UVITRepo-1.0.0.pkg -target /
```

This installs `UVITRepo.plugin` into `/usr/local/munki/repoplugins/`.

## Configuration

### 1. Get an API token

Log in to the UVIT console and go to **My Account → API Tokens → Create Token**
(`/myaccount/tokens`). Copy the generated token — it looks like `uvit_pat_…`.
This is your `UVIT_TOKEN`.

The token identifies you as a user. The server checks your membership for the
target you specify (`UVIT_TARGET`); it does not read a tenant or repo from the
token itself.

### 2. Find your target and repo ID

- **`UVIT_TARGET`**: `global` if you are a sysadmin pushing to the global scope,
  or `tenant:<id>` (e.g. `tenant:42`) for a specific tenant. The server uses
  this value to resolve the tenant and verify that the token owner is a member.
  Requests without a target are rejected with 400.

- **`UVIT_REPO_ID`**: the numeric ID of the Software Repo to write to. Find it
  in the UVIT console under **Admin > Software Repos** — the ID is shown in the
  repo detail view.

### 3. Configure munkiimport

```
munkiimport --configure
```

Set the following values when prompted:

| Setting     | Value                                          |
|-------------|------------------------------------------------|
| `repo_url`  | `https://<your-console>/api/repo`              |
| `plugin`    | `UVITRepo`                                     |

Example:

```
repo_url = https://console.uvit.eigercode.ch/api/repo
plugin   = UVITRepo
```

### 4. Supply credentials

**Option A — environment variables (session-scoped):**

```bash
export UVIT_TOKEN="uvit_pat_..."
export UVIT_TARGET="tenant:42"
export UVIT_REPO_ID="7"
munkiimport GoogleChrome.dmg
```

**Option B — macOS admin preferences (persistent, recommended for workstations):**

```bash
sudo defaults write /Library/Preferences/ch.eigercode.uvit.munkiimport \
  uvitToken "uvit_pat_..."
sudo defaults write /Library/Preferences/ch.eigercode.uvit.munkiimport \
  uvitTarget "tenant:42"
sudo defaults write /Library/Preferences/ch.eigercode.uvit.munkiimport \
  uvitRepoID "7"
```

Read them back to verify:

```bash
sudo defaults read /Library/Preferences/ch.eigercode.uvit.munkiimport
```

## Usage

```bash
munkiimport /path/to/GoogleChrome.dmg
```

munkiimport will:

1. Upload the installer to the UVIT S3 repo (`PUT /api/repo/pkgs/…`).
2. Upload the generated pkginfo plist (`PUT /api/repo/pkgsinfo/…`).
3. Call `makecatalogs` — this is a no-op on the UVIT side (catalogs are
   built dynamically from the database).

The package appears in the UVIT console immediately after a successful push.

## AutoPkg

Add these variables to your AutoPkg preferences or pass them on the command line:

```xml
<key>MUNKI_REPO</key>
<string>https://console.uvit.eigercode.ch/api/repo</string>
<key>MUNKI_REPO_PLUGIN</key>
<string>UVITRepo</string>
```

Set the UVIT variables in the environment before running AutoPkg:

```bash
export UVIT_TOKEN="uvit_pat_..."
export UVIT_TARGET="tenant:42"
export UVIT_REPO_ID="7"
autopkg run com.github.autopkg.recipe.GoogleChromePkg.munki
```

## Environment variables / preference keys

| Env var        | Pref key      | Required        | Description                                                 |
|----------------|---------------|-----------------|-------------------------------------------------------------|
| `UVIT_TOKEN`   | `uvitToken`   | Yes             | Opaque API token from My Account → API Tokens (`uvit_pat_…`). |
| `UVIT_TARGET`  | `uvitTarget`  | Yes             | `global` or `tenant:<id>`. Sent on every request. The server |
|                |               |                 | resolves the tenant from this value and checks membership.   |
| `UVIT_REPO_ID` | `uvitRepoID`  | For writes      | Numeric repo ID; required for PUT/DELETE on pkgs/pkgsinfo.  |

Preferences domain: `ch.eigercode.uvit.munkiimport`

## Endpoint mapping

| Munki call                             | UVIT API endpoint                               | Headers                          |
|----------------------------------------|-------------------------------------------------|----------------------------------|
| `list("pkgsinfo")`                     | `GET <repo_url>/pkgsinfo`                       | Auth + Target                    |
| `list(<other kind>)`                   | returns `[]` (see Known Limitations below)      | —                                |
| `get("pkgsinfo/<name>/<ver>.plist")`   | `GET <repo_url>/pkgsinfo/<name>/<ver>.plist`    | Auth + Target                    |
| `get("pkgs/<path>")`                   | `GET <repo_url>/pkgs/<path>` → 307 to S3        | Auth + Target                    |
| `put("pkgs/<path>", fromFile:)`        | `PUT <repo_url>/pkgs/<path>`                    | Auth + Target + Repo-ID          |
| `put("pkgsinfo/<path>", content:)`     | `PUT <repo_url>/pkgsinfo/<path>`                | Auth + Target + Repo-ID          |
| `delete("pkgsinfo/<path>")`            | `DELETE <repo_url>/pkgsinfo/<path>`             | Auth + Target + Repo-ID          |
| `delete("pkgs/<path>")`                | `DELETE <repo_url>/pkgs/<path>`                 | Auth + Target + Repo-ID          |
| `pathFor(_)`                           | `nil` (non-filesystem repo)                     | —                                |
| `makecatalogs` (called by Munki tools) | `POST <repo_url>/makecatalogs` → no-op 200      | Auth + Target                    |

Header names: `Authorization: Bearer <token>`, `X-UVIT-Target: <target>`,
`X-UVIT-Repo-ID: <repoID>`.

## Known limitations

### dylib code-signing / Library Validation (must verify on a real Mac)

macOS Library Validation requires that a dylib loaded by a hardened process
either shares the same Apple Developer Team ID as the host binary, or that
the host binary carries the
`com.apple.security.cs.disable-library-validation` entitlement.

Munki 7 binaries are signed by the Munki project (Greg Neagle / googlemunki).
**UVITRepo.plugin is signed by EigerCode GmbH (different Team ID).** Whether
macOS allows the load depends on Munki's exact entitlements.

This must be tested on a real Mac running a production Munki 7 build before
deploying to a fleet. If Library Validation blocks the load, options are:

1. File a Munki issue requesting
   `com.apple.security.cs.disable-library-validation` be added to Munki's
   entitlements (the correct long-term fix).
2. Use an ad-hoc or unsigned build of the plugin for internal testing only.

### catalogs / manifests / icons list not implemented server-side

`list("catalogs")`, `list("manifests")`, and `list("icons")` return empty
lists. This means:

- `makecatalogs` (separate Munki tool) cannot enumerate catalogs via the API
  — it is not needed here because UVIT builds catalogs dynamically.
- `iconimporter` cannot sync icons via this API.

These require server-side list endpoints that are planned as a follow-up.

## Building from source

Requires Xcode 16+ and macOS 12+.

```bash
xcodebuild build \
  -project UVITRepo.xcodeproj \
  -configuration Release \
  -scheme UVITRepo \
  -destination "generic/platform=macOS" \
  -derivedDataPath build
```

Or use the convenience script (also creates a .pkg):

```bash
./build_pkg.sh
```

## License

MIT — see [LICENSE](LICENSE).
