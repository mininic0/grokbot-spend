import SwiftUI

struct EventCoverageView: View {
    let coverage: SpendSnapshot.EventCoverage
    var compact = false

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: coverage.unknownCount > 0 ? "circle.dashed" : "calendar.badge.clock")
                .font(.system(size: compact ? 14 : 18)).foregroundStyle(Palette.muted)
            VStack(alignment: .leading, spacing: 7) {
                Text("Event coverage · through \(cutoff)").font(.system(size: compact ? 11 : 13, weight: .medium))
                Text("\(coverage.includedCount.formatted()) Grok events · \(coverage.excludedCount.formatted()) other-product events · \(coverage.unknownCount.formatted()) unknown product")
                    .font(.system(size: compact ? 10 : 12)).foregroundStyle(Palette.muted)
                if let unassigned = coverage.unassignedCount, unassigned > 0 {
                    Text("\(unassigned.formatted()) Grok events have no proven parent · \(money(coverage.unassignedValueCents)) recorded value")
                        .font(.system(size: compact ? 10 : 12)).foregroundStyle(Palette.amber)
                }
                if !compact {
                    Text("This cutoff applies to event details. Missing or unknown records can leave attribution incomplete.")
                        .font(.system(size: 10)).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(compact ? 12 : 20)
        .background(Palette.panel, in: RoundedRectangle(cornerRadius: 10))
    }

    private var cutoff: String {
        guard let date = Dates.parse(coverage.through) else { return coverage.through }
        return date.formatted(.dateTime.month(.abbreviated).day().hour().minute()) + " local"
    }
}
