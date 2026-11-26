using System.Text;
using System.Text.Json;

namespace Clearcote;

/// The signed release manifest of a PRO download is missing or does not check out, so the build is not used.
/// <see cref="Reason"/> is the reason code from docs/PRO-RELEASE-MANIFEST.md.
public sealed class ProManifestException : Exception
{
    public ProManifestException(string reason, string message, Exception? inner = null) : base(message, inner) => Reason = reason;

    /// One of: missing, malformed, unknown-key, signature, product, platform, version, revision, archive, binary.
    public string Reason { get; }
}

/// The signed PRO release manifest (docs/PRO-RELEASE-MANIFEST.md): one signed document per platform archive that
/// binds product, version, revision, platform, the archive's SHA-256 and size, and the SHA-256 of the binaries in
/// it. The download answer's own hashes are only used once they equal the manifest's.
internal static class ProManifest
{
    /// For the SDK's own tests only (internal, reached through InternalsVisibleTo): the keys to check against in
    /// place of <see cref="Release.ProReleaseKeys"/>. Nothing in the SDK sets it, and no environment variable,
    /// option or file does.
    internal static IReadOnlyDictionary<string, string>? KeysForTests;

    private static IReadOnlyDictionary<string, string> Keys => KeysForTests ?? Release.ProReleaseKeys;

    private static ProManifestException Refuse(string reason, string what) =>
        new(reason, $"Clearcote Pro release manifest check failed ({reason}): {what}. Refusing to use this build.");

    private static string? Str(JsonElement obj, string name) =>
        obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() : null;

    private static long? Whole(JsonElement obj, string name) =>
        obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number
        && v.TryGetInt64(out var n) ? n : null;

    /// A value for an error message: short, and with nothing but printable ASCII (it may come from the server).
    private static string Show(string? value)
    {
        if (value is null) return "(none)";
        var text = new string(value.Take(80).Select(c => c is >= ' ' and <= '~' ? c : '?').ToArray());
        return $"\"{text}\"";
    }

    /// base64url (RFC 4648 section 5) without padding; null when the text is not that.
    private static byte[]? Base64Url(string text)
    {
        if (text.Length == 0 || text.Length % 4 == 1) return null;
        foreach (var c in text)
            if (!(char.IsAsciiLetterOrDigit(c) || c is '-' or '_')) return null;
        try { return Convert.FromBase64String(text.Replace('-', '+').Replace('_', '/') + new string('=', (4 - text.Length % 4) % 4)); }
        catch (FormatException) { return null; }
    }

    private static bool SameHash(string? a, string? b) =>
        !string.IsNullOrEmpty(a) && string.Equals(a, b, StringComparison.OrdinalIgnoreCase);

    /// Check the signed manifest in a download answer and return its payload. The checks run in the order of
    /// docs/PRO-RELEASE-MANIFEST.md and the first one that fails throws a <see cref="ProManifestException"/>.
    ///
    /// <paramref name="platform"/> is the platform asked for ("windows", "linux", "macos");
    /// <paramref name="wantRevision"/> ("r68") and <paramref name="wantVersion"/> ("158.0.8127.36") are what the
    /// caller pinned, or null.
    public static JsonElement Verify(JsonElement meta, string platform, string? wantRevision = null, string? wantVersion = null)
    {
        // 1. the answer carries a manifest
        if (meta.ValueKind != JsonValueKind.Object || !meta.TryGetProperty("manifest", out var field)
            || field.ValueKind == JsonValueKind.Null || (field.ValueKind == JsonValueKind.String && field.GetString()!.Length == 0))
            throw Refuse("missing", "the download answer has no signed manifest");

        // 2. base64url(payload) "." base64url(signature), and the payload is a JSON object
        if (field.ValueKind != JsonValueKind.String) throw Refuse("malformed", "the manifest is not a string");
        var parts = field.GetString()!.Split('.');
        var body = parts.Length == 2 ? Base64Url(parts[0]) : null;
        var signature = parts.Length == 2 ? Base64Url(parts[1]) : null;
        if (body is null || signature is null) throw Refuse("malformed", "the manifest is not two base64url parts");
        JsonElement payload;
        try
        {
            using var doc = JsonDocument.Parse(body);
            payload = doc.RootElement.Clone();
        }
        catch (JsonException) { throw Refuse("malformed", "the manifest's payload is not JSON"); }
        if (payload.ValueKind != JsonValueKind.Object) throw Refuse("malformed", "the manifest's payload is not a JSON object");

        // 3. signed by a pinned key. Until the signature has verified, `key` is the only field of the payload read.
        var keyId = Str(payload, "key");
        if (keyId is null || !Keys.TryGetValue(keyId, out var keyHex))
            throw Refuse("unknown-key", $"it names the signing key {Show(keyId)}, which is not a release key pinned in this SDK version");

        // 4. the signature, over the ASCII text of the first part
        byte[] key;
        try { key = Convert.FromHexString(keyHex); }
        catch (FormatException) { key = Array.Empty<byte>(); }
        if (signature.Length != 64 || !Ed25519.Verify(key, Encoding.ASCII.GetBytes(parts[0]), signature))
            throw Refuse("signature", $"its signature does not verify under the pinned release key {Show(keyId)}");

        // 5-7. what the manifest is for
        if (Whole(payload, "v") != 1)
            throw Refuse("malformed", "the manifest's format version is not 1");
        if (Str(payload, "product") != "clearcote-pro")
            throw Refuse("product", $"it is for the product {Show(Str(payload, "product"))}, not \"clearcote-pro\"");
        if (Str(payload, "platform") != platform)
            throw Refuse("platform", $"it is for the platform {Show(Str(payload, "platform"))}, not \"{platform}\"");

        // 8-9. the version and revision: the answer's, and the caller's when one was pinned
        var version = Str(payload, "version");
        if (version is null || version != Str(meta, "version"))
            throw Refuse("version", $"it is for version {Show(version)}, but the download answer names {Show(Str(meta, "version"))}");
        if (wantVersion is not null && version != wantVersion)
            throw Refuse("version", $"it is for version {Show(version)}, but version \"{wantVersion}\" was asked for");
        var revision = Str(payload, "revision");
        if (revision is null || revision != Str(meta, "revision"))
            throw Refuse("revision", $"it is for revision {Show(revision)}, but the download answer names {Show(Str(meta, "revision"))}");
        if (wantRevision is not null && revision != wantRevision)
            throw Refuse("revision", $"it is for revision {Show(revision)}, but revision \"{wantRevision}\" was asked for");

        // 10. the archive
        var archive = payload.TryGetProperty("archive", out var ar) ? ar : default;
        if (!SameHash(Str(archive, "sha256"), Str(meta, "sha256")))
            throw Refuse("archive", "the archive SHA-256 in the download answer is not the one the manifest lists");
        if (Whole(archive, "size") is not { } size || size != Whole(meta, "size"))
            throw Refuse("archive", "the archive size in the download answer is not the one the manifest lists");

        // 11. the binaries: the launcher under the answer's `binary` name (the platform's own when it names none,
        // as the download itself reads it), and chrome.dll on Windows
        var binaries = payload.TryGetProperty("binaries", out var bins) ? bins : default;
        var launcher = Str(meta, "binary") ?? (platform == "windows" ? "chrome.exe" : platform == "macos" ? "Chromium" : "chrome");
        if (!SameHash(Str(binaries, launcher), Str(meta, "exe_sha256")))
            throw Refuse("binary", $"the SHA-256 of {Show(launcher)} in the download answer is not one the manifest lists");
        if (platform == "windows" && !SameHash(Str(binaries, "chrome.dll"), Str(meta, "chrome_dll_sha256")))
            throw Refuse("binary", "the SHA-256 of \"chrome.dll\" in the download answer is not one the manifest lists");

        return payload;
    }
}
