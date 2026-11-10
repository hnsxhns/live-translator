import UIKit
import UniformTypeIdentifiers

/// Output Folder (same as the desktop app): a folder the user picks in the Files app (iCloud Drive or On My iPad).
/// The app creates Backup/ and Outputs/ inside it and saves files there without asking each time.
/// iOS only lets an app keep writing to a folder it was given through a security-scoped bookmark.
final class OutputFolder: NSObject, UIDocumentPickerDelegate {
    private static let key = "outputFolderBookmark"
    private var pending: ((UIDocumentPickerViewController, [URL]?) -> Void)?

    /// The chosen folder (nil if none, or if it was moved/deleted).
    static func url() -> URL? {
        guard let data = UserDefaults.standard.data(forKey: key) else { return nil }
        var stale = false
        guard let u = try? URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale)
        else { return nil }
        if stale, u.startAccessingSecurityScopedResource() {      // refresh an outdated bookmark
            defer { u.stopAccessingSecurityScopedResource() }
            if let fresh = try? u.bookmarkData() { UserDefaults.standard.set(fresh, forKey: key) }
        }
        return u
    }

    /// Ask the user for a folder; creates Backup/ and Outputs/. done(name, error) — both nil when cancelled.
    func pick(from vc: UIViewController, done: @escaping (String?, String?) -> Void) {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder])
        picker.allowsMultipleSelection = false
        picker.directoryURL = Self.url()
        picker.delegate = self
        pending = { _, urls in
            guard let u = urls?.first else { return done(nil, nil) }
            guard u.startAccessingSecurityScopedResource() else { return done(nil, "không có quyền ghi vào thư mục này") }
            defer { u.stopAccessingSecurityScopedResource() }
            do {
                for sub in ["Backup", "Outputs"] {
                    try FileManager.default.createDirectory(at: u.appendingPathComponent(sub), withIntermediateDirectories: true)
                }
                UserDefaults.standard.set(try u.bookmarkData(), forKey: Self.key)
                done(u.lastPathComponent, nil)
            } catch {
                done(nil, error.localizedDescription)
            }
        }
        vc.present(picker, animated: true)
    }

    /// Write a file into <Output Folder>/<sub>/<name>. Returns an error text, or nil when saved.
    static func save(sub: String, name: String, data: Data) -> String? {
        guard let u = url() else { return "chưa chọn Output Folder" }
        guard u.startAccessingSecurityScopedResource() else { return "không có quyền ghi vào Output Folder" }
        defer { u.stopAccessingSecurityScopedResource() }
        do {
            let dir = u.appendingPathComponent(sub)
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try data.write(to: dir.appendingPathComponent(name.replacingOccurrences(of: "/", with: "_")), options: .atomic)
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    /// Restore: pick a backup file, starting in <Output Folder>/Backup. done(name, text) — nil when cancelled.
    func pickBackup(from vc: UIViewController, done: @escaping (String?, String?) -> Void) {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.json], asCopy: true)
        picker.allowsMultipleSelection = false
        if let u = Self.url() { picker.directoryURL = u.appendingPathComponent("Backup") }
        picker.delegate = self
        pending = { _, urls in
            guard let f = urls?.first, let text = try? String(contentsOf: f, encoding: .utf8) else { return done(nil, nil) }
            done(f.lastPathComponent, text)
        }
        vc.present(picker, animated: true)
    }

    func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        let p = pending
        pending = nil
        p?(controller, urls)
    }

    func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
        let p = pending
        pending = nil
        p?(controller, nil)
    }
}
