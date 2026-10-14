import Foundation

enum AutomationWindow: String, CaseIterable, Identifiable, Sendable {
    case period = "Billing period", week = "Last 7 days", day = "Last 24 hours"
    var id: String { rawValue }
    var seconds: TimeInterval? {
        switch self { case .period: nil; case .week: 7 * 86400; case .day: 86400 }
    }
}

enum AutomationSort: String, CaseIterable, Identifiable {
    case value = "Recorded value", average = "Average per matched run", runs = "Matched active runs", recent = "Last activity"
    var id: String { rawValue }
}

struct AutomationRun: Identifiable, Sendable {
    let id: String
    let events: [SpendSnapshot.Event]
    let firstActivity: Date?
    let lastActivity: Date?
    let recordedValueCents: Double?
    var models: [String] { Set(events.compactMap(\.model)).sorted() }
}

struct AutomationSummary: Identifiable, Sendable {
    let row: SpendSnapshot.Row
    let runs: [AutomationRun]
    let recordedValueCents: Double?
    let firstActivity: Date?
    let lastActivity: Date?
    let missingValueCount: Int
    let missingTimeCount: Int
    var id: String { row.id }
    var botName: String { row.botName ?? row.botId ?? "Owner unavailable" }
    var eventCount: Int { runs.reduce(0) { $0 + $1.events.count } }
    var averageValueCents: Double? {
        guard !runs.isEmpty, let recordedValueCents else { return nil }
        return recordedValueCents / Double(runs.count)
    }
    static func sorted(_ rows: [Self], by sort: AutomationSort) -> [Self] {
        rows.sorted { left, right in
            let lhs: Double, rhs: Double
            switch sort {
            case .value: lhs = left.recordedValueCents ?? -.infinity; rhs = right.recordedValueCents ?? -.infinity
            case .average: lhs = left.averageValueCents ?? -.infinity; rhs = right.averageValueCents ?? -.infinity
            case .runs: lhs = Double(left.runs.count); rhs = Double(right.runs.count)
            case .recent: lhs = left.lastActivity?.timeIntervalSince1970 ?? -.infinity; rhs = right.lastActivity?.timeIntervalSince1970 ?? -.infinity
            }
            return lhs == rhs ? left.id < right.id : lhs > rhs
        }
    }
}

struct AutomationAnalysis: Sendable {
    let summaries: [AutomationSummary]
    let window: AutomationWindow
    let start: Date?
    let through: Date?
    let windowAvailable: Bool
    let wholeValueCents: Double?
    let namedValueCents: Double?
    let unassignedValueCents: Double?
    let unassignedEventCount: Int
    let knownBotOtherValueCents: Double?
    let knownBotOtherEventCount: Int
    let unknownTimeCount: Int

    init(snapshot: SpendSnapshot, window: AutomationWindow) {
        self.window = window
        // Reuse parsers across the event feed, rather than constructing one for
        // each record. This work runs once per snapshot/window, off the UI thread.
        let ordinary = ISO8601DateFormatter()
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions.insert(.withFractionalSeconds)
        func date(_ raw: String?) -> Date? {
            guard let raw else { return nil }
            return fractional.date(from: raw) ?? ordinary.date(from: raw)
        }
        let cutoff = date(snapshot.eventCoverage?.through)
        let periodStart = date(snapshot.period.start), periodEnd = date(snapshot.period.end)
        let start = window.seconds.flatMap { seconds in cutoff.map { max($0.addingTimeInterval(-seconds), periodStart ?? .distantPast) } } ?? periodStart
        let through = cutoff.map { min($0, periodEnd ?? .distantFuture) }
        self.start = start
        self.through = through
        windowAvailable = window == .period || (cutoff != nil && periodStart != nil)

        struct Owner: Hashable { let bot: String; let routine: String }
        struct TimedEvent { let event: SpendSnapshot.Event; let time: Date? }
        var groups: [Owner: [TimedEvent]] = [:]
        var included: [SpendSnapshot.Event] = [], unassigned: [SpendSnapshot.Event] = [], knownBotOther: [SpendSnapshot.Event] = []
        var unknownTimes = 0
        for event in snapshot.events ?? [] where event.accountKey == snapshot.account.key {
            let time = date(event.timestampISO)
            if time == nil { unknownTimes += 1 }
            if !windowAvailable { continue }
            if let time {
                if let start, time < start { continue }
                if let through, time > through { continue }
                if let periodEnd, time >= periodEnd { continue }
            } else if window != .period { continue }
            included.append(event)
            if event.attribution?.botId == nil { unassigned.append(event) }
            guard event.attribution?.method == "routine-run-id",
                  let bot = event.attribution?.botId, !bot.isEmpty,
                  let routine = event.attribution?.routineId, !routine.isEmpty,
                  let conversation = event.conversationId, !conversation.isEmpty else {
                if event.attribution?.botId != nil { knownBotOther.append(event) }
                continue
            }
            groups[Owner(bot: bot, routine: routine), default: []].append(TimedEvent(event: event, time: time))
        }
        var summaries: [AutomationSummary] = []
        for row in snapshot.routines {
            guard let bot = row.botId, let routine = row.routineId else { continue }
            let matched = groups[Owner(bot: bot, routine: routine)] ?? []
            let groupedRuns = Dictionary(grouping: matched) { $0.event.conversationId! }
            let runs = groupedRuns.map { id, entries in
                let times = entries.compactMap(\.time)
                return AutomationRun(id: id, events: entries.map(\.event).sorted { ($0.timestampISO ?? "") > ($1.timestampISO ?? "") }, firstActivity: times.min(), lastActivity: times.max(), recordedValueCents: completeValue(entries.map(\.event)))
            }.sorted { ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast) }
            let times = matched.compactMap(\.time)
            summaries.append(AutomationSummary(row: row, runs: runs, recordedValueCents: completeValue(matched.map(\.event)), firstActivity: times.min(), lastActivity: times.max(), missingValueCount: matched.filter { $0.event.chargedCents == nil }.count, missingTimeCount: matched.filter { $0.time == nil }.count))
        }
        self.summaries = AutomationSummary.sorted(summaries, by: .value)
        wholeValueCents = completeValue(included)
        // Named automation value is a subset of whole Grok Bot usage. It never
        // absorbs anonymous children, direct bot activity, or another account.
        namedValueCents = completeValue(groups.values.flatMap { $0.map(\.event) })
        unassignedValueCents = completeValue(unassigned)
        unassignedEventCount = unassigned.count
        knownBotOtherValueCents = completeValue(knownBotOther)
        knownBotOtherEventCount = knownBotOther.count
        unknownTimeCount = unknownTimes
    }
}

private func completeValue(_ events: [SpendSnapshot.Event]) -> Double? {
    guard !events.isEmpty else { return nil }
    let values = events.compactMap(\.chargedCents)
    guard values.count == events.count, values.allSatisfy(\.isFinite) else { return nil }
    return values.reduce(0, +)
}
