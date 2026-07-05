//
//  UVITRepo.swift
//  UVITRepo
//
//  Munki 7 repo plugin — pushes packages into UVIT via the /api/repo HTTP API.
//
//  MIT License — Copyright (c) 2026 EigerCode GmbH
//  See LICENSE for the full license text.

import Foundation

// MARK: - Error types

class RepoError: Error, CustomStringConvertible {
    private let message: String

    public init(_ message: String) {
        self.message = message
    }

    public var description: String { message }
}

extension RepoError: LocalizedError {
    var errorDescription: String? { message }
}

class HTTPRepoError: Error, CustomStringConvertible {
    public let statusCode: Int
    public let body: String

    public init(_ statusCode: Int, body: String = "") {
        self.statusCode = statusCode
        self.body = body
    }

    public var description: String {
        switch statusCode {
        case 401:
            return "HTTP 401 Unauthorized — check that UVIT_TOKEN is set and not expired"
        case 403:
            return "HTTP 403 Forbidden — token lacks permission for this tenant/repo"
        default:
            let suffix = body.isEmpty ? "" : ": \(body.prefix(200))"
            return "HTTP error \(statusCode)\(suffix)"
        }
    }
}

extension HTTPRepoError: LocalizedError {
    var errorDescription: String? { description }
}

// MARK: - HTTP helpers

/// Throws an HTTPRepoError if the HTTP status code is not 2xx.
private func throwIfStatusNotOK(_ response: URLResponse, body: Data = Data()) throws {
    guard let http = response as? HTTPURLResponse else {
        throw RepoError("Response was not an HTTPURLResponse")
    }
    guard (200...299).contains(http.statusCode) else {
        let bodyString = String(data: body.prefix(512), encoding: .utf8) ?? ""
        throw HTTPRepoError(http.statusCode, body: bodyString)
    }
}

// MARK: - UVITRepo

/// Munki 7 repo plugin that targets the UVIT /api/repo HTTP ingest API.
///
/// ## Configuration
///
/// Read from the macOS preferences domain `ch.eigercode.uvit.munkiimport`, with
/// environment variable fallbacks:
///
/// | Pref key      | Env var       | Description                                           |
/// |---------------|---------------|-------------------------------------------------------|
/// | `uvitToken`   | `UVIT_TOKEN`  | Bearer JWT minted from the UVIT console (required).   |
/// |               |               | The JWT already encodes tenant + repo binding.        |
/// | `uvitTarget`  | `UVIT_TARGET` | Optional target hint (`global` or `tenant:<id>`).     |
/// |               |               | Supplementary — the JWT carries the binding.          |
/// | `uvitRepoID`  | `UVIT_REPO_ID`| Optional repo-ID hint. Same note as uvitTarget.       |
///
/// Write system-wide prefs (requires sudo):
///
///   sudo defaults write /Library/Preferences/ch.eigercode.uvit.munkiimport \
///     uvitToken "eyJ..."
///
/// ## Endpoint mapping
///
/// | Munki call                              | UVIT API call                              |
/// |-----------------------------------------|--------------------------------------------|
/// | `list("pkgsinfo")`                      | `GET <baseURL>/pkgsinfo`                   |
/// | `list(<other>)`                         | returns [] (server-side follow-up needed)  |
/// | `get("pkgsinfo/<name>/<ver>.plist")`    | `GET <baseURL>/pkgsinfo/<name>/<ver>.plist`|
/// | `get("pkgs/<path>")`                    | `GET <baseURL>/pkgs/<path>` (307 redirect) |
/// | `put("pkgs/<path>", fromFile:)`         | `PUT <baseURL>/pkgs/<path>`                |
/// | `put("pkgsinfo/<path>", content:)`      | `PUT <baseURL>/pkgsinfo/<path>`            |
/// | `delete("pkgsinfo/<path>")`             | `DELETE <baseURL>/pkgsinfo/<path>`         |
/// | `delete("pkgs/<path>")`                 | `DELETE <baseURL>/pkgs/<path>`             |
/// | `pathFor(_)`                            | returns nil (non-filesystem repo)          |
///
/// Note: UVIT catalogs are dynamic; makecatalogs is a no-op on the server side.
// ponytail: server-side list endpoints for catalogs/manifests/icons are needed
// for full makecatalogs + iconimporter parity; tracked as a server-side follow-up.
// When the server adds them, extend list() to handle those kinds here.
class UVITRepo: Repo {
    static let prefsDomain = "ch.eigercode.uvit.munkiimport"

    var baseURL: URL
    var token: String = ""
    var target: String = ""
    var repoID: String = ""

    required init(_ url: String) throws {
        guard let parsedURL = URL(string: url) else {
            throw RepoError("Could not create valid URL from \(url)")
        }
        self.baseURL = parsedURL
        loadCredentials()
    }

    // MARK: - Credential loading

    /// Loads credentials from the macOS preferences domain
    /// `ch.eigercode.uvit.munkiimport`, falling back to environment variables.
    private func loadCredentials() {
        let prefs = UserDefaults(suiteName: Self.prefsDomain)
        let env = ProcessInfo.processInfo.environment

        token = prefs?.string(forKey: "uvitToken") ?? env["UVIT_TOKEN"] ?? ""
        target = prefs?.string(forKey: "uvitTarget") ?? env["UVIT_TARGET"] ?? ""
        repoID = prefs?.string(forKey: "uvitRepoID") ?? env["UVIT_REPO_ID"] ?? ""

        if token.isEmpty {
            fputs("[UVITRepo] WARNING: No token found in preferences or environment.\n", stderr)
            fputs("[UVITRepo] Set uvitToken in /Library/Preferences/\(Self.prefsDomain).plist\n", stderr)
            fputs("[UVITRepo] or export UVIT_TOKEN=<token> before running munkiimport.\n", stderr)
        }
    }

    // MARK: - Request builder

    /// Returns a URLRequest with auth headers applied.
    ///
    /// - Parameter includeRepoID: Pass `true` for mutating requests
    ///   (PUT/DELETE on pkgs and pkgsinfo). Adds `X-UVIT-Repo-ID` if configured.
    private func baseRequest(_ url: URL, includeRepoID: Bool = false) -> URLRequest {
        var request = URLRequest(url: url)
        if !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if !target.isEmpty {
            request.setValue(target, forHTTPHeaderField: "X-UVIT-Target")
        }
        if includeRepoID && !repoID.isEmpty {
            request.setValue(repoID, forHTTPHeaderField: "X-UVIT-Repo-ID")
        }
        return request
    }

    /// Appends a path component to the base URL.
    private func url(appending path: String) -> URL {
        if #available(macOS 13.0, *) {
            return baseURL.appending(path: path)
        } else {
            return baseURL.appendingPathComponent(path)
        }
    }

    // MARK: - Repo protocol

    /// Lists item paths for the given repo kind.
    ///
    /// Only `pkgsinfo` has a server-side list endpoint today. Other kinds
    /// (catalogs, manifests, icons) return an empty list so that makecatalogs
    /// and iconimporter degrade gracefully rather than erroring out.
    func list(_ kind: String) async throws -> [String] {
        let resourceType = (kind as NSString).pathComponents.first ?? kind

        // Only pkgsinfo is implemented server-side; other kinds need follow-up.
        guard resourceType == "pkgsinfo" else {
            return []
        }

        let listURL = url(appending: "pkgsinfo")
        var request = baseRequest(listURL)
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response) = try await URLSession.shared.data(for: request)
        try throwIfStatusNotOK(response, body: data)

        // Server response: {"items": [{"path": "Name/1.0.plist"}, ...]}
        struct ListItem: Decodable { let path: String }
        struct ListResponse: Decodable { let items: [ListItem] }

        let decoded = try JSONDecoder().decode(ListResponse.self, from: data)
        return decoded.items.map(\.path)
    }

    /// Returns the raw bytes of a repo item.
    ///
    /// For `pkgsinfo/*` this is the plist blob. For `pkgs/*` the server issues
    /// a 307 redirect to a presigned S3 URL; URLSession follows it automatically.
    func get(_ identifier: String) async throws -> Data {
        let itemURL = url(appending: identifier)
        let request = baseRequest(itemURL)
        let (data, response) = try await URLSession.shared.data(for: request)
        try throwIfStatusNotOK(response, body: data)
        return data
    }

    /// Downloads a repo item and writes it to `local_file_path`.
    func get(_ identifier: String, toFile local_file_path: String) async throws {
        let data = try await get(identifier)
        FileManager.default.createFile(atPath: local_file_path, contents: data)
    }

    /// Uploads in-memory `content` to the repo at `identifier`.
    ///
    /// Use for pkgsinfo blobs. For large installer binaries prefer `put(_:fromFile:)`.
    func put(_ identifier: String, content: Data) async throws {
        let itemURL = url(appending: identifier)
        var request = baseRequest(itemURL, includeRepoID: true)
        request.httpMethod = "PUT"
        let resourceType = (identifier as NSString).pathComponents.first ?? ""
        if resourceType == "pkgsinfo" {
            request.setValue("application/xml", forHTTPHeaderField: "Content-Type")
        } else {
            request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        }
        let (responseData, response) = try await URLSession.shared.upload(for: request, from: content)
        try throwIfStatusNotOK(response, body: responseData)
    }

    /// Streams `local_file_path` to the repo at `identifier`.
    ///
    /// Use for installer binaries (pkgs/*). URLSession streams the file without
    /// loading it entirely into memory.
    func put(_ identifier: String, fromFile local_file_path: String) async throws {
        let itemURL = url(appending: identifier)
        var request = baseRequest(itemURL, includeRepoID: true)
        request.httpMethod = "PUT"
        let resourceType = (identifier as NSString).pathComponents.first ?? ""
        if resourceType == "pkgsinfo" {
            request.setValue("application/xml", forHTTPHeaderField: "Content-Type")
        } else {
            request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        }
        let localFile = URL(fileURLWithPath: local_file_path)
        let (responseData, response) = try await URLSession.shared.upload(for: request, fromFile: localFile)
        try throwIfStatusNotOK(response, body: responseData)
    }

    /// Deletes a repo item.
    func delete(_ identifier: String) async throws {
        let itemURL = url(appending: identifier)
        var request = baseRequest(itemURL, includeRepoID: true)
        request.httpMethod = "DELETE"
        let (responseData, response) = try await URLSession.shared.data(for: request)
        try throwIfStatusNotOK(response, body: responseData)
    }

    /// Non-filesystem repo — no local path exists for any identifier.
    func pathFor(_: String) -> String? { nil }
}

// MARK: - dylib entry point

/// C-callable factory function loaded by Munki's plugin system.
///
/// Munki resolves `createPlugin` from the dylib at runtime; this function
/// instantiates a `UVITRepoBuilder` and hands it to the Objective-C runtime.
@_cdecl("createPlugin")
public func createPlugin() -> UnsafeMutableRawPointer {
    return Unmanaged.passRetained(UVITRepoBuilder()).toOpaque()
}

final class UVITRepoBuilder: RepoPluginBuilder {
    override func connect(_ url: String) -> Repo? {
        return try? UVITRepo(url)
    }
}
