import SwiftUI

struct AutomationDetailView: View {
    let summary: AutomationSummary
    let window: AutomationWindow
    let through: Date?
    let demo: Bool
    @Environment(\.dismiss) private var dismiss
    @State private var visibleLimit = 50

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 7) {
                    HStack(spacing: 8) {
                        Text("AUTOMATION · EXACT MATCHED RUNS").font(.system(size: 10, weight: .semibold)).tracking(1).foregroundStyle(Palette.mint)
                        if demo { StatusBadge(text: "DEMO", color: Palette.amber) }
                    }
                    Text(summary.row.name).font(.system(size: 24, weight: .semibold)).textSelection(.enabled)
                    Text("Bot: \(summary.botName)").font(.system(size: 13)).foregroundStyle(Palette.muted).textSelection(.enabled)
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            HStack(spacing: 32) {
                metric("RECORDED VALUE", money(summary.recordedValueCents))
                metric("AVG / MATCHED RUN", eventMoney(summary.averageValueCents))
                metric("MATCHED ACTIVE RUNS", summary.runs.count.formatted())
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading).background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
            Text("\(window.rawValue) · through \(activityDate(through)) local. Run values include only matched activity in this window; historical totals are partial. Recorded value is consumption, not cash charges.")
                .font(.system(size: 11)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            if summary.missingValueCount > 0 || summary.missingTimeCount > 0 {
                Text("\(summary.missingValueCount) events have no recorded amount · \(summary.missingTimeCount) have no timestamp. Unavailable values are not counted as zero.")
                    .font(.system(size: 11)).foregroundStyle(Palette.amber)
            }
            Text("Find “\(summary.row.name)” under “\(summary.botName)” in Grok Bot to review its settings.")
                .font(.system(size: 11)).foregroundStyle(Palette.muted).textSelection(.enabled)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(summary.runs.prefix(visibleLimit)) { run in
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 10) {
                                Text("Run conversation: \(run.id)").font(.system(size: 10)).textSelection(.enabled)
                                ForEach(run.events) { event in
                                    HStack(alignment: .top, spacing: 15) {
                                        Text(event.timestampISO.map { activityDate(Dates.parse($0)) } ?? "Time unavailable").frame(width: 125, alignment: .leading)
                                        Text(event.model ?? "Model unavailable").frame(maxWidth: .infinity, alignment: .leading)
                                        VStack(alignment: .trailing, spacing: 3) {
                                            Text(eventMoney(event.chargedCents))
                                            Text("recorded value").font(.system(size: 9)).foregroundStyle(Palette.muted)
                                        }.frame(width: 105, alignment: .trailing)
                                    }.font(.system(size: 11)).monospacedDigit()
                                }
                            }.padding(.vertical, 12).foregroundStyle(Palette.muted)
                        } label: {
                            HStack(alignment: .top, spacing: 15) {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text("\(activityDate(run.firstActivity)) → \(activityDate(run.lastActivity))").font(.system(size: 12, weight: .medium))
                                    Text("\(run.events.count.formatted()) events · Provider model: \(run.models.joined(separator: ", "))")
                                        .font(.system(size: 10)).foregroundStyle(Palette.muted)
                                }
                                Spacer()
                                Text(eventMoney(run.recordedValueCents)).font(.system(size: 15, weight: .medium, design: .rounded)).monospacedDigit().foregroundStyle(Palette.mint)
                            }.padding(.vertical, 12)
                        }
                        Rectangle().fill(Palette.line).frame(height: 1)
                    }
                    if visibleLimit < summary.runs.count {
                        Button("Show more matched runs") { visibleLimit += 50 }.padding(.vertical, 15)
                    }
                    if summary.runs.isEmpty {
                        Text("No exact run activity matched this window. This does not prove the automation had no usage.")
                            .font(.system(size: 12)).foregroundStyle(Palette.muted).padding(.vertical, 25)
                    }
                }.padding(.trailing, 5)
            }
            Text("Each group uses an exact account, bot, automation, and conversation ID match. No ownership is inferred from timing. A provider model alias may not reveal the underlying model.")
                .font(.system(size: 10)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
        }.padding(28).frame(width: 930, height: 700).background(Palette.background).preferredColorScheme(.dark)
    }

    private func metric(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 9, weight: .semibold)).foregroundStyle(Palette.muted)
            Text(value).font(.system(size: 24, weight: .medium, design: .rounded)).monospacedDigit()
        }
    }
}
