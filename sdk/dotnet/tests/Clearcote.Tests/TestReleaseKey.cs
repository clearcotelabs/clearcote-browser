using System.Numerics;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Clearcote.Tests;

/// TEST-ONLY Ed25519 signing (RFC 8032, 5.1.6), for the fake download route. The SDK itself can only verify.
/// The arithmetic here is written separately from the SDK's (Ed25519.cs), so a signature made here that the SDK
/// accepts checks both.
internal static class TestEd25519
{
    private static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
    private static readonly BigInteger L = BigInteger.Pow(2, 252) + BigInteger.Parse("27742317777372353535851937790883648493");
    private static readonly BigInteger D = Mod(-121665 * BigInteger.ModPow(121666, P - 2, P));
    private static readonly BigInteger Bx = BigInteger.Parse("15112221349535400772501151409588531511454012693041857206046113283949847762202");
    private static readonly BigInteger By = BigInteger.Parse("46316835694926478169428394003475163141307993866256225615783033603165251855960");

    private static BigInteger Mod(BigInteger v) => ((v % P) + P) % P;
    private static BigInteger Number(ReadOnlySpan<byte> littleEndian) => new(littleEndian, isUnsigned: true);

    private static byte[] Bytes32(BigInteger v)
    {
        var raw = v.ToByteArray(isUnsigned: true);
        var out32 = new byte[32];
        raw.CopyTo(out32, 0);
        return out32;
    }

    // Extended coordinates (X, Y, Z, T), as in RFC 8032, 5.1.4.
    private static BigInteger[] Add(BigInteger[] p, BigInteger[] q)
    {
        BigInteger a = (p[1] - p[0]) * (q[1] - q[0]), b = (p[1] + p[0]) * (q[1] + q[0]);
        BigInteger c = 2 * D * p[3] * q[3], d = 2 * p[2] * q[2];
        BigInteger e = b - a, f = d - c, g = d + c, h = b + a;
        return new[] { Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h) };
    }

    /// The encoding of k times the base point.
    private static byte[] BaseTimes(BigInteger k)
    {
        BigInteger[] sum = { 0, 1, 1, 0 }, p = { Bx, By, 1, Mod(Bx * By) };
        for (; !k.IsZero; k >>= 1)
        {
            if (!k.IsEven) sum = Add(sum, p);
            p = Add(p, p);
        }
        var inv = BigInteger.ModPow(sum[2], P - 2, P);
        BigInteger x = Mod(sum[0] * inv), y = Mod(sum[1] * inv);
        var enc = Bytes32(y);
        if (!x.IsEven) enc[31] |= 0x80;
        return enc;
    }

    private static (BigInteger Scalar, byte[] Prefix) Expand(byte[] seed)
    {
        var h = SHA512.HashData(seed);
        h[0] &= 248; h[31] &= 127; h[31] |= 64;
        return (Number(h.AsSpan(0, 32)), h[32..]);
    }

    public static byte[] PublicKey(byte[] seed) => BaseTimes(Expand(seed).Scalar);

    public static byte[] Sign(byte[] seed, byte[] message)
    {
        var (a, prefix) = Expand(seed);
        var publicKey = BaseTimes(a);
        var r = Number(SHA512.HashData(prefix.Concat(message).ToArray())) % L;
        var bigR = BaseTimes(r);
        var k = Number(SHA512.HashData(bigR.Concat(publicKey).Concat(message).ToArray())) % L;
        return bigR.Concat(Bytes32((r + k * a) % L)).ToArray();
    }
}

/// The TEST release key of sdk/testdata/pro_manifest_vectors.json: it is pinned nowhere in the SDK and signs
/// nothing real. The fake download route signs its manifests with it, and the tests pin its public half through
/// <c>ProManifest.KeysForTests</c> (internal to the SDK; there is no public or environment way to pin a key).
internal static class TestReleaseKey
{
    private static readonly Lazy<JsonElement> Vectors = new(() =>
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("pro_manifest_vectors.json"))).RootElement.Clone());
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, string> Signed = new();

    public static string KeyId => Vectors.Value.GetProperty("key_id").GetString()!;
    public static string PublicKeyHex => Vectors.Value.GetProperty("public_key_hex").GetString()!;
    public static byte[] Seed => Convert.FromHexString(Vectors.Value.GetProperty("private_seed_hex").GetString()!);

    /// What the tests pin: the test key, and nothing else.
    public static IReadOnlyDictionary<string, string> Pins => new Dictionary<string, string> { [KeyId] = PublicKeyHex };

    /// Every test, and every child process a test starts, checks manifests against the test key. A test of the
    /// SDK as it ships sets <c>ProManifest.KeysForTests = null</c> for its own duration.
    [ModuleInitializer]
    internal static void Pin()
    {
        try { ProManifest.KeysForTests = Pins; }
        catch { /* no vectors file here: the PRO download tests then fail with unknown-key */ }
    }

    public static string Base64Url(byte[] data) => Convert.ToBase64String(data).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    /// base64url(payload) "." base64url(signature over the ASCII text of the first part).
    public static string Sign(string payloadJson) =>
        Signed.GetOrAdd(payloadJson, json =>
        {
            var body = Base64Url(Encoding.UTF8.GetBytes(json));
            return body + "." + Base64Url(TestEd25519.Sign(Seed, Encoding.ASCII.GetBytes(body)));
        });

    /// A signed manifest for one platform archive (docs/PRO-RELEASE-MANIFEST.md).
    public static string Manifest(string version, string revision, string platform, string asset, string sha256, long size,
                                  IReadOnlyDictionary<string, string> binaries) =>
        Sign(JsonSerializer.Serialize(new
        {
            v = 1, product = "clearcote-pro", version, revision, platform,
            archive = new { name = asset, sha256, size },
            binaries, key = KeyId, issued = "2026-11-26",
        }));
}
