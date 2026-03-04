import SwiftUI
import Foundation
import Security
import CryptoKit

@main
struct InteropTesterMacApp: App {
    @StateObject private var viewModel = InteropViewModel()

    var body: some Scene {
        WindowGroup {
            VStack(alignment: .leading, spacing: 10) {
                Text("Engine Endpoint").font(.headline)
                TextField("Base URL", text: $viewModel.baseUrl)

                Text("Auth").font(.headline)
                HStack {
                    TextField("Bootstrap token", text: $viewModel.bootstrapToken)
                    Button("Exchange Bootstrap") { Task { await viewModel.exchangeBootstrap() } }
                    Button("Issue Dev Token") { Task { await viewModel.issueDevToken() } }
                }
                TextField("Bearer token", text: $viewModel.bearerToken)
                HStack {
                    TextField("TLS cert SHA-256 pin (required for HTTPS)", text: $viewModel.tlsPin)
                    Button("Fetch TLS Pin") { Task { await viewModel.fetchTlsPin(forceRefresh: true) } }
                }

                Text("Cluster").font(.headline)
                HStack {
                    Button("Refresh Peers") { Task { await viewModel.refreshPeers() } }
                    TextField("Capability filter", text: $viewModel.capabilityFilter)
                    Toggle("Auto Refresh", isOn: $viewModel.autoRefresh)
                    TextField("Interval ms", text: $viewModel.refreshIntervalMs).frame(width: 120)
                    Button("Export Snapshot") { Task { await viewModel.exportSnapshot() } }
                }

                Text("Clock Control").font(.headline)
                HStack {
                    TextField("Peer Id", text: $viewModel.peerId)
                    TextField("Execution Session Id", text: $viewModel.executionSessionId)
                }

                HStack {
                    Button("Request Authority") { Task { await viewModel.requestAuthority() } }
                    Button("Release Authority") { Task { await viewModel.releaseAuthority() } }
                    Button("Apply Clock Sync") { Task { await viewModel.applyClockSync() } }
                }

                TextEditor(text: $viewModel.clusterJson)
                    .font(.system(size: 12, design: .monospaced))
                    .frame(minHeight: 320)

                TextEditor(text: $viewModel.logText)
                    .font(.system(size: 12, design: .monospaced))
                    .frame(minHeight: 180)
            }
            .padding(16)
            .frame(minWidth: 980, minHeight: 760)
            .onChange(of: viewModel.autoRefresh) { _, enabled in
                if enabled {
                    viewModel.startAutoRefresh()
                } else {
                    viewModel.stopAutoRefresh()
                }
            }
        }
    }
}

@MainActor
final class InteropViewModel: ObservableObject {
    @Published var baseUrl: String = "https://127.0.0.1:5109"
    @Published var bootstrapToken: String = ""
    @Published var bearerToken: String = ""
    @Published var tlsPin: String = ""
    @Published var capabilityFilter: String = ""
    @Published var autoRefresh: Bool = false
    @Published var refreshIntervalMs: String = "3000"

    @Published var peerId: String = ""
    @Published var executionSessionId: String = "session-a"
    @Published var clusterJson: String = ""
    @Published var logText: String = ""

    private var timerTask: Task<Void, Never>?
    private var session: URLSession?
    private var sessionCacheKey: String = ""

    func refreshPeers() async {
        do {
            let data = try await get(path: "/v1/interop/cluster/capabilities")
            hydratePeerSelectionIfNeeded(data)
            log(clusterSummary(data))
            if let filter = normalizedCapabilityFilter(), !filter.isEmpty {
                clusterJson = filterClusterPayload(data: data, capability: filter)
            } else {
                clusterJson = pretty(data)
            }
            log("cluster capabilities refreshed")
        } catch {
            log("refresh failed: \(error.localizedDescription)")
        }
    }

    func exchangeBootstrap() async {
        guard !bootstrapToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            log("bootstrap token required")
            return
        }

        do {
            let data = try await post(path: "/v1/auth/bootstrap/exchange", body: [
                "bootstrapToken": bootstrapToken
            ], includeAuth: false)
            if let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let token = object["accessToken"] as? String {
                bearerToken = token
                log("bootstrap exchange succeeded")
            } else {
                log("bootstrap exchange returned no accessToken")
            }
        } catch {
            log("bootstrap exchange failed: \(error.localizedDescription)")
        }
    }

    func issueDevToken() async {
        do {
            let data = try await post(path: "/v1/auth/bootstrap/dev/issue", body: ["principal": "developer"], includeAuth: false)
            if let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let token = object["bootstrapToken"] as? String {
                bootstrapToken = token
                log("dev bootstrap token issued")
            } else {
                log("dev issue returned no bootstrapToken")
            }
        } catch {
            log("dev token issue failed: \(error.localizedDescription)")
        }
    }

    func requestAuthority() async {
        await postClock(path: "/v1/interop/peers/\(peerId)/clock/authority/request", body: [
            "executionSessionId": executionSessionId,
            "autoAccept": false
        ])
    }

    func releaseAuthority() async {
        await postClock(path: "/v1/interop/peers/\(peerId)/clock/authority/release", body: [
            "executionSessionId": executionSessionId,
            "reasonCode": "tester_release"
        ])
    }

    func applyClockSync() async {
        let nowNs = Int64(Date().timeIntervalSince1970 * 1_000_000_000)
        await postClock(path: "/v1/interop/peers/\(peerId)/clock/sync/apply", body: [
            "executionSessionId": executionSessionId,
            "externalTick": 120,
            "externalTimeNs": nowNs,
            "reasonCode": "tester_sync"
        ])
    }

    func exportSnapshot() async {
        do {
            let exportDir = URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent("Library/Application Support/EngineInteropTester/exports", isDirectory: true)
            try FileManager.default.createDirectory(at: exportDir, withIntermediateDirectories: true)
            let file = exportDir.appendingPathComponent("interop_snapshot_\(timestamp()).json")
            let payload: [String: Any] = [
                "baseUrl": baseUrl,
                "capabilityFilter": capabilityFilter,
                "cluster": parseJsonString(clusterJson) ?? NSNull(),
                "log": logText,
                "exportedAtUtc": ISO8601DateFormatter().string(from: Date())
            ]
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: file)
            log("snapshot exported: \(file.path)")
        } catch {
            log("export failed: \(error.localizedDescription)")
        }
    }

    func startAutoRefresh() {
        stopAutoRefresh()
        let interval = max(1.0, (Double(refreshIntervalMs) ?? 3000) / 1000.0)
        timerTask = Task {
            while !Task.isCancelled {
                await refreshPeers()
                try? await Task.sleep(for: .seconds(interval))
            }
        }
        log("auto refresh enabled")
    }

    func stopAutoRefresh() {
        timerTask?.cancel()
        timerTask = nil
        log("auto refresh disabled")
    }

    private func get(path: String) async throws -> Data {
        _ = try await ensureTlsPinIfNeeded(forceRefresh: false)
        var request = URLRequest(url: try absolute(path: path))
        request.httpMethod = "GET"
        applyHeaders(&request, includeAuth: true)
        let (data, response) = try await urlSession(for: request.url).data(for: request)
        try ensureSuccess(response: response, data: data)
        return data
    }

    private func post(path: String, body: [String: Any], includeAuth: Bool) async throws -> Data {
        _ = try await ensureTlsPinIfNeeded(forceRefresh: false)
        var request = URLRequest(url: try absolute(path: path))
        request.httpMethod = "POST"
        applyHeaders(&request, includeAuth: includeAuth)
        request.httpBody = try JSONSerialization.data(withJSONObject: body, options: [])
        let (data, response) = try await urlSession(for: request.url).data(for: request)
        try ensureSuccess(response: response, data: data)
        return data
    }

    private func postClock(path: String, body: [String: Any]) async {
        do {
            let data = try await post(path: path, body: body, includeAuth: true)
            log("POST \(path) ok")
            log(pretty(data))
        } catch {
            log("POST \(path) failed: \(error.localizedDescription)")
        }
    }

    private func applyHeaders(_ request: inout URLRequest, includeAuth: Bool) {
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if includeAuth {
            let token = bearerToken.trimmingCharacters(in: .whitespacesAndNewlines)
            if !token.isEmpty {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }
        }
    }

    private func ensureSuccess(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else { return }
        guard (200...299).contains(http.statusCode) else {
            throw NSError(domain: "InteropTester", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: String(data: data, encoding: .utf8) ?? "request failed"])
        }
    }

    private func absolute(path: String) throws -> URL {
        guard let base = URL(string: baseUrl), let url = URL(string: path, relativeTo: base)?.absoluteURL else {
            throw NSError(domain: "InteropTester", code: -1, userInfo: [NSLocalizedDescriptionKey: "Invalid base URL"])
        }

        if url.scheme?.lowercased() == "https" {
            let pin = normalizedPin()
            if pin.isEmpty {
                throw NSError(domain: "InteropTester", code: -2, userInfo: [NSLocalizedDescriptionKey: "HTTPS requires TLS pin"])
            }
        }

        return url
    }

    @discardableResult
    func fetchTlsPin(forceRefresh: Bool) async -> String? {
        do {
            let pin = try await ensureTlsPinIfNeeded(forceRefresh: forceRefresh)
            if let pin {
                log("tls pin fetched: \(pin)")
            }
            return pin
        } catch {
            log("tls pin fetch failed: \(error.localizedDescription)")
            return nil
        }
    }

    private func ensureTlsPinIfNeeded(forceRefresh: Bool) async throws -> String? {
        guard let base = URL(string: baseUrl), base.scheme?.lowercased() == "https" else {
            return nil
        }

        let current = normalizedPin()
        if !forceRefresh && !current.isEmpty {
            return current
        }

        let pin = try await resolveTlsPin(base: base)
        tlsPin = pin
        session = nil
        sessionCacheKey = ""
        return pin
    }

    private func resolveTlsPin(base: URL) async throws -> String {
        let probeUrl = URL(string: "/", relativeTo: base)?.absoluteURL ?? base
        var request = URLRequest(url: probeUrl)
        request.httpMethod = "HEAD"

        return try await withCheckedThrowingContinuation { continuation in
            let delegate = TlsProbeDelegate()
            delegate.onPin = { pin in
                continuation.resume(returning: pin)
            }
            delegate.onError = { error in
                continuation.resume(throwing: error)
            }

            let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
            let task = session.dataTask(with: request) { _, _, error in
                if let error {
                    delegate.failIfPending(error)
                    return
                }

                delegate.failIfPending(NSError(domain: "InteropTester", code: -3, userInfo: [NSLocalizedDescriptionKey: "TLS probe completed without certificate"]))
            }
            task.resume()
        }
    }

    private func urlSession(for url: URL?) -> URLSession {
        guard let url else {
            return URLSession.shared
        }

        let pin = normalizedPin()
        let key = "\(url.scheme ?? "")|\(pin)"
        if let session, key == sessionCacheKey {
            return session
        }

        let config = URLSessionConfiguration.default
        let delegate = PinnedSessionDelegate(expectedPin: pin)
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        self.session = session
        self.sessionCacheKey = key
        return session
    }

    private func normalizedPin() -> String {
        String(tlsPin.filter(\.isHexDigit).uppercased())
    }

    private func normalizedCapabilityFilter() -> String? {
        let value = capabilityFilter.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }

    private func filterClusterPayload(data: Data, capability: String) -> String {
        guard var root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let items = root["items"] as? [[String: Any]] else {
            return pretty(data)
        }

        let filtered = items.filter { item in
            guard let capabilities = item["capabilities"] as? [String] else { return false }
            return capabilities.contains { $0.caseInsensitiveCompare(capability) == .orderedSame }
        }

        root["filteredPeers"] = filtered.count
        root["capabilityFilter"] = capability
        root["items"] = filtered
        let encoded = (try? JSONSerialization.data(withJSONObject: root, options: [.prettyPrinted, .sortedKeys])) ?? data
        return String(data: encoded, encoding: .utf8) ?? "{}"
    }

    private func parseJsonString(_ value: String) -> Any? {
        guard let data = value.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: data)
    }

    private func pretty(_ data: Data) -> String {
        if let object = try? JSONSerialization.jsonObject(with: data),
           let encoded = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]) {
            return String(data: encoded, encoding: .utf8) ?? "{}"
        }

        return String(data: data, encoding: .utf8) ?? "{}"
    }

    private func timestamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter.string(from: Date())
    }

    private func log(_ message: String) {
        logText = "[\(Date())] \(message)\n" + logText
    }

    private func hydratePeerSelectionIfNeeded(_ data: Data) {
        guard peerId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return
        }

        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let items = root["items"] as? [[String: Any]] else {
            return
        }

        if let first = items.first?["peerId"] as? String, !first.isEmpty {
            peerId = first
        }
    }

    private func clusterSummary(_ data: Data) -> String {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let items = root["items"] as? [[String: Any]] else {
            return "cluster summary unavailable"
        }

        var unique = Set<String>()
        for item in items {
            if let capabilities = item["capabilities"] as? [String] {
                capabilities.forEach { unique.insert($0.lowercased()) }
            }
        }

        return "cluster summary: peers=\(items.count), uniqueCapabilities=\(unique.count)"
    }
}

final class PinnedSessionDelegate: NSObject, URLSessionDelegate {
    private let expectedPin: String

    init(expectedPin: String) {
        self.expectedPin = expectedPin
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }

        guard !expectedPin.isEmpty else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        guard let certificate = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        let data = SecCertificateCopyData(certificate) as Data
        let digest = sha256Hex(data)
        if digest.caseInsensitiveCompare(expectedPin) == .orderedSame {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    private func sha256Hex(_ data: Data) -> String {
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02X", $0) }.joined()
    }
}

final class TlsProbeDelegate: NSObject, URLSessionDelegate {
    var onPin: ((String) -> Void)?
    var onError: ((Error) -> Void)?
    private var completed = false

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let certificate = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.performDefaultHandling, nil)
            return
        }

        let certData = SecCertificateCopyData(certificate) as Data
        let digest = SHA256.hash(data: certData).map { String(format: "%02X", $0) }.joined()
        if !completed {
            completed = true
            onPin?(digest)
        }

        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func failIfPending(_ error: Error) {
        if completed {
            return
        }

        completed = true
        onError?(error)
    }
}
