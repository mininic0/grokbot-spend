#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
cat > "$test_dir/Check.swift" <<'SWIFT'
import Foundation

@main
struct Check {
    static func main() throws {
        let formula = " =SUM(1,2)"
        guard csvCell(formula) == "\"' =SUM(1,2)\"" else {
            print("FAIL: leading-whitespace CSV formula is not escaped")
            exit(1)
        }
        precondition(csvCell("\t+SUM(1,2)") == "\"'\t+SUM(1,2)\"")
        precondition(csvCell("\u{00A0}@SUM(1,2)") == "\"'\u{00A0}@SUM(1,2)\"")
        precondition(csvCell("-42.5") == "\"-42.5\"")
        precondition(csvCell("Bot \"A\"") == "\"Bot \"\"A\"\"\"")
        print("PASS: CSV formulas after spaces/tabs/Unicode whitespace escaped; numbers and quoting preserved")
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let decoder = JSONDecoder()
        let snapshot = try decoder.decode(SpendSnapshot.self, from: data)
        precondition(snapshot.mode == "demo")
        precondition(snapshot.events(for: snapshot.bots[0], category: .bots).count == 1)
        precondition(snapshot.events(for: snapshot.models[0], category: .models).count == 2)
        precondition(snapshot.events(for: snapshot.sources[0], category: .sources).isEmpty)
        precondition(snapshot.events?.first?.modelCostCents != snapshot.events?.first?.chargedCents)
        precondition(snapshot.events?[1].totalTokens == nil)
        precondition(money(nil) == "—" && quantity(nil) == "—")
        precondition(eventMoney(0.01) == "$0.0001" && eventMoney(0.001) == "<$0.0001")
        precondition(eventMoney(0) == "$0.00" && eventMoney(nil) == "—")
        var json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        json.removeValue(forKey: "events")
        let missingEvents = try decoder.decode(SpendSnapshot.self, from: JSONSerialization.data(withJSONObject: json))
        precondition(missingEvents.events == nil)
        precondition(missingEvents.events(for: missingEvents.bots[0], category: .bots).isEmpty)
        json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        var events = json["events"] as! [[String: Any]]
        events[0]["accountKey"] = "different-account"
        json["events"] = events
        let otherAccount = try decoder.decode(SpendSnapshot.self, from: JSONSerialization.data(withJSONObject: json))
        precondition(otherAccount.events(for: otherAccount.bots[0], category: .bots).isEmpty)
        print("PASS: synthetic snapshot decoding, exact bot/model matches, source no-match, missing events, account isolation, separate event costs, unavailable metrics")
        let unknown = try decoder.decode(SpendSnapshot.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])))
        precondition(unknown.totals.usageCents == nil)
        precondition(unknown.rows(for: .bots).allSatisfy { $0.rankedCostCents == nil })
        precondition(unknown.rows(for: .bots).map(\.tokens) == [5426000, 4123000, 1960000, 824000])
        precondition(money(unknown.totals.usageCents) == "—")
        let recordedData = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[3]))
        let recorded = try decoder.decode(SpendSnapshot.self, from: recordedData)
        precondition(recorded.totals.usageCents == nil && recorded.totals.onDemandCents == nil)
        precondition(recorded.rows(for: .bots).map(\.rankedCostCents) == [2350, 700, 600])
        precondition(recorded.rows(for: .bots).allSatisfy(\.isRecordedValue))
        let unassigned = recorded.bots.first { $0.id == "unassigned-events" }!
        precondition(recorded.events(for: unassigned, category: .bots).count == 2)
        precondition(recorded.routines[0].isModelCost && !recorded.routines[0].isRecordedValue)
        precondition(recorded.events(for: recorded.routines[0], category: .routines).count == 1)
        precondition(recorded.eventCoverage?.unknownCount == 2)
        precondition(recorded.eventTotals?.chargedCents == 3650 && recorded.eventTotals?.chargedCentsMissingCount == 0)
        precondition(recorded.eventTotals?.modelCostCentsMissingCount == 1)
        precondition(recorded.todayValueCents == 950 && recorded.todayIsRecordedValue)
        precondition(recorded.dailyUsesRecordedValue && recorded.daily.contains { $0.eventValueCents != nil })
        precondition(!snapshot.dailyUsesRecordedValue && !snapshot.todayIsRecordedValue)
        precondition(unknown.todayValueCents == nil)
        print("PASS: omitted aggregate dollars, token ranking, recorded-value fallback, explicit model basis, unassigned ownership, exact routine match, event coverage")
        let automationData = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[4]))
        let automationSnapshot = try decoder.decode(SpendSnapshot.self, from: automationData)
        let periodAnalysis = AutomationAnalysis(snapshot: automationSnapshot, window: .period)
        let weekAnalysis = AutomationAnalysis(snapshot: automationSnapshot, window: .week)
        let dayAnalysis = AutomationAnalysis(snapshot: automationSnapshot, window: .day)
        let routineID = "demo-research:a"
        let periodAutomation = periodAnalysis.summaries.first { $0.id == routineID }!
        let weekAutomation = weekAnalysis.summaries.first { $0.id == routineID }!
        let dayAutomation = dayAnalysis.summaries.first { $0.id == routineID }!
        precondition(periodAutomation.recordedValueCents == 1600 && periodAutomation.runs.count == 3)
        precondition(weekAutomation.recordedValueCents == 1200 && weekAutomation.runs.count == 2)
        precondition(dayAutomation.recordedValueCents == 1000 && dayAutomation.runs.count == 2)
        precondition(dayAutomation.runs.first { $0.id == "run-cross" }?.recordedValueCents == 300)
        precondition(dayAutomation.averageValueCents == 500)
        precondition(periodAnalysis.summaries.first { $0.id == "demo-build:a" }?.recordedValueCents == 120)
        precondition(dayAnalysis.summaries.first { $0.id == "demo-build:a" }?.recordedValueCents == 100)
        precondition(periodAnalysis.summaries.first { $0.id == "demo-research:d" }?.recordedValueCents == nil)
        precondition(periodAnalysis.unassignedEventCount == 1 && periodAnalysis.unassignedValueCents == 50)
        precondition(periodAnalysis.knownBotOtherEventCount == 1 && periodAnalysis.knownBotOtherValueCents == 80)
        precondition(periodAnalysis.unknownTimeCount == 1)
        precondition(AutomationSummary.sorted(periodAnalysis.summaries, by: .value).first?.id == routineID)
        precondition(AutomationSummary.sorted(periodAnalysis.summaries, by: .average).first?.id == "demo-research:b")
        precondition(AutomationSummary.sorted(periodAnalysis.summaries, by: .runs).first?.id == routineID)
        precondition(periodAutomation.lastActivity == Dates.parse("2026-10-13T17:00:00Z"))
        var noCutoffJSON = try JSONSerialization.jsonObject(with: automationData) as! [String: Any]
        noCutoffJSON.removeValue(forKey: "eventCoverage")
        let noCutoff = try decoder.decode(SpendSnapshot.self, from: JSONSerialization.data(withJSONObject: noCutoffJSON))
        precondition(!AutomationAnalysis(snapshot: noCutoff, window: .day).windowAvailable)
        print("PASS: automation recorded-value basis, exact owner/run grouping, cutoff-anchored 7d/24h windows, cross-window runs, missing values/timestamps, sorting and account isolation")
        var large = try JSONSerialization.jsonObject(with: recordedData) as! [String: Any]
        let prototype = (large["events"] as! [[String: Any]])[0]
        large["events"] = (0..<37_469).map { index -> [String: Any] in
            var event = prototype
            event["eventId"] = "synthetic-scale-\(index)"
            return event
        }
        let start = Date()
        let scaled = try decoder.decode(SpendSnapshot.self, from: JSONSerialization.data(withJSONObject: large))
        precondition(scaled.events(for: recorded.bots.first { $0.id == "demo-research" }!, category: .bots).count == 37_469)
        print("PASS: 37,469 synthetic events decoded and matched in \(String(format: "%.2f", Date().timeIntervalSince(start)))s; native view renders 200 at a time")
    }
}
SWIFT
swiftc -swift-version 6 "$project_root/Sources/GrokSpend/Snapshot.swift" "$project_root/Sources/GrokSpend/AutomationAnalysis.swift" "$project_root/Sources/GrokSpend/CSV.swift" "$test_dir/Check.swift" -o "$test_dir/check"
"$test_dir/check" "$project_root/assets/demo-snapshot.json" "$project_root/assets/demo-unavailable-snapshot.json" "$project_root/assets/demo-recorded-snapshot.json" "$project_root/assets/demo-automations-snapshot.json"
