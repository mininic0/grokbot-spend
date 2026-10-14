import SwiftUI

struct RowSelection: Identifiable {
    let row: SpendSnapshot.Row
    let category: SpendCategory
    var id: String { category.id + ":" + row.id }
}

struct EventDetailView: View {
    let snapshot: SpendSnapshot
    let selection: RowSelection
    @Environment(\.dismiss) private var dismiss
    @State private var visibleLimit = 200
    private let events: [SpendSnapshot.Event]

    init(snapshot: SpendSnapshot, selection: RowSelection) {
        self.snapshot = snapshot
        self.selection = selection
        events = snapshot.events(for: selection.row, category: selection.category)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        Text("\(selection.category.rawValue.uppercased()) · USAGE EVENTS")
                            .font(.system(size: 10, weight: .semibold)).tracking(1.5).foregroundStyle(Palette.mint)
                        if snapshot.mode == "demo" { StatusBadge(text: "DEMO", color: Palette.amber) }
                    }
                    Text(selection.row.name).font(.system(size: 25, weight: .semibold)).textSelection(.enabled)
                    Text("\(Dates.period(snapshot.period)) · UTC billing dates")
                        .font(.system(size: 11)).foregroundStyle(Palette.muted)
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            HStack(spacing: 35) {
                detailMetric(selection.row.isRecordedValue ? "RECORDED USAGE VALUE" : selection.row.isModelCost ? "MODEL USAGE VALUE" : "CATEGORY USAGE COST", money(selection.row.rankedCostCents))
                if selection.row.rankedCostCents == nil { detailMetric("CATEGORY TOKENS", quantity(selection.row.tokens)) }
                detailMetric("MATCHING EVENTS", events.count.formatted())
                Spacer()
            }.padding(18).background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
            Text(selection.row.isRecordedValue
                 ? "This is the provider’s recorded consumption value from matched events, including usage covered by the plan. It is not the amount you paid or an invoice charge. Model values are shown separately when supplied."
                 : selection.row.isModelCost
                 ? "This model usage value comes from exactly matched events. It is not a cash charge. The provider-recorded amount can represent included-plan usage; it is not treated as on-demand spend or an invoice charge."
                 : "Category totals come from billing aggregates. Event details may cover only part of that total. Model usage value and the provider-recorded amount can represent included-plan consumption; neither is evidence of a cash charge.")
                .font(.system(size: 12)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            if selection.row.id == "unassigned-events" {
                Text("Ownership is unavailable for these events. They are grouped here to expose their recorded value without assigning them to a bot or routine.")
                    .font(.system(size: 11)).foregroundStyle(Palette.amber).fixedSize(horizontal: false, vertical: true)
            }
            if let coverage = snapshot.eventCoverage {
                EventCoverageView(coverage: coverage, compact: true)
            }
            if events.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "text.magnifyingglass").font(.system(size: 28)).foregroundStyle(Palette.muted)
                    Text(emptyTitle).font(.system(size: 16, weight: .medium))
                    Text(emptyDetail).font(.system(size: 12)).foregroundStyle(Palette.muted)
                        .multilineTextAlignment(.center).frame(maxWidth: 550)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                eventHeader
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(events.prefix(visibleLimit)) { event in
                            VStack(alignment: .leading, spacing: 9) {
                                HStack(alignment: .top, spacing: 14) {
                                    Text(timestamp(event.timestampISO)).frame(width: 124, alignment: .leading)
                                    Text(event.model ?? "Unavailable").frame(maxWidth: .infinity, alignment: .leading)
                                    Text(quantity(event.totalTokens)).frame(width: 80, alignment: .trailing)
                                    Text(eventMoney(event.modelCostCents)).frame(width: 95, alignment: .trailing)
                                    Text(eventMoney(event.chargedCents)).frame(width: 108, alignment: .trailing)
                                }.font(.system(size: 12, weight: .medium)).monospacedDigit().textSelection(.enabled)
                                HStack(spacing: 6) {
                                    Image(systemName: event.attribution?.method == nil ? "questionmark.circle" : "link")
                                    Text(attributionLabel(event))
                                    if let conversation = event.conversationId {
                                        Text("· Conversation \(conversation)").lineLimit(1).truncationMode(.middle).help(conversation)
                                    }
                                }.font(.system(size: 10)).foregroundStyle(Palette.muted).textSelection(.enabled)
                            }.padding(.vertical, 15)
                            Rectangle().fill(Palette.line).frame(height: 1)
                        }
                        if visibleLimit < events.count {
                            Button("Show \(min(200, events.count - visibleLimit)) more events (\((events.count - visibleLimit).formatted()) remaining)") {
                                visibleLimit = min(events.count, visibleLimit + 200)
                            }.padding(18)
                        }
                    }
                }
            }
            HStack {
                Image(systemName: "lock.shield")
                Text("Account-scoped records; ownership uses exact identifiers. No message or conversation content is collected.")
            }.font(.system(size: 10)).foregroundStyle(Palette.muted)
        }
        .padding(28)
        .frame(width: 930, height: 700)
        .background(Palette.background)
        .preferredColorScheme(.dark)
    }

    private var eventHeader: some View {
        HStack(spacing: 14) {
            Text("TIME · LOCAL").frame(width: 124, alignment: .leading)
            Text("MODEL").frame(maxWidth: .infinity, alignment: .leading)
            Text("TOKENS").frame(width: 80, alignment: .trailing)
            Text("MODEL USAGE VALUE").multilineTextAlignment(.trailing).frame(width: 95, alignment: .trailing)
            Text("PROVIDER RECORDED").multilineTextAlignment(.trailing).frame(width: 108, alignment: .trailing)
        }.font(.system(size: 9, weight: .semibold)).tracking(0.6).foregroundStyle(Palette.muted)
    }

    private var emptyTitle: String {
        selection.category == .sources ? "Source events cannot be matched yet" : "No exact event matches available"
    }

    private var emptyDetail: String {
        if selection.category == .sources {
            return "The source aggregate does not provide an exact join to individual events. Its reported total is preserved without guessing which events belong to it."
        }
        if !snapshot.capabilities.events {
            return "This snapshot has no supported usage-event feed. The provider’s category total above remains available independently."
        }
        return "No event in this snapshot has the exact identifiers needed for this row. Missing matches do not mean the category had no usage."
    }

    private func detailMetric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(label).font(.system(size: 9, weight: .semibold)).tracking(1).foregroundStyle(Palette.muted)
            Text(value).font(.system(size: 25, weight: .medium, design: .rounded)).monospacedDigit()
        }
    }

    private func timestamp(_ raw: String?) -> String {
        guard let raw, let date = Dates.parse(raw) else { return "Unavailable" }
        return date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    private func attributionLabel(_ event: SpendSnapshot.Event) -> String {
        if let method = event.attribution?.method {
            switch method {
            case "bot-conversation-id": return "Exact bot conversation"
            case "routine-run-id": return event.attribution?.routineName.map { "Exact routine run · \($0)" } ?? "Exact routine run"
            default: return method
            }
        }
        return "Bot attribution unavailable: \(event.attribution?.unassignedReason ?? "unreported")"
    }
}
