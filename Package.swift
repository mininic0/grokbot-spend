// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "GrokSpend",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "GrokSpend", targets: ["GrokSpend"]),
        .executable(name: "GrokSpendAuth", targets: ["GrokSpendAuth"]),
    ],
    targets: [
        .executableTarget(name: "GrokSpend"),
        .executableTarget(name: "GrokSpendAuth"),
    ]
)
