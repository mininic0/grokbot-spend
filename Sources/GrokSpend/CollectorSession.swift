import Darwin
import Foundation

struct CollectionResult: Decodable, Sendable {
    let status: Int32
    let detail: String?
}

private final class BoundedErrorOutput: @unchecked Sendable {
    private let lock = NSLock()
    private var bytes = Data()
    func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        bytes.append(data.prefix(max(0, 2048 - bytes.count)))
    }
    var message: String? {
        lock.lock()
        defer { lock.unlock() }
        let text = String(decoding: bytes, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }
}

/// A private, app-lifetime transport. Only safe status metadata crosses these
/// pipes; the Python worker owns its session token in RAM. One request at a time.
final class CollectorSession: @unchecked Sendable {
    private let process: Process
    private let input: FileHandle
    private let output: FileHandle
    private let errors = BoundedErrorOutput()
    private let stateLock = NSLock()
    private var stopped = false
    private var requestInProgress = false

    init(script: String, helper: String, snapshot: String) throws {
        let process = Process()
        let inputPipe = Pipe(), outputPipe = Pipe(), errorPipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", script, "--output", snapshot, "--auth-helper", helper]
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        environment["GROK_SPEND_AUTH_HELPER"] = helper
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardInput = inputPipe
        process.standardOutput = outputPipe
        process.standardError = errorPipe
        self.process = process
        input = inputPipe.fileHandleForWriting
        output = outputPipe.fileHandleForReading
        try process.run()
        let errors = self.errors
        DispatchQueue.global(qos: .utility).async {
            while let data = try? errorPipe.fileHandleForReading.read(upToCount: 4096), !data.isEmpty {
                errors.append(data)
            }
        }
    }

    var isRunning: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return !stopped && process.isRunning
    }

    func refresh(allowPrompt: Bool, timeout: TimeInterval = 300) -> CollectionResult {
        stateLock.lock()
        guard !stopped && process.isRunning else {
            stateLock.unlock()
            return CollectionResult(status: 1, detail: errors.message ?? "The local collector session has stopped.")
        }
        guard !requestInProgress else {
            stateLock.unlock()
            return CollectionResult(status: 1, detail: "A refresh is already running.")
        }
        requestInProgress = true
        do {
            var request = try JSONSerialization.data(withJSONObject: ["command": "refresh", "allowPrompt": allowPrompt])
            request.append(0x0A)
            try input.write(contentsOf: request)
            stateLock.unlock()
        } catch {
            requestInProgress = false
            stateLock.unlock()
            stop()
            return CollectionResult(status: 1, detail: "Could not communicate with the local collector session.")
        }
        defer {
            stateLock.lock()
            requestInProgress = false
            stateLock.unlock()
        }
        switch readReply(deadline: Date().addingTimeInterval(timeout)) {
        case .success(let data):
            guard let result = try? JSONDecoder().decode(CollectionResult.self, from: data), [0, 1, 2].contains(result.status) else {
                stop()
                return CollectionResult(status: 1, detail: "The collector returned an invalid status message.")
            }
            return CollectionResult(status: result.status, detail: result.detail.map { String($0.prefix(2048)) })
        case .failure(let failure):
            stop()
            return CollectionResult(status: failure == .timeout ? 124 : 1, detail: failure == .timeout
                ? "Refresh exceeded five minutes and the session was stopped."
                : errors.message ?? "The local collector session ended without a valid response.")
        }
    }

    private enum ReplyFailure: Error { case timeout, closed, oversized }

    private func readReply(deadline: Date) -> Result<Data, ReplyFailure> {
        var data = Data()
        while data.count < 8192 {
            let remaining = deadline.timeIntervalSinceNow
            guard remaining > 0 else { return .failure(.timeout) }
            var descriptor = pollfd(fd: output.fileDescriptor, events: Int16(POLLIN | POLLHUP), revents: 0)
            let ready = poll(&descriptor, 1, Int32(max(1, min(remaining * 1000, 1000))))
            if ready < 0 {
                if errno == EINTR { continue }
                return .failure(.closed)
            }
            if ready == 0 { continue }
            if descriptor.revents & Int16(POLLIN | POLLHUP) == 0 { return .failure(.closed) }
            var byte: UInt8 = 0
            let count = Darwin.read(output.fileDescriptor, &byte, 1)
            if count < 0 && errno == EINTR { continue }
            guard count == 1 else { return .failure(.closed) }
            if byte == 0x0A { return .success(data) }
            data.append(byte)
        }
        return .failure(.oversized)
    }

    /// Safe to call while a request is waiting. The read handle remains alive
    /// until that request observes EOF, preventing descriptor-reuse races.
    func stop() {
        stateLock.lock()
        guard !stopped else { stateLock.unlock(); return }
        stopped = true
        try? input.close()
        stateLock.unlock()
        guard process.isRunning else { return }
        process.terminate()
        let process = self.process
        DispatchQueue.global(qos: .utility).async {
            let deadline = Date().addingTimeInterval(2)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            process.waitUntilExit()
        }
    }
}
