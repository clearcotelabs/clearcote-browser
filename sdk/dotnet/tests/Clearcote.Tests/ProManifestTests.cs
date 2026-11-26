using System.Diagnostics;
using System.Numerics;
using System.Text;
using System.Text.Json;
using Xunit;
using Xunit.Abstractions;

namespace Clearcote.Tests;

// The signed PRO release manifest (docs/PRO-RELEASE-MANIFEST.md): the Ed25519 verifier the SDK carries, and the
// manifest check itself against sdk/testdata/pro_manifest_vectors.json, the file the Python and Node suites read
// too, so the three SDKs cannot drift apart. The test key of that file is pinned for the tests by
// TestReleaseKey (ProManifest.KeysForTests); the SDK itself pins no key yet.
public class ProManifestTests
{
    private static readonly JsonElement Vec =
        JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("pro_manifest_vectors.json"))).RootElement.Clone();

    private readonly ITestOutputHelper _out;
    public ProManifestTests(ITestOutputHelper output) => _out = output;

    private static byte[] Hex(string hex) => Convert.FromHexString(hex);

    private static (byte[] Key, byte[] Message, byte[] Signature) Rfc(int i)
    {
        var v = Vec.GetProperty("rfc8032")[i];
        return (Hex(v.GetProperty("public_key_hex").GetString()!), Hex(v.GetProperty("message_hex").GetString()!),
                Hex(v.GetProperty("signature_hex").GetString()!));
    }

    private static byte[] Changed(byte[] data, int at, byte mask = 0x01)
    {
        var copy = (byte[])data.Clone();
        copy[at] ^= mask;
        return copy;
    }

    // ── the Ed25519 verifier ─────────────────────────────────────────────────

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(2)]
    public void An_RFC_8032_signature_verifies_and_fails_when_one_byte_changes(int i)
    {
        var (key, message, signature) = Rfc(i);
        Assert.True(Ed25519.Verify(key, message, signature));
        for (var at = 0; at < signature.Length; at++)
            Assert.False(Ed25519.Verify(key, message, Changed(signature, at)), $"signature byte {at}");
        for (var at = 0; at < key.Length; at++)
            Assert.False(Ed25519.Verify(Changed(key, at), message, signature), $"public key byte {at}");
        for (var at = 0; at < message.Length; at++)
            Assert.False(Ed25519.Verify(key, Changed(message, at), signature), $"message byte {at}");
        Assert.False(Ed25519.Verify(key, message.Append((byte)0).ToArray(), signature)); // one byte more
    }

    [Fact]
    public void A_key_or_signature_of_the_wrong_length_is_refused()
    {
        var (key, message, signature) = Rfc(1);
        Assert.False(Ed25519.Verify(key[..31], message, signature));
        Assert.False(Ed25519.Verify(key.Append((byte)0).ToArray(), message, signature));
        Assert.False(Ed25519.Verify(key, message, signature[..63]));
        Assert.False(Ed25519.Verify(key, message, signature.Append((byte)0).ToArray()));
        Assert.False(Ed25519.Verify(Array.Empty<byte>(), message, Array.Empty<byte>()));
    }

    [Fact]
    public void An_S_that_is_not_below_the_group_order_is_refused()
    {
        // S + L names the same point as S, so a verifier that only reduces S accepts this second encoding of a
        // valid signature. RFC 8032 requires S < L.
        var order = BigInteger.Pow(2, 252) + BigInteger.Parse("27742317777372353535851937790883648493");
        var (key, message, signature) = Rfc(2);
        var s = new BigInteger(signature.AsSpan(32), isUnsigned: true) + order;
        Assert.True(s < BigInteger.Pow(2, 256));
        var bytes = new byte[32];
        s.ToByteArray(isUnsigned: true).CopyTo(bytes, 0);
        Assert.False(Ed25519.Verify(key, message, signature[..32].Concat(bytes).ToArray()));
    }

    [Fact]
    public void A_public_key_or_R_that_is_not_the_encoding_of_a_curve_point_is_refused()
    {
        var one = Hex("01" + new string('0', 62));            // y = 1, x = 0: the neutral point
        var basePoint = Hex("58" + new string('6', 62));
        var zero = new byte[32];
        var message = Encoding.ASCII.GetBytes("any message");

        // Controls: with the neutral point as the public key, [S]B = R + [k]A holds for R = [S]B whatever the
        // message. That is how the cases below reach the decoding of the key and of R, and nothing else.
        Assert.True(Ed25519.Verify(one, message, basePoint.Concat(Hex("01" + new string('0', 62))).ToArray())); // S = 1, R = B
        Assert.True(Ed25519.Verify(one, message, one.Concat(zero).ToArray()));                                  // S = 0, R = neutral

        // y = p + 1 is a second, non-canonical spelling of y = 1.
        var onePlusP = Hex("ee" + new string('f', 60) + "7f");
        Assert.Null(Ed25519.Decode(onePlusP));
        Assert.False(Ed25519.Verify(onePlusP, message, basePoint.Concat(Hex("01" + new string('0', 62))).ToArray()));
        Assert.False(Ed25519.Verify(one, message, onePlusP.Concat(zero).ToArray()));
        // x = 0 with the sign bit set is not an encoding either.
        var minusZero = Hex("01" + new string('0', 60) + "80");
        Assert.Null(Ed25519.Decode(minusZero));
        Assert.False(Ed25519.Verify(minusZero, message, basePoint.Concat(Hex("01" + new string('0', 62))).ToArray()));
        Assert.False(Ed25519.Verify(one, message, minusZero.Concat(zero).ToArray()));

        // A y with no x on the curve (about half of all y): the first such y from 2 up.
        byte[]? offCurve = null;
        for (byte y = 2; y < 50 && offCurve is null; y++)
        {
            var enc = new byte[32];
            enc[0] = y;
            if (Ed25519.Decode(enc) is null) offCurve = enc;
        }
        Assert.NotNull(offCurve);
        Assert.False(Ed25519.Verify(offCurve!, message, one.Concat(zero).ToArray()));
        Assert.False(Ed25519.Verify(one, message, offCurve!.Concat(zero).ToArray()));

        Assert.NotNull(Ed25519.Decode(basePoint));
        Assert.Null(Ed25519.Decode(basePoint[..31]));
    }

    [Fact]
    public void The_test_signer_agrees_with_the_vectors_file()
    {
        // The vectors were signed by Node's crypto (OpenSSL); Ed25519 signatures are deterministic, so the
        // test-only signer here must give the same public key and the same bytes.
        Assert.Equal(TestReleaseKey.PublicKeyHex, Convert.ToHexString(TestEd25519.PublicKey(TestReleaseKey.Seed)).ToLowerInvariant());
        var manifest = Case("valid windows").GetProperty("meta").GetProperty("manifest").GetString()!;
        var body = manifest.Split('.')[0];
        Assert.Equal(manifest, body + "." + TestReleaseKey.Base64Url(TestEd25519.Sign(TestReleaseKey.Seed, Encoding.ASCII.GetBytes(body))));
    }

    [Fact]
    public void One_verification_takes_well_under_100_ms()
    {
        var (key, message, signature) = Rfc(2);
        Assert.True(Ed25519.Verify(key, message, signature)); // warm up
        var times = new List<double>();
        for (var i = 0; i < 25; i++)
        {
            var sw = Stopwatch.StartNew();
            Assert.True(Ed25519.Verify(key, message, signature));
            times.Add(sw.Elapsed.TotalMilliseconds);
        }
        times.Sort();
        _out.WriteLine($"Ed25519.Verify: min {times[0]:F2} ms, median {times[times.Count / 2]:F2} ms, max {times[^1]:F2} ms (25 runs)");
        // The fastest run: the least disturbed by whatever else the machine is doing.
        Assert.True(times[0] < 100, $"fastest of 25 verifications took {times[0]:F1} ms");
    }

    // ── the manifest check, against the shared vectors ───────────────────────

    private static JsonElement Case(string name) =>
        Vec.GetProperty("cases").EnumerateArray().Single(c => c.GetProperty("name").GetString() == name);

    public static IEnumerable<object[]> CaseNames() =>
        Vec.GetProperty("cases").EnumerateArray().Select(c => new object[] { c.GetProperty("name").GetString()! });

    private static string? Text(JsonElement c, string name) =>
        c.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    [Theory]
    [MemberData(nameof(CaseNames))]
    public void A_shared_vector_case_gives_the_expected_result(string name)
    {
        var c = Case(name);
        var expect = c.GetProperty("expect").GetString()!;
        JsonElement Run() => ProManifest.Verify(c.GetProperty("meta"), c.GetProperty("platform").GetString()!,
                                                Text(c, "want_revision"), Text(c, "want_version"));
        if (expect == "ok")
        {
            var payload = Run();
            Assert.Equal("clearcote-pro", payload.GetProperty("product").GetString());
            Assert.Equal(c.GetProperty("meta").GetProperty("revision").GetString(), payload.GetProperty("revision").GetString());
            return;
        }
        var e = Assert.Throws<ProManifestException>(() => { Run(); });
        Assert.Equal(expect, e.Reason);
        Assert.StartsWith($"Clearcote Pro release manifest check failed ({expect}): ", e.Message);
        Assert.EndsWith(". Refusing to use this build.", e.Message);
    }

    [Fact]
    public void The_vectors_cover_every_reason_code()
    {
        var seen = Vec.GetProperty("cases").EnumerateArray().Select(c => c.GetProperty("expect").GetString()!).ToHashSet();
        Assert.Equal(new[] { "archive", "binary", "malformed", "missing", "ok", "platform", "product", "revision", "signature", "unknown-key", "version" },
                     seen.OrderBy(s => s, StringComparer.Ordinal));
        Assert.Equal(TestReleaseKey.KeyId, Assert.Single(TestReleaseKey.Pins).Key);
    }

    [Fact]
    public void Without_a_pinned_key_even_a_correct_manifest_is_refused()
    {
        // The SDK as it ships: only Release.ProReleaseKeys counts, and the test key is not in it.
        var c = Case("valid windows");
        var pinned = ProManifest.KeysForTests;
        try
        {
            ProManifest.KeysForTests = null;
            Assert.DoesNotContain(TestReleaseKey.KeyId, Release.ProReleaseKeys.Keys);
            var e = Assert.Throws<ProManifestException>(() => { ProManifest.Verify(c.GetProperty("meta"), "windows"); });
            Assert.Equal("unknown-key", e.Reason);
        }
        finally { ProManifest.KeysForTests = pinned; }
        Assert.Equal("r68", ProManifest.Verify(c.GetProperty("meta"), "windows").GetProperty("revision").GetString());
    }

    [Fact]
    public void A_pinned_key_that_is_not_the_signers_fails_the_signature_not_the_key_lookup()
    {
        var c = Case("valid windows");
        var pinned = ProManifest.KeysForTests;
        try
        {
            // the same key id, pinned to another key (RFC 8032 test 1's), and to text that is not a key at all
            foreach (var wrong in new[] { "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "not hex", "" })
            {
                ProManifest.KeysForTests = new Dictionary<string, string> { [TestReleaseKey.KeyId] = wrong };
                Assert.Equal("signature", Assert.Throws<ProManifestException>(() => { ProManifest.Verify(c.GetProperty("meta"), "windows"); }).Reason);
            }
        }
        finally { ProManifest.KeysForTests = pinned; }
    }

    [Fact]
    public void Every_release_key_pinned_in_the_SDK_is_a_usable_Ed25519_public_key()
    {
        // Nothing to check while the map is empty; it guards the day the release key is pinned.
        foreach (var (id, hex) in Release.ProReleaseKeys)
        {
            Assert.False(string.IsNullOrWhiteSpace(id));
            Assert.Matches("^[0-9a-f]{64}$", hex);
            Assert.NotNull(Ed25519.Decode(Hex(hex)));
        }
        // read-only: a caller cannot add a key by casting the map back to a dictionary
        Assert.False(Release.ProReleaseKeys is Dictionary<string, string>);
        Assert.Throws<NotSupportedException>(() => ((IDictionary<string, string>)Release.ProReleaseKeys).Add("k", new string('0', 64)));
    }

    [Theory]
    [InlineData(null, null, null)]
    [InlineData("", null, null)]
    [InlineData("latest", null, null)]
    [InlineData("158", null, null)]
    [InlineData("0.0.0", null, null)]
    [InlineData("r68", "r68", null)]
    [InlineData("  R68 ", "r68", null)]
    [InlineData("r068", "r068", null)]
    [InlineData("158.0.8127.36", null, "158.0.8127.36")]
    [InlineData("158.0.8127.36-r68", "r68", "158.0.8127.36")]
    [InlineData("158.0.8127.36-R68", "r68", "158.0.8127.36")]
    [InlineData("pro-158.0.8127.36-r68", "r68", "158.0.8127.36")]
    public void A_selector_pins_the_revision_and_the_version_it_names(string? selector, string? revision, string? version) =>
        Assert.Equal((revision, version), Download.ProPins(selector));
}
