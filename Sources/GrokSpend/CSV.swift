import Foundation

func csvCell(_ value: String) -> String {
    let isNumber = Double(value).map { $0.isFinite } ?? false
    let leading = value.trimmingCharacters(in: .whitespacesAndNewlines).prefix(1)
    let formula = ["=", "+", "-", "@"].contains(String(leading))
    let controlPrefix = ["\t", "\r"].contains(String(value.prefix(1)))
    let safe = !isNumber && (formula || controlPrefix) ? "'" + value : value
    return "\"" + safe.replacingOccurrences(of: "\"", with: "\"\"") + "\""
}
