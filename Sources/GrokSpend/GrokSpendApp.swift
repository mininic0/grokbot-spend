import AppKit
import SwiftUI

@main
struct GrokSpendApp: App {
    @StateObject private var store = SpendStore()

    var body: some Scene {
        Window("Grok Spend", id: "dashboard") {
            DashboardView(store: store)
                .preferredColorScheme(.dark)
        }
        .defaultSize(width: 1190, height: 840)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Refresh billing") { store.refresh() }.keyboardShortcut("r")
                Button("Export snapshot…") { store.exportJSON() }.keyboardShortcut("e", modifiers: [.command, .shift])
                Button("Disconnect session") { store.disconnect() }.disabled(!store.hasSession)
            }
        }
        MenuBarExtra {
            SpendMenu(store: store)
        } label: {
            Label("\(store.demo ? "DEMO " : "")\(money(store.todayCents))", systemImage: "chart.bar.xaxis")
        }
        .menuBarExtraStyle(.window)
    }
}

struct SpendMenu: View {
    @ObservedObject var store: SpendStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("Grok Spend", systemImage: "chart.bar.xaxis").font(.headline)
                Spacer()
                if store.demo { StatusBadge(text: "DEMO", color: Palette.amber) }
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("TODAY’S USAGE · UTC").font(.system(size: 10, weight: .semibold)).tracking(1.4).foregroundStyle(.secondary)
                Text(money(store.todayCents)).font(.system(size: 34, weight: .semibold, design: .rounded)).monospacedDigit()
                Text(store.todayCaption).font(.caption).foregroundStyle(.secondary)
            }
            Divider()
            LabeledContent("This billing period", value: money(store.snapshot?.totals.usageCents))
            LabeledContent("On-demand usage", value: money(store.snapshot?.totals.onDemandCents))
            Text("Last success: \(store.lastRefresh)").font(.caption).foregroundStyle(.secondary)
            Text(store.refreshCadence).font(.caption2).foregroundStyle(.secondary)
            if let error = store.error { Text(error).font(.caption).foregroundStyle(Palette.amber).fixedSize(horizontal: false, vertical: true) }
            if store.hasSession {
                Button("Disconnect session") { store.disconnect() }.font(.caption)
                    .help("Clear the in-memory session and stop automatic refresh; keep the saved snapshot")
            }
            HStack {
                Button("Open dashboard") {
                    openWindow(id: "dashboard")
                    NSApp.activate(ignoringOtherApps: true)
                }.buttonStyle(.plain).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Palette.mint, in: RoundedRectangle(cornerRadius: 7)).foregroundStyle(Palette.background)
                Button { store.refresh() } label: {
                    Image(systemName: "arrow.clockwise")
                }.disabled(store.isRefreshing).help("Refresh billing")
                Spacer()
                Button { NSApp.terminate(nil) } label: { Image(systemName: "power") }.help("Quit Grok Spend")
            }
        }
        .padding(20)
        .frame(width: 330)
        .preferredColorScheme(.dark)
    }
}
