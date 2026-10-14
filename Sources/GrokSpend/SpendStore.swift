import AppKit
import Combine
import Foundation
import UniformTypeIdentifiers

@MainActor
final class SpendStore: ObservableObject {
    @Published private(set) var snapshot: SpendSnapshot?
    @Published private(set) var isRefreshing = false
    @Published private(set) var error: String?
    @Published private(set) var exportNotice: String?
    @Published private(set) var autoRefreshEnabled = false
    @Published private(set) var autoRefreshPaused = false
    @Published private(set) var hasSession = false
    let isDemo: Bool
    let isFilePreview: Bool
    let snapshotURL: URL
    private var snapshotData: Data?
    private var autoRefreshTask: Task<Void, Never>?
    private var session: CollectorSession?
    private var connectionGeneration = UUID()
    private var disconnected = false
    private var terminationObserver: NSObjectProtocol?

    init() {
        let args = CommandLine.arguments
        isDemo = args.contains("--demo")
        let fileIndex = args.firstIndex(of: "--snapshot")
        isFilePreview = fileIndex != nil && !isDemo
        if isDemo {
            snapshotURL = Bundle.main.resourceURL!.appendingPathComponent("demo-snapshot.json")
        } else if let fileIndex, args.indices.contains(fileIndex + 1) {
            snapshotURL = URL(fileURLWithPath: NSString(string: args[fileIndex + 1]).expandingTildeInPath)
        } else {
            snapshotURL = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/Grok Spend/snapshot.json")
        }
        loadSnapshot(initial: true)
        terminationObserver = NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.disconnect() }
        }
    }

    var demo: Bool { isDemo || snapshot?.mode == "demo" }
    var todayCents: Double? {
        guard demo || snapshotIsFromToday else { return nil }
        return snapshot?.todayValueCents
    }
    var todayCaption: String {
        guard todayCents != nil else { return "Today’s dollar value is unavailable" }
        return snapshot?.todayIsRecordedValue == true ? "Recorded usage value, USD · not cash charges" : "Provider-reported usage cost, USD"
    }
    var snapshotIsFromToday: Bool {
        guard let raw = snapshot?.generatedAt, let date = Dates.parse(raw) else { return false }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar.isDate(date, inSameDayAs: Date())
    }
    var lastRefresh: String {
        guard let raw = snapshot?.generatedAt, let date = Dates.parse(raw) else { return "Not refreshed yet" }
        return date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }
    var statusText: String {
        if demo { return "Synthetic demo" }
        if isRefreshing { return "Refreshing…" }
        if error != nil { return "Refresh needs attention" }
        if isFilePreview { return "Snapshot preview" }
        if disconnected { return "Disconnected · saved snapshot" }
        if snapshot == nil { return "Ready to connect" }
        if !snapshotIsFromToday { return "Older snapshot · refresh for today" }
        return "Local billing snapshot"
    }
    var refreshCadence: String {
        if demo { return "Demo · no background connections" }
        if isFilePreview { return "File preview · no background connections" }
        if autoRefreshEnabled { return "Refreshes every 15 minutes · no background prompts" }
        if autoRefreshPaused { return "Automatic refresh paused · refresh manually to resume" }
        return "Refresh to enable updates every 15 minutes"
    }

    func loadSnapshot(initial: Bool = false) {
        do {
            let data = try Data(contentsOf: snapshotURL)
            let value = try JSONDecoder().decode(SpendSnapshot.self, from: data)
            guard value.schemaVersion == 1 else {
                error = "This snapshot format is newer than this app. Update Grok Spend to read it."
                return
            }
            snapshot = value
            snapshotData = data
            error = value.lastError
        } catch {
            if initial && !isDemo && !isFilePreview && !FileManager.default.fileExists(atPath: snapshotURL.path) {
                self.error = nil
            } else {
                self.error = "The snapshot could not be read. Any previously loaded values are still shown."
            }
        }
    }

    func refresh(allowPrompt: Bool = true) {
        guard !isRefreshing else { return }
        exportNotice = nil
        if isDemo || isFilePreview { loadSnapshot(); return }
        guard let resources = Bundle.main.resourceURL, let executable = Bundle.main.executableURL else {
            error = "Open the packaged Grok Spend app to refresh billing."
            return
        }
        let collector = resources.appendingPathComponent("collector/session.py")
        let helper = executable.deletingLastPathComponent().appendingPathComponent("GrokSpendAuth")
        guard FileManager.default.fileExists(atPath: collector.path), FileManager.default.isExecutableFile(atPath: helper.path) else {
            error = "The collector is missing. Rebuild the app with scripts/build-app.sh."
            return
        }
        if session?.isRunning != true {
            session?.stop()
            session = nil
            hasSession = false
            guard allowPrompt else {
                pauseAutomaticRefresh()
                error = "The local session has ended. Refresh manually to reconnect. Your last snapshot is preserved."
                return
            }
            do {
                session = try CollectorSession(script: collector.path, helper: helper.path, snapshot: snapshotURL.path)
                hasSession = true
            } catch {
                self.error = "Could not start the collector session. Check Python 3.10 or later is installed."
                return
            }
        }
        guard let worker = session else { return }
        isRefreshing = true
        error = nil
        disconnected = false
        let generation = connectionGeneration
        Task {
            let result = await Task.detached(priority: .userInitiated) {
                worker.refresh(allowPrompt: allowPrompt)
            }.value
            guard generation == connectionGeneration else { return }
            isRefreshing = false
            if result.status == 0 {
                loadSnapshot()
                if error == nil && allowPrompt { startAutomaticRefresh() }
            } else {
                if result.status == 2 {
                    worker.stop()
                    session = nil
                    hasSession = false
                    pauseAutomaticRefresh()
                    error = (result.detail ?? "Grok Bot sign-in is unavailable.") + " Your last snapshot is preserved. Automatic updates are paused; refresh manually to resume."
                } else {
                    if !worker.isRunning {
                        session = nil
                        hasSession = false
                        pauseAutomaticRefresh()
                    }
                    error = (result.detail ?? "Refresh failed (collector status \(result.status)).") + " Your last successful snapshot is preserved."
                }
            }
        }
    }

    func disconnect() {
        connectionGeneration = UUID()
        autoRefreshTask?.cancel()
        autoRefreshTask = nil
        autoRefreshEnabled = false
        autoRefreshPaused = false
        session?.stop()
        session = nil
        hasSession = false
        isRefreshing = false
        disconnected = true
        error = nil
    }

    private func startAutomaticRefresh() {
        guard !isDemo, !isFilePreview, !autoRefreshEnabled else { return }
        autoRefreshEnabled = true
        autoRefreshPaused = false
        autoRefreshTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(15 * 60)) } catch { return }
                guard self?.autoRefreshEnabled == true else { return }
                self?.refresh(allowPrompt: false)
            }
        }
    }

    private func pauseAutomaticRefresh() {
        autoRefreshTask?.cancel()
        autoRefreshTask = nil
        autoRefreshEnabled = false
        autoRefreshPaused = true
    }

    func exportJSON() {
        guard let snapshotData else { return }
        save(snapshotData, name: demo ? "grok-spend-demo.json" : "grok-spend-snapshot.json", type: .json)
    }

    func exportCSV(category: SpendCategory) {
        guard let snapshot else { return }
        let header = ["mode", "period_start", "period_end", "category", "id", "name", "usage_cents", "included_cents", "on_demand_cents", "tokens", "runs", "attribution", "cost_basis", "model_cost_cents", "provider_recorded_cents", "event_value_cents"]
        let rows: [[String]] = snapshot.rows(for: category).map { row in
            let metrics = [row.usageCents, row.includedCents, row.onDemandCents, row.tokens, row.runs]
                .map { value -> String in value.map { String($0) } ?? "" }
            let identity = [snapshot.mode, snapshot.period.start, snapshot.period.end, category.rawValue, row.id, row.name]
            let eventMetrics = [row.modelCostCents, row.chargedCents, row.eventValueCents].map { value -> String in value.map { String($0) } ?? "" }
            return identity + metrics + [row.attribution, row.displayedCostBasis] + eventMetrics
        }
        let text = ([header] + rows).map { $0.map(csvCell).joined(separator: ",") }.joined(separator: "\r\n") + "\r\n"
        save(Data(text.utf8), name: "grok-spend-\(category.rawValue.lowercased())\(demo ? "-demo" : "").csv", type: .commaSeparatedText)
    }

    private func save(_ data: Data, name: String, type: UTType) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = name
        panel.allowedContentTypes = [type]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try data.write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            exportNotice = "Exported \(url.lastPathComponent)"
        } catch { self.error = "The export could not be saved to the selected location." }
    }
}
