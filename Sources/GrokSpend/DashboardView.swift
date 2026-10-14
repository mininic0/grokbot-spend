import Charts
import SwiftUI

enum Palette {
    static let background = Color(red: 0.061, green: 0.077, blue: 0.087)
    static let panel = Color(red: 0.087, green: 0.106, blue: 0.118)
    static let line = Color.white.opacity(0.085)
    static let mint = Color(red: 0.55, green: 0.89, blue: 0.72)
    static let amber = Color(red: 0.97, green: 0.76, blue: 0.44)
    static let muted = Color(red: 0.53, green: 0.59, blue: 0.60)
}

struct DashboardView: View {
    @ObservedObject var store: SpendStore
    @State private var category: SpendCategory = .routines
    @State private var selectedRow: RowSelection?

    private var snapshot: SpendSnapshot? { store.snapshot }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    if store.demo { banner("You’re viewing synthetic demo data. No account connection or live costs.", icon: "sparkles", color: Palette.amber) }
                    if store.isFilePreview { banner("Snapshot preview · Refresh reloads this file without contacting the provider.", icon: "doc.text", color: Palette.mint) }
                    if let error = store.error { banner(error, icon: "exclamationmark.triangle", color: Palette.amber) }
                    if let notice = store.exportNotice { banner(notice, icon: "checkmark.circle", color: Palette.mint) }
                    if snapshot == nil { welcome }
                    totals
                    categoryNavigation
                    if category == .routines, let snapshot {
                        AutomationsView(snapshot: snapshot)
                    } else {
                        HStack(alignment: .top, spacing: 18) {
                            breakdown.frame(maxWidth: .infinity)
                            VStack(spacing: 18) {
                                dailyTrend
                                weeklyAllowance
                                reconciliation
                            }.frame(width: 300)
                        }
                    }
                    tokenSummary
                    if let coverage = snapshot?.eventCoverage { EventCoverageView(coverage: coverage) }
                    footer
                }
                .padding(.horizontal, 34)
                .padding(.top, 34)
                .padding(.bottom, 26)
                .frame(maxWidth: 1440)
            }
        }
        .background(Palette.background)
        .frame(minWidth: 1030, minHeight: 760)
        .tint(Palette.mint)
        .sheet(item: $selectedRow) { selection in
            if let snapshot { EventDetailView(snapshot: snapshot, selection: selection) }
        }
    }

    private var sidebar: some View {
        VStack(spacing: 25) {
            ZStack {
                RoundedRectangle(cornerRadius: 12).fill(Palette.mint)
                Image(systemName: "chart.bar.xaxis").font(.system(size: 20, weight: .bold)).foregroundStyle(Palette.background)
            }.frame(width: 42, height: 42).padding(.top, 47).accessibilityLabel("Grok Spend")
            Rectangle().fill(Palette.line).frame(width: 26, height: 1)
            Image(systemName: "square.grid.2x2.fill").font(.system(size: 18)).foregroundStyle(Palette.mint)
                .frame(width: 44, height: 44).background(Palette.mint.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                .help("Spending overview")
            Spacer()
            Image(systemName: "lock.shield").font(.system(size: 19)).foregroundStyle(Palette.muted).help("Your snapshot stays on this Mac")
            Text("LOCAL").font(.system(size: 8, weight: .bold)).tracking(1.3).foregroundStyle(Palette.muted)
        }
        .padding(.bottom, 26)
        .frame(width: 80)
        .background(Color.black.opacity(0.15))
        .overlay(alignment: .trailing) { Rectangle().fill(Palette.line).frame(width: 1) }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 9) {
                    Text("GROK SPEND").font(.system(size: 10, weight: .bold)).tracking(2.3).foregroundStyle(Palette.mint)
                    Rectangle().fill(Palette.muted.opacity(0.4)).frame(width: 1, height: 10)
                    Text("YOUR USAGE, EXPLAINED").font(.system(size: 10, weight: .medium)).tracking(1.6).foregroundStyle(Palette.muted)
                }
                Text("Where your usage goes.").font(.system(size: 29, weight: .semibold)).tracking(-0.7)
                HStack(spacing: 8) {
                    Image(systemName: "calendar").font(.system(size: 11))
                    Text(Dates.period(snapshot?.period)).font(.system(size: 12))
                    Text("·  USD · UTC dates").font(.system(size: 11, weight: .medium))
                }.foregroundStyle(Palette.muted)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 10) {
                HStack(spacing: 9) {
                    Menu {
                        Button("Snapshot as JSON…") { store.exportJSON() }.disabled(snapshot == nil)
                        Button("\(category.rawValue) as CSV…") { store.exportCSV(category: category) }.disabled(snapshot == nil)
                        Divider()
                        Button("Disconnect session") { store.disconnect() }.disabled(!store.hasSession)
                    } label: { Label("Export", systemImage: "square.and.arrow.up") }
                    .menuStyle(.borderlessButton).fixedSize()
                    .padding(.horizontal, 12).padding(.vertical, 8).background(Palette.panel, in: RoundedRectangle(cornerRadius: 7))
                    Button { store.refresh() } label: {
                        HStack(spacing: 6) {
                            if store.isRefreshing { ProgressView().controlSize(.mini) }
                            else { Image(systemName: "arrow.clockwise") }
                            Text(store.isRefreshing ? "Refreshing" : "Refresh")
                        }.font(.system(size: 12, weight: .semibold)).padding(.horizontal, 4).padding(.vertical, 3)
                    }.buttonStyle(.plain).padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Palette.mint, in: RoundedRectangle(cornerRadius: 7))
                        .foregroundStyle(Palette.background).disabled(store.isRefreshing).opacity(store.isRefreshing ? 0.65 : 1)
                }
                HStack(spacing: 5) {
                    Circle().fill(store.error == nil ? Palette.mint : Palette.amber).frame(width: 5, height: 5)
                    Text(store.statusText).font(.system(size: 10))
                }.foregroundStyle(Palette.muted)
            }
        }
    }

    private var welcome: some View {
        HStack(spacing: 16) {
            Image(systemName: "chart.xyaxis.line").font(.system(size: 28)).foregroundStyle(Palette.mint)
            VStack(alignment: .leading, spacing: 5) {
                Text("Start with your real billing data").font(.system(size: 16, weight: .semibold))
                Text("Refresh reads usage using your existing Grok Bot session. macOS may ask to allow access to its saved sign-in.")
                    .font(.system(size: 12)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }.padding(22).card()
    }

    private var totals: some View {
        HStack(spacing: 16) {
            if let events = snapshot?.eventTotals {
                MetricCard(title: events.chargedCents != nil ? "RECORDED USAGE VALUE" : "MODEL USAGE VALUE", value: events.chargedCents ?? events.modelCostCents, caption: eventValueCaption(events), icon: "cpu", accent: Palette.mint, featured: true, loaded: true)
            }
            MetricCard(title: "REPORTED USAGE COST", value: snapshot?.totals.usageCents, caption: "Provider-reported · this period", icon: "chart.bar", accent: Palette.mint, featured: snapshot?.eventTotals == nil, loaded: snapshot != nil)
            MetricCard(title: "INCLUDED USAGE", value: snapshot?.totals.includedCents, caption: "Usage covered by your allowance", icon: "checkmark.seal", accent: Palette.mint, loaded: snapshot != nil)
            MetricCard(title: "ON-DEMAND USAGE", value: snapshot?.totals.onDemandCents, caption: "Usage outside the included allowance", icon: "bolt", accent: Palette.amber, loaded: snapshot != nil)
        }
    }

    private var categoryNavigation: some View {
            HStack(spacing: 5) {
                ForEach([SpendCategory.routines, .bots, .models, .sources]) { item in
                    Button { category = item } label: {
                        HStack(spacing: 6) {
                            Image(systemName: item.icon).font(.system(size: 11))
                            Text(item.rawValue).font(.system(size: 12, weight: .medium))
                        }
                        .foregroundStyle(category == item ? Palette.mint : Palette.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 9)
                        .background(category == item ? Palette.mint.opacity(0.10) : Color.clear, in: RoundedRectangle(cornerRadius: 7))
                    }.buttonStyle(.plain)
                }
            }.padding(5).background(Color.black.opacity(0.16), in: RoundedRectangle(cornerRadius: 10))
    }

    private var breakdown: some View {
        let rows = snapshot?.rows(for: category) ?? []
        let rankByTokens = !rows.isEmpty && rows.allSatisfy { $0.rankedCostCents == nil }
        let modelCostView = !rows.isEmpty && rows.allSatisfy(\.isModelCost)
        let recordedValueView = !rows.isEmpty && rows.allSatisfy(\.isRecordedValue)
        let maximum = rankByTokens ? rows.compactMap(\.tokens).max() ?? 0 : rows.compactMap(\.rankedCostCents).max() ?? 0
        return VStack(alignment: .leading, spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 6) {
                    Text("The breakdown").font(.system(size: 17, weight: .semibold))
                    Text(rankByTokens ? "Token usage · dollar amounts unavailable" : recordedValueView ? "Recorded usage value · not cash charges" : modelCostView ? "Model usage value · not cash charges" : "Follow the cost, one view at a time.")
                        .font(.system(size: 11)).foregroundStyle(Palette.muted)
                }
                Spacer()
                Image(systemName: "line.3.horizontal.decrease").foregroundStyle(Palette.muted)
            }.padding(22)
            HStack {
                Text(category.rawValue.uppercased()).tracking(1.2)
                Spacer()
                Text(rankByTokens ? "TOKENS" : recordedValueView ? "RECORDED VALUE" : modelCostView ? "MODEL USAGE VALUE" : "USAGE COST").tracking(1.2)
            }.font(.system(size: 9, weight: .semibold)).foregroundStyle(Palette.muted).padding(.horizontal, 22).padding(.top, 24).padding(.bottom, 4)
            if rows.isEmpty {
                VStack(spacing: 13) {
                    Image(systemName: category.icon).font(.system(size: 26)).foregroundStyle(Palette.muted)
                    Text(emptyTitle).font(.system(size: 14, weight: .medium))
                    Text(emptyDetail).font(.system(size: 12)).foregroundStyle(Palette.muted).multilineTextAlignment(.center).frame(maxWidth: 330)
                }.frame(maxWidth: .infinity, minHeight: 245).padding(.horizontal, 22)
            } else {
                ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                    Button { selectedRow = RowSelection(row: row, category: category) } label: {
                        SpendRowView(row: row, rank: index + 1, maximum: maximum, category: category, rankByTokens: rankByTokens)
                            .contentShape(Rectangle())
                    }.buttonStyle(.plain).help("Inspect matching usage events for \(row.name)")
                    if index < rows.count - 1 { Rectangle().fill(Palette.line).frame(height: 1).padding(.horizontal, 22) }
                }
            }
            HStack(spacing: 7) {
                Image(systemName: "info.circle").font(.system(size: 10))
                Text("Views overlap. Bot, model, and source totals are not added together.").font(.system(size: 10))
            }.foregroundStyle(Palette.muted).padding(20).frame(maxWidth: .infinity, alignment: .leading).background(Color.black.opacity(0.10))
        }.card()
    }

    private var emptyTitle: String {
        if snapshot == nil { return "Your breakdown will appear here" }
        if snapshot?.available(category) == false { return "\(category.rawValue) attribution unavailable" }
        return "No \(category.rawValue.lowercased()) reported for this period"
    }
    private var emptyDetail: String {
        if snapshot == nil { return "Refresh to load the provider’s usage records." }
        if category == .routines { return "A routine needs an exact billing identifier to show a cost. We don’t assign costs by guessing when it ran." }
        return "Missing data is left unavailable. It does not mean these costs are zero."
    }

    private var dailyTrend: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Daily usage").font(.system(size: 14, weight: .semibold))
                Spacer()
                Text("USD").font(.system(size: 9, weight: .medium)).foregroundStyle(Palette.muted)
            }
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Text(money(store.todayCents)).font(.system(size: 27, weight: .semibold, design: .rounded)).monospacedDigit()
                Text("today · UTC").font(.system(size: 11)).foregroundStyle(Palette.muted)
            }
            Text(store.todayCaption).font(.system(size: 10)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            let recorded = snapshot?.dailyUsesRecordedValue == true
            let days = (snapshot?.daily ?? []).filter { $0.parsedDate != nil && (recorded ? $0.eventValueCents : $0.usageCents) != nil }.sorted { $0.date < $1.date }
            if days.isEmpty {
                Text("Daily costs unavailable").font(.system(size: 12)).foregroundStyle(Palette.muted).frame(maxWidth: .infinity, minHeight: 95)
            } else {
                Chart(days) { day in
                    if let date = day.parsedDate, let cents = recorded ? day.eventValueCents : day.usageCents {
                        BarMark(x: .value("Date", date, unit: .day), y: .value(recorded ? "Recorded usage value (USD)" : "Usage cost (USD)", cents / 100))
                            .foregroundStyle(Palette.mint.opacity(day.date == days.last?.date ? 1 : 0.38))
                            .cornerRadius(2)
                    }
                }
                .chartXAxis { AxisMarks(values: .automatic(desiredCount: 3)) { value in
                    AxisValueLabel(format: .dateTime.day()).foregroundStyle(Palette.muted)
                } }
                .chartYAxis { AxisMarks(position: .leading, values: .automatic(desiredCount: 3)) { value in
                    AxisGridLine().foregroundStyle(Palette.line)
                    AxisValueLabel().foregroundStyle(Palette.muted)
                } }
                .frame(height: 106)
                .environment(\.timeZone, TimeZone(secondsFromGMT: 0)!)
                .accessibilityLabel(recorded ? "Daily recorded usage value in US dollars, not cash charges" : "Daily usage costs in US dollars")
                Text(recorded ? "Recorded usage value (USD), not cash charges" : "Provider-reported daily usage cost (USD)")
                    .font(.system(size: 10)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            }
        }.padding(20).card()
    }

    private var weeklyAllowance: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Weekly allowance").font(.system(size: 13, weight: .semibold))
                Spacer()
                Text(snapshot?.weekly.percentUsed.map { "\($0.formatted(.number.precision(.fractionLength(0))))%" } ?? "—")
                    .font(.system(size: 12, weight: .medium, design: .monospaced)).foregroundStyle(Palette.mint)
            }
            if let percent = snapshot?.weekly.percentUsed {
                GeometryReader { geometry in
                    Capsule().fill(Palette.line)
                    Capsule().fill(Palette.mint).frame(width: geometry.size.width * min(max(percent / 100, 0), 1))
                }.frame(height: 5)
                if let raw = snapshot?.weekly.resetsAt, let date = Dates.parse(raw) {
                    Text("Resets \(date.formatted(.dateTime.weekday(.wide).hour().minute()))").font(.system(size: 10)).foregroundStyle(Palette.muted)
                }
            } else {
                Text("Allowance status unavailable").font(.system(size: 11)).foregroundStyle(Palette.muted)
            }
        }.padding(20).card()
    }

    private var reconciliation: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Text("Can we account for it?").font(.system(size: 13, weight: .semibold))
                Spacer()
                Image(systemName: snapshot?.reconciliation.status == "matched" ? "checkmark.circle.fill" : "circle.dashed")
                    .foregroundStyle(snapshot?.reconciliation.status == "matched" ? Palette.mint : Palette.muted)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(snapshot?.reconciliation.coveragePercent.map { "\($0.formatted(.number.precision(.fractionLength(0...1))))%" } ?? "—")
                    .font(.system(size: 24, weight: .semibold, design: .rounded))
                Text("attributed").font(.system(size: 11)).foregroundStyle(Palette.muted)
            }
            Text(snapshot?.reconciliation.label ?? "Coverage will be checked against the provider’s product total.")
                .font(.system(size: 11)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            if let difference = snapshot?.reconciliation.differenceCents, abs(difference) > 0.005 {
                Text("Unreconciled difference: \(money(difference))").font(.system(size: 11, weight: .medium)).foregroundStyle(Palette.amber)
            }
        }.padding(20).card()
    }

    private var tokenSummary: some View {
        HStack(spacing: 25) {
            VStack(alignment: .leading, spacing: 5) {
                Text("Token mix").font(.system(size: 13, weight: .semibold))
                Text(snapshot?.tokenBreakdown.scope == "unavailable" || snapshot == nil ? "Token scope unavailable" : "\(snapshot!.tokenBreakdown.scope) scope")
                    .font(.system(size: 10)).foregroundStyle(Palette.muted)
            }
            Spacer()
            tokenValue("INPUT", snapshot?.tokenBreakdown.input)
            tokenValue("OUTPUT", snapshot?.tokenBreakdown.output)
            tokenValue("CACHE READ", snapshot?.tokenBreakdown.cacheRead)
            tokenValue("CACHE WRITE", snapshot?.tokenBreakdown.cacheWrite)
        }.padding(.horizontal, 22).padding(.vertical, 19).card()
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array((snapshot?.warnings ?? []).enumerated()), id: \.offset) { _, warning in
                HStack(alignment: .top, spacing: 7) {
                    Image(systemName: "info.circle")
                    Text(warning).fixedSize(horizontal: false, vertical: true)
                }.font(.system(size: 10)).foregroundStyle(Palette.muted)
            }
            HStack {
                Text("Last successful refresh: \(store.lastRefresh)")
                Spacer()
                Text("Usage cost ≠ invoice charge")
                Text("·")
                Text("Stored on this Mac")
            }.font(.system(size: 10)).foregroundStyle(Palette.muted)
            Text(store.refreshCadence).font(.system(size: 10)).foregroundStyle(Palette.muted)
        }
    }

    private func tokenValue(_ label: String, _ value: Double?) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(label).font(.system(size: 8, weight: .semibold)).tracking(1.0).foregroundStyle(Palette.muted)
            Text(quantity(value)).font(.system(size: 16, weight: .medium, design: .monospaced))
        }.frame(minWidth: 85, alignment: .leading)
    }

    private func eventValueCaption(_ total: SpendSnapshot.EventTotals) -> String {
        let missing = total.chargedCents != nil ? total.chargedCentsMissingCount : total.modelCostCentsMissingCount
        guard let missing else { return "Reported event values · not cash charges" }
        let valued = max(0, total.eventCount - missing)
        if missing > 0 {
            return "Partial: \(valued.formatted()) of \(total.eventCount.formatted()) events · not cash charges"
        }
        return "\(valued.formatted()) valued events · not cash charges"
    }

    private func banner(_ text: String, icon: String, color: Color) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
            Text(text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }.font(.system(size: 12)).foregroundStyle(color).padding(14)
            .background(color.opacity(0.07), in: RoundedRectangle(cornerRadius: 9))
            .overlay { RoundedRectangle(cornerRadius: 9).stroke(color.opacity(0.15), lineWidth: 1) }
    }
}

struct MetricCard: View {
    let title: String
    let value: Double?
    let caption: String
    let icon: String
    let accent: Color
    var featured = false
    var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text(title).font(.system(size: 9, weight: .semibold)).tracking(1.3).foregroundStyle(Palette.muted).frame(height: 22, alignment: .leading)
                Spacer()
                Image(systemName: icon).font(.system(size: 14)).foregroundStyle(accent)
            }
            Text(money(value)).font(.system(size: 33, weight: .medium, design: .rounded)).tracking(-0.8).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
                .foregroundStyle(featured ? Palette.mint : Color.white)
            Text(value == nil ? (loaded ? "Not supplied by the provider" : "Unavailable · refresh to check") : caption).font(.system(size: 10)).foregroundStyle(Palette.muted).frame(height: 26, alignment: .topLeading)
        }.padding(21).frame(maxWidth: .infinity, alignment: .leading).card()
    }
}

struct SpendRowView: View {
    let row: SpendSnapshot.Row
    let rank: Int
    let maximum: Double
    let category: SpendCategory
    let rankByTokens: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Text(String(format: "%02d", rank)).font(.system(size: 10, weight: .medium, design: .monospaced)).foregroundStyle(Palette.muted).frame(width: 19)
                Image(systemName: category.icon).font(.system(size: 15)).foregroundStyle(Palette.mint)
                    .frame(width: 36, height: 36).background(Palette.mint.opacity(0.07), in: RoundedRectangle(cornerRadius: 9))
                VStack(alignment: .leading, spacing: 5) {
                    Text(row.name).font(.system(size: 13, weight: .medium)).lineLimit(1).help(row.name)
                    Text(row.detail.isEmpty ? row.attribution : row.detail).font(.system(size: 10)).foregroundStyle(Palette.muted).lineLimit(1).help(row.detail)
                }
                Spacer(minLength: 12)
                VStack(alignment: .trailing, spacing: 5) {
                    Text(rankByTokens ? quantity(row.tokens) : money(row.rankedCostCents)).font(.system(size: 16, weight: .medium, design: .rounded)).monospacedDigit()
                    if rankByTokens {
                        Text("Dollar amount unavailable").font(.system(size: 9)).foregroundStyle(Palette.muted)
                    } else if row.isRecordedValue {
                        Text("Usage value · not cash charged").font(.system(size: 9)).foregroundStyle(Palette.muted)
                    } else if row.isModelCost {
                        Text("Model usage · not cash charged").font(.system(size: 9)).foregroundStyle(Palette.muted)
                    } else if let onDemand = row.onDemandCents {
                        Text("\(money(onDemand)) on-demand").font(.system(size: 9)).foregroundStyle(Palette.amber.opacity(0.85))
                    } else { Text("On-demand unavailable").font(.system(size: 9)).foregroundStyle(Palette.muted) }
                }
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(Palette.muted)
            }
            if let value = rankByTokens ? row.tokens : row.rankedCostCents, maximum > 0 {
                GeometryReader { geometry in
                    Capsule().fill(Palette.line)
                    Capsule().fill(Palette.mint.opacity(0.65)).frame(width: geometry.size.width * min(max(value / maximum, 0), 1))
                }.frame(height: 3).padding(.leading, 67)
                .accessibilityLabel(rankByTokens ? "\(row.name): \(quantity(value)) tokens; dollar amount unavailable" : "\(row.name): \(money(value)) \(row.isRecordedValue ? "recorded usage value, not cash charged" : row.isModelCost ? "model usage value, not cash charged" : "usage cost")")
            }
        }.padding(.horizontal, 22).padding(.vertical, 18)
    }
}

struct StatusBadge: View {
    let text: String
    let color: Color
    var body: some View {
        Text(text).font(.system(size: 9, weight: .bold)).tracking(1.0).foregroundStyle(color)
            .padding(.horizontal, 7).padding(.vertical, 4).background(color.opacity(0.12), in: Capsule())
    }
}

private extension View {
    func card() -> some View {
        background(Palette.panel, in: RoundedRectangle(cornerRadius: 12))
            .overlay { RoundedRectangle(cornerRadius: 12).stroke(Palette.line, lineWidth: 1) }
    }
}
