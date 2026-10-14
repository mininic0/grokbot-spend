import Foundation
import Security
import CommonCrypto
import LocalAuthentication
import Darwin

// Read-only interoperability with Electron's macOS v10 safeStorage format.
// No Grok Bot code is loaded, and no account/keychain items are created or changed.
// Format references:
// https://github.com/chromium/chromium/blob/140.0.7339.0/components/os_crypt/sync/os_crypt_mac.mm
// https://github.com/electron/electron/blob/main/shell/browser/electron_browser_main_parts.cc

enum AuthFailure: Error {
    case usage, unsafeOutput, missingStore, invalidStore, noActiveAccount
    case missingToken, unsupportedCipher, keychain(OSStatus), crypto
    case invalidToken, expiredToken, timedOut, nonInteractiveTimeout

    var code: Int32 {
        switch self {
        case .usage: return 64
        case .unsafeOutput: return 65
        case .missingStore, .noActiveAccount, .missingToken: return 66
        case .invalidStore, .unsupportedCipher, .crypto, .invalidToken: return 67
        case .keychain: return 68
        case .expiredToken: return 69
        case .timedOut, .nonInteractiveTimeout: return 70
        }
    }

    var message: String {
        switch self {
        case .usage:
            return "Usage: GrokSpendAuth check|access-token|self-test [--no-prompt]. access-token requires a pipe."
        case .unsafeOutput:
            return "Refusing to print an access token to a terminal or regular file; use the collector pipe."
        case .missingStore:
            return "Grok Bot's session store is unavailable. Open Grok Bot and sign in, then retry."
        case .invalidStore:
            return "Grok Bot's session store has an unsupported or invalid format. No data was changed."
        case .noActiveAccount:
            return "No active Grok Bot account is saved. Open Grok Bot and sign in, then retry."
        case .missingToken:
            return "The active Grok Bot account has no saved access token. Open Grok Bot and retry."
        case .unsupportedCipher:
            return "The saved session uses an unsupported encryption format. No data was changed."
        case .keychain(let status):
            if [errSecAuthFailed, errSecInteractionNotAllowed, errSecUserCanceled].contains(status) {
                return "Keychain permission to read Grok Bot's Safe Storage key is unavailable (status \(status)). Use Connect without --no-prompt and approve the normal macOS Keychain request."
            }
            return "Grok Bot's Safe Storage key is unavailable (Keychain status \(status)). Unlock the login Keychain, then use Connect without --no-prompt to allow normal macOS access."
        case .crypto:
            return "The saved session could not be decrypted with Grok Bot's existing Safe Storage key."
        case .invalidToken:
            return "The saved access token is invalid. Open Grok Bot and sign in again."
        case .expiredToken:
            return "The saved access token has expired. Open Grok Bot so it can renew its session, then retry. Grok Spend does not refresh credentials."
        case .timedOut:
            return "Keychain access timed out after 180 seconds. Retry and respond to the macOS access prompt."
        case .nonInteractiveTimeout:
            return "Noninteractive Keychain access timed out after 10 seconds. Use Connect to grant normal macOS access, then retry."
        }
    }
}

func report(_ failure: AuthFailure) -> Never {
    FileHandle.standardError.write(Data((failure.message + "\n").utf8))
    exit(failure.code)
}

func readStore(at url: URL) throws -> [String: Any] {
    // Open the original file read-only, without following a final symlink. Read
    // through the same descriptor so an app-side atomic replacement is harmless.
    let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw AuthFailure.missingStore }
    defer { close(fd) }
    var info = stat()
    guard fstat(fd, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFREG,
          info.st_uid == getuid(), info.st_size > 0,
          info.st_size <= 8 * 1024 * 1024 else { throw AuthFailure.invalidStore }
    var bytes = [UInt8](repeating: 0, count: Int(info.st_size))
    var offset = 0
    while offset < bytes.count {
        let remaining = bytes.count - offset
        let count = bytes.withUnsafeMutableBytes { buffer in
            Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), remaining)
        }
        if count < 0 && errno == EINTR { continue }
        guard count > 0 else { throw AuthFailure.invalidStore }
        offset += count
    }
    guard let store = try? JSONSerialization.jsonObject(with: Data(bytes)) as? [String: Any],
          let version = store["version"] as? NSNumber,
          CFGetTypeID(version) != CFBooleanGetTypeID(), version.doubleValue == 1
    else { throw AuthFailure.invalidStore }
    return store
}

func encryptedAccessToken(in store: [String: Any]) throws -> Data {
    guard let serializedAccounts = store["cursor-accounts"] as? String,
          let encodedAccounts = serializedAccounts.data(using: .utf8),
          let accountStore = try? JSONSerialization.jsonObject(with: encodedAccounts) as? [String: Any],
          let accounts = accountStore["accounts"] as? [String: Any]
    else { throw AuthFailure.invalidStore }
    guard let active = accountStore["active"] as? String, !active.isEmpty,
          let account = accounts[active] as? [String: Any]
    else { throw AuthFailure.noActiveAccount }
    guard let encoded = account["cursor-access-token"] as? String, !encoded.isEmpty
    else { throw AuthFailure.missingToken }
    // Current saved-account slots contain bare base64. Reject a future/legacy
    // scoped envelope rather than accidentally selecting a different account.
    guard let cipher = Data(base64Encoded: encoded),
          cipher.starts(with: Data("v10".utf8)), cipher.count > 3,
          (cipher.count - 3) % kCCBlockSizeAES128 == 0
    else { throw AuthFailure.unsupportedCipher }
    return cipher
}

func readSafeStoragePassword(noPrompt: Bool) throws -> Data {
    var query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "Grok Bot Safe Storage",
        // Grok Bot 0.70.2 uses this account name in its existing keychain item.
        kSecAttrAccount as String: "Grok Bot Key",
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne
    ]
    if noPrompt {
        // Grok Bot's item lives in the legacy file-based login Keychain. The
        // LAContext flag alone does not suppress its authorization UI. This
        // deprecated API still controls that backend's UI for THIS PROCESS
        // only; it neither changes item ACLs nor grants/deletes any access.
        let uiStatus = SecKeychainSetUserInteractionAllowed(false)
        guard uiStatus == errSecSuccess else { throw AuthFailure.keychain(uiStatus) }
        let context = LAContext()
        context.interactionNotAllowed = true
        query[kSecUseAuthenticationContext as String] = context
    }
    var item: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &item)
    guard status == errSecSuccess, let bytes = item as? Data, !bytes.isEmpty
    else { throw AuthFailure.keychain(status == errSecSuccess ? errSecDecode : status) }
    return bytes
}

func deriveKey(password: Data) throws -> Data {
    let salt = Data("saltysalt".utf8)
    var key = Data(count: kCCKeySizeAES128)
    let status = key.withUnsafeMutableBytes { output in
        password.withUnsafeBytes { input in
            salt.withUnsafeBytes { saltBytes in
                CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2),
                    input.baseAddress!.assumingMemoryBound(to: Int8.self), password.count,
                    saltBytes.baseAddress!.assumingMemoryBound(to: UInt8.self), salt.count,
                    CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA1), 1003,
                    output.baseAddress!.assumingMemoryBound(to: UInt8.self), kCCKeySizeAES128)
            }
        }
    }
    guard status == kCCSuccess else { throw AuthFailure.crypto }
    return key
}

func decrypt(_ envelope: Data, password: Data) throws -> Data {
    guard envelope.starts(with: Data("v10".utf8)), envelope.count > 3,
          (envelope.count - 3) % kCCBlockSizeAES128 == 0
    else { throw AuthFailure.unsupportedCipher }
    var key = try deriveKey(password: password)
    defer { key.resetBytes(in: 0..<key.count) }
    let iv = Data(repeating: 0x20, count: kCCBlockSizeAES128)
    let cipher = envelope.dropFirst(3)
    let capacity = cipher.count + kCCBlockSizeAES128
    var clear = Data(count: capacity)
    var moved = 0
    let status = clear.withUnsafeMutableBytes { output in
        key.withUnsafeBytes { keyBytes in
            iv.withUnsafeBytes { ivBytes in
                cipher.withUnsafeBytes { input in
                    CCCrypt(CCOperation(kCCDecrypt), CCAlgorithm(kCCAlgorithmAES),
                        CCOptions(kCCOptionPKCS7Padding), keyBytes.baseAddress, kCCKeySizeAES128,
                        ivBytes.baseAddress, input.baseAddress, cipher.count,
                        output.baseAddress, capacity, &moved)
                }
            }
        }
    }
    guard status == kCCSuccess else {
        clear.resetBytes(in: 0..<clear.count)
        throw AuthFailure.crypto
    }
    clear.count = moved
    return clear
}

func tokenExpiry(_ bytes: Data) throws -> Date? {
    guard let token = String(data: bytes, encoding: .utf8), !token.isEmpty,
          token.utf8.allSatisfy({ $0 > 0x20 && $0 < 0x7f })
    else { throw AuthFailure.invalidToken }
    let parts = token.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 3 else { return nil } // Opaque tokens remain supported.
    var middle = String(parts[1]).replacingOccurrences(of: "-", with: "+")
        .replacingOccurrences(of: "_", with: "/")
    middle += String(repeating: "=", count: (4 - middle.count % 4) % 4)
    guard let payload = Data(base64Encoded: middle),
          let claims = try? JSONSerialization.jsonObject(with: payload) as? [String: Any]
    else { throw AuthFailure.invalidToken }
    // This is an unverified local expiry hint. The server still validates auth.
    guard let expiry = claims["exp"] as? NSNumber else { return nil }
    guard CFGetTypeID(expiry) != CFBooleanGetTypeID(), expiry.doubleValue.isFinite
    else { throw AuthFailure.invalidToken }
    return Date(timeIntervalSince1970: expiry.doubleValue)
}

func requirePipe() throws {
    var info = stat()
    guard fstat(STDOUT_FILENO, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFIFO || (info.st_mode & S_IFMT) == S_IFSOCK
    else { throw AuthFailure.unsafeOutput }
}

func selfTest() throws {
    // Independently generated with Python hashlib.pbkdf2_hmac and OpenSSL AES.
    // These are public synthetic inputs, never a user credential.
    let password = Data("grok-spend-synthetic-password".utf8)
    let expectedKey = "ca68cacfb61d18a30662ae554207c6e7"
    let actualKey = try deriveKey(password: password).map { String(format: "%02x", $0) }.joined()
    guard actualKey == expectedKey else { throw AuthFailure.crypto }
    let cipher = Data(base64Encoded: "djEwGgwOjtCuapQCQbOdjm9DI1YEFn5OR7yHtKdJvl6YipY=")!
    guard try decrypt(cipher, password: password) == Data("synthetic-access-token".utf8)
    else { throw AuthFailure.crypto }
    do {
        _ = try decrypt(Data("v20unsupported".utf8), password: password)
        throw AuthFailure.crypto
    } catch AuthFailure.unsupportedCipher { }
    let accounts: [String: Any] = ["active": "synthetic-active", "accounts": [
        "synthetic-active": ["cursor-access-token": cipher.base64EncodedString()],
        "synthetic-other": ["cursor-access-token": "invalid-other-account"]]]
    let accountsJSON = String(data: try JSONSerialization.data(withJSONObject: accounts), encoding: .utf8)!
    guard try encryptedAccessToken(in: ["cursor-accounts": accountsJSON]) == cipher
    else { throw AuthFailure.crypto }
    do {
        _ = try encryptedAccessToken(in: ["cursor-accounts": "{\"active\":null,\"accounts\":{}}"])
        throw AuthFailure.crypto
    } catch AuthFailure.noActiveAccount { }
    let payload = Data("{\"exp\":1234567890}".utf8).base64EncodedString()
        .replacingOccurrences(of: "=", with: "").replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
    guard try tokenExpiry(Data("e30.\(payload).synthetic".utf8))?.timeIntervalSince1970 == 1234567890
    else { throw AuthFailure.crypto }
    print("{\"ok\":true,\"checks\":[\"pbkdf2\",\"aes-cbc\",\"unsupported-format\",\"active-account-selection\",\"signed-out\",\"jwt-expiry\"]}")
}

do {
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard let command = arguments.first, ["check", "access-token", "self-test"].contains(command),
          arguments.dropFirst().allSatisfy({ $0 == "--no-prompt" })
    else { throw AuthFailure.usage }
    if command == "self-test" {
        try selfTest()
        exit(0)
    }
    if command == "access-token" { try requirePipe() }
    // The collector also owns a subprocess deadline. Bound an unattended native
    // Keychain dialog here without changing the Keychain's security policy.
    let noPrompt = arguments.contains("--no-prompt")
    DispatchQueue.global().asyncAfter(deadline: .now() + (noPrompt ? 10 : 180)) {
        report(noPrompt ? .nonInteractiveTimeout : .timedOut)
    }
    let storeURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Grok Bot/sand-secrets.json")
    let cipher = try encryptedAccessToken(in: readStore(at: storeURL))
    var password = try readSafeStoragePassword(noPrompt: noPrompt)
    defer { password.resetBytes(in: 0..<password.count) }
    var token = try decrypt(cipher, password: password)
    defer { token.resetBytes(in: 0..<token.count) }
    let expiry = try tokenExpiry(token)
    let expired = expiry.map { $0 <= Date() } ?? false
    if command == "check" {
        var result: [String: Any] = ["ok": !expired, "sessionReadable": true,
            "expired": expired, "expiryVerifiedByServer": false]
        if let expiry { result["expiresAt"] = ISO8601DateFormatter().string(from: expiry) }
        let output = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        FileHandle.standardOutput.write(output + Data("\n".utf8))
        if expired { throw AuthFailure.expiredToken }
    } else {
        guard !expired else { throw AuthFailure.expiredToken }
        FileHandle.standardOutput.write(token)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
} catch let failure as AuthFailure {
    report(failure)
} catch {
    // Never interpolate Foundation errors: they can include paths or input data.
    report(.invalidStore)
}
