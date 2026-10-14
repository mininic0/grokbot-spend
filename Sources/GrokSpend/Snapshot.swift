import Foundation

struct SpendSnapshot: Decodable, Sendable {
    struct Account: Decodable, Sendable { let key: String; let label: String }
    struct Period: Decodable, Sendable { let start: String; let end: String }
    struct Weekly: Decodable, Sendable { let percentUsed: Double?; let resetsAt: String? }
    struct Totals: Decodable, Sendable {
        let usageCents: Double?; let includedCents: Double?; let onDemandCents: Double?; let todayCents: Double?
    }
    struct Reconciliation: Decodable, Sendable {
        let status: String; let referenceCents: Double?; let attributedCents: Double?
        let differenceCents: Double?; let coveragePercent: Double?; let label: String
    }
    struct Row: Decodable, Identifiable, Sendable {
        let id: String; let name: String; let usageCents: Double?; let includedCents: Double?
        let onDemandCents: Double?; let tokens: Double?; let runs: Double?
        let attribution: String; let detail: String
        let botId: String?; let routineId: String?
        let botName: String?
        let costBasis: String?
        let modelCostCents: Double?; let chargedCents: Double?
        let eventValueCents: Double?
        var isModelCost: Bool {
            if usageCents != nil { return costBasis == "model-cost" }
            return eventValueCents == nil && modelCostCents != nil
        }
        var isRecordedValue: Bool {
            if usageCents != nil { return costBasis == "recorded-usage-value" }
            return eventValueCents != nil
        }
        var rankedCostCents: Double? { usageCents ?? eventValueCents ?? modelCostCents }
        var displayedCostBasis: String {
            if isRecordedValue { return "recorded-usage-value" }
            if isModelCost { return "model-cost" }
            return "provider-aggregate"
        }
    }
    struct Day: Decodable, Identifiable, Sendable {
        let date: String; let usageCents: Double?; let onDemandCents: Double?
        let eventValueCents: Double?
        var id: String { date }
        var parsedDate: Date? { Dates.parse(date + "T12:00:00Z") }
    }
    struct Tokens: Decodable, Sendable {
        let input: Double?; let output: Double?; let cacheRead: Double?; let cacheWrite: Double?; let scope: String
    }
    struct Capabilities: Decodable, Sendable {
        let bots: Bool; let models: Bool; let sources: Bool; let routines: Bool; let events: Bool
    }
    struct Event: Decodable, Identifiable, Sendable {
        struct Attribution: Decodable, Sendable {
            let botId: String?; let routineId: String?; let routineName: String?
            let method: String?; let unassignedReason: String?
        }
        let eventId: String
        let accountKey: String
        let timestampISO: String?
        let model: String?
        let conversationId: String?
        let modelCostCents: Double?
        let chargedCents: Double?
        let totalTokens: Double?
        let attribution: Attribution?
        var id: String { eventId }
    }
    struct EventCoverage: Decodable, Sendable {
        let through: String
        let includedCount: Int
        let excludedCount: Int
        let unknownCount: Int
        let unassignedCount: Int?
        let unassignedValueCents: Double?
        let attributedValueCents: Double?
    }
    struct EventTotals: Decodable, Sendable {
        let modelCostCents: Double?
        let modelCostCentsMissingCount: Int
        let eventCount: Int
        let chargedCents: Double?
        let chargedCentsMissingCount: Int?
        let todayRecordedValueCents: Double?
    }
    let schemaVersion: Int
    let generatedAt: String
    let mode: String
    let account: Account
    let period: Period
    let weekly: Weekly
    let totals: Totals
    let reconciliation: Reconciliation
    let bots: [Row]; let models: [Row]; let sources: [Row]; let routines: [Row]
    let daily: [Day]
    let tokenBreakdown: Tokens
    let warnings: [String]
    let capabilities: Capabilities
    let lastError: String?
    let events: [Event]?
    let eventCoverage: EventCoverage?
    let eventTotals: EventTotals?

    var todayValueCents: Double? { totals.todayCents ?? eventTotals?.todayRecordedValueCents }
    var todayIsRecordedValue: Bool { totals.todayCents == nil && eventTotals?.todayRecordedValueCents != nil }
    var dailyUsesRecordedValue: Bool { !daily.contains { $0.usageCents != nil } }

    func events(for row: Row, category: SpendCategory) -> [Event] {
        (events ?? []).filter { event in
            guard event.accountKey == account.key else { return false }
            switch category {
            case .bots:
                if row.id == "unassigned-events" { return event.attribution?.botId == nil }
                return event.attribution?.botId == row.id
            case .models: return event.model == row.id
            case .routines:
                guard let botID = row.botId, let routineID = row.routineId else { return false }
                return event.attribution?.botId == botID && event.attribution?.routineId == routineID
            case .sources: return false
            }
        }.sorted { ($0.timestampISO ?? "") > ($1.timestampISO ?? "") }
    }

    func rows(for category: SpendCategory) -> [Row] {
        let values: [Row]
        switch category {
        case .bots: values = bots
        case .models: values = models
        case .sources: values = sources
        case .routines: values = routines
        }
        return values.sorted {
            let left = $0.rankedCostCents ?? -.infinity
            let right = $1.rankedCostCents ?? -.infinity
            if left != right { return left > right }
            let leftTokens = $0.tokens ?? -.infinity
            let rightTokens = $1.tokens ?? -.infinity
            if leftTokens != rightTokens { return leftTokens > rightTokens }
            return $0.id < $1.id
        }
    }

    func available(_ category: SpendCategory) -> Bool {
        switch category {
        case .bots: capabilities.bots
        case .models: capabilities.models
        case .sources: capabilities.sources
        case .routines: capabilities.routines
        }
    }
}

enum SpendCategory: String, CaseIterable, Identifiable {
    case bots = "Bots", models = "Models", sources = "Sources", routines = "Automations"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .bots: "sparkle"
        case .models: "cpu"
        case .sources: "square.stack.3d.up"
        case .routines: "arrow.triangle.2.circlepath"
        }
    }
}

enum Dates {
    static func parse(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions.insert(.withFractionalSeconds)
        return formatter.date(from: value)
    }

    static func period(_ period: SpendSnapshot.Period?) -> String {
        guard let period, let start = parse(period.start), let end = parse(period.end) else { return "Billing period unavailable" }
        let lastIncluded = end.addingTimeInterval(-1)
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "MMM d"
        let first = formatter.string(from: start)
        formatter.dateFormat = "MMM d, yyyy"
        return first + " – " + formatter.string(from: lastIncluded)
    }
}

func money(_ cents: Double?) -> String {
    guard let cents, cents.isFinite else { return "—" }
    return (cents / 100).formatted(.currency(code: "USD").locale(Locale(identifier: "en_US")))
}

func eventMoney(_ cents: Double?) -> String {
    guard let cents, cents.isFinite else { return "—" }
    if cents != 0 && abs(cents) < 0.01 { return cents > 0 ? "<$0.0001" : ">−$0.0001" }
    return (cents / 100).formatted(.currency(code: "USD").locale(Locale(identifier: "en_US")).precision(.fractionLength(2...4)))
}

func quantity(_ value: Double?) -> String {
    guard let value, value.isFinite else { return "—" }
    return value.formatted(.number.notation(.compactName).precision(.fractionLength(0...1)))
}
