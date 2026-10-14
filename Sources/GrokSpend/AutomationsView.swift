import SwiftUI

struct AutomationsView: View {
    let snapshot: SpendSnapshot
    @State private var window: AutomationWindow = .day
    @State private var sort: AutomationSort = .value
    @State private var search = ""
    @State private var analysis: AutomationAnalysis?
    @State private var selection: AutomationSummary?

    private var rows: [AutomationSummary] {
        AutomationSummary.sorted((analysis?.summaries ?? []).filter {
            search.isEmpty || $0.row.name.localizedCaseInsensitiveContains(search) || $0.botName.localizedCaseInsensitiveContains(search)
        }, by: sort)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 7) {
                    Text("Which automations use the most?").font(.system(size: 21, weight: .semibold))
                    Text("Compare recorded value and matched activity. Use the automation and bot names to find it in Grok Bot.")
                        .font(.system(size: 12)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 15)
            }
            Picker("Activity window", selection: $window) {
                ForEach(AutomationWindow.allCases) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 420).accessibilityLabel("Automation activity window")
            if let analysis {
                coverage(analysis)
                HStack(spacing: 18) {
                    HStack(spacing: 8) {
                        Image(systemName: "magnifyingglass").foregroundStyle(Palette.muted)
                        TextField("Find automation or bot", text: $search).textFieldStyle(.plain)
                    }.padding(10).background(Color.black.opacity(0.2), in: RoundedRectangle(cornerRadius: 7)).frame(maxWidth: 360)
                    Spacer()
                    Text("Sort by").font(.system(size: 11)).foregroundStyle(Palette.muted)
                    Picker("Sort automations", selection: $sort) {
                        ForEach(AutomationSort.allCases) { Text($0.rawValue).tag($0) }
                    }.labelsHidden().frame(width: 210)
                }
                tableHeader
                if !analysis.windowAvailable {
                    empty("Activity window unavailable", "The snapshot has no valid event cutoff. Choose Billing period or refresh to get a timestamped snapshot.")
                } else if rows.isEmpty {
                    empty(search.isEmpty ? "No exact automation matches" : "No matching automation names", search.isEmpty ? "Recorded activity cannot yet be linked to named automations. Unassigned usage remains visible above; this does not mean automation usage was zero." : "Search by either automation name or bot owner.")
                } else {
                    LazyVStack(spacing: 0) {
                        ForEach(rows) { summary in
                            Button { selection = summary } label: { automationRow(summary) }.buttonStyle(.plain)
                                .help("Inspect exact matched runs for \(summary.row.name)")
                            Rectangle().fill(Palette.line).frame(height: 1)
                        }
                    }
                }
                Text("Matched active runs are distinct run IDs with activity in this window, not scheduled frequency or run starts. Values cover only the matching activity; averages are not a prediction of future charges.")
                    .font(.system(size: 11)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            } else {
                ProgressView("Preparing automation activity…").frame(maxWidth: .infinity, minHeight: 150)
            }
        }
        .padding(24).background(Palette.panel, in: RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(Palette.line, lineWidth: 1))
        .task(id: snapshot.generatedAt + ":" + window.id) {
            analysis = nil
            let chosen = window
            let result = await Task.detached(priority: .userInitiated) { AutomationAnalysis(snapshot: snapshot, window: chosen) }.value
            guard !Task.isCancelled else { return }
            analysis = result
        }
        .sheet(item: $selection) { summary in
            AutomationDetailView(summary: summary, window: window, through: analysis?.through, demo: snapshot.mode == "demo")
        }
    }

    private func coverage(_ analysis: AutomationAnalysis) -> some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .top, spacing: 24) {
                coverageMetric("NAMED AUTOMATIONS", analysis.namedValueCents, caption: "Observed subset of \(money(analysis.wholeValueCents)) Grok usage")
                coverageMetric("PARENT STILL UNKNOWN", analysis.unassignedValueCents, caption: "\(analysis.unassignedEventCount.formatted()) events · not assigned to a named automation")
                coverageMetric("BOT KNOWN · AUTOMATION UNPROVEN", analysis.knownBotOtherValueCents, caption: "\(analysis.knownBotOtherEventCount.formatted()) events · no exact automation link")
            }
            Label("Historical automation totals are partial", systemImage: "circle.dashed")
                .font(.system(size: 12, weight: .semibold)).foregroundStyle(Palette.amber)
            Text("Grok Bot exposes a limited recent run history. Older child runs can remain unassigned; known bot activity can also be automation-driven without an exact link. Named automation values are observed subsets, and may understate historical activity.")
                .font(.system(size: 11)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            Text("Matched activity window: \(activityDate(analysis.start)) → \(activityDate(analysis.through)) · local display")
                .font(.system(size: 10)).foregroundStyle(Palette.muted)
            if analysis.unknownTimeCount > 0 {
                Text("\(analysis.unknownTimeCount.formatted()) account events lack timestamps; recent windows exclude them.")
                    .font(.system(size: 10)).foregroundStyle(Palette.amber)
            }
            Text("Recorded usage value includes plan-covered consumption. It is not extra cash charged or guaranteed savings from turning an automation off.")
                .font(.system(size: 10)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
        }.padding(17).background(Color.black.opacity(0.15), in: RoundedRectangle(cornerRadius: 9))
    }

    private func coverageMetric(_ title: String, _ value: Double?, caption: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 9, weight: .semibold)).tracking(1).foregroundStyle(Palette.muted)
            Text(money(value)).font(.system(size: 25, weight: .medium, design: .rounded)).monospacedDigit()
            Text(caption).font(.system(size: 10)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private var tableHeader: some View {
        HStack(spacing: 12) {
            Text("AUTOMATION").frame(maxWidth: .infinity, alignment: .leading)
            Text("BOT OWNER").frame(width: 140, alignment: .leading)
            sortHeader("RECORDED VALUE", .value, width: 105)
            sortHeader("AVG / MATCHED RUN", .average, width: 105)
            sortHeader("MATCHED RUNS", .runs, width: 82)
            sortHeader("LAST ACTIVITY", .recent, width: 110)
        }.font(.system(size: 9, weight: .semibold)).foregroundStyle(Palette.muted)
    }

    private func sortHeader(_ text: String, _ choice: AutomationSort, width: CGFloat) -> some View {
        Button { sort = choice } label: {
            HStack(spacing: 3) {
                Text(text).multilineTextAlignment(.trailing)
                if sort == choice { Image(systemName: "arrow.down").font(.system(size: 8)) }
            }.frame(width: width, alignment: .trailing).foregroundStyle(sort == choice ? Palette.mint : Palette.muted)
        }.buttonStyle(.plain).help("Sort descending by \(choice.rawValue.lowercased())")
    }

    private func automationRow(_ summary: AutomationSummary) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                Text(summary.row.name).font(.system(size: 13, weight: .medium)).lineLimit(2)
                Text(summary.runs.isEmpty ? "No matched activity in window" : "\(summary.eventCount.formatted()) exact matched events")
                    .font(.system(size: 10)).foregroundStyle(Palette.muted)
            }.frame(maxWidth: .infinity, alignment: .leading)
            Text(summary.botName).font(.system(size: 11)).foregroundStyle(Palette.muted).lineLimit(2).frame(width: 140, alignment: .leading)
            Text(money(summary.recordedValueCents)).foregroundStyle(Palette.mint).frame(width: 105, alignment: .trailing)
            Text(eventMoney(summary.averageValueCents)).frame(width: 105, alignment: .trailing)
            Text(summary.runs.count.formatted()).frame(width: 82, alignment: .trailing)
            Text(activityDate(summary.lastActivity)).font(.system(size: 10)).foregroundStyle(Palette.muted).frame(width: 110, alignment: .trailing)
        }.font(.system(size: 13, weight: .medium, design: .rounded)).monospacedDigit()
            .padding(.vertical, 15).contentShape(Rectangle())
    }

    private func empty(_ title: String, _ detail: String) -> some View {
        VStack(spacing: 12) {
            Text(title).font(.system(size: 16, weight: .medium))
            Text(detail).font(.system(size: 12)).foregroundStyle(Palette.muted).multilineTextAlignment(.center).frame(maxWidth: 550)
        }.frame(maxWidth: .infinity, minHeight: 180)
    }
}

func activityDate(_ date: Date?) -> String {
    date?.formatted(.dateTime.month(.abbreviated).day().hour().minute()) ?? "Unavailable"
}
