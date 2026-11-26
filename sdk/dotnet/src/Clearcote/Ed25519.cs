using System.Numerics;
using System.Security.Cryptography;

namespace Clearcote;

/// Ed25519 signature VERIFICATION (RFC 8032, section 5.1.7), for the signed PRO release manifest. .NET 8 has no
/// Ed25519 in its base library and the SDK takes no package for it, so the check is done here with BigInteger.
/// It only ever handles public data (a pinned public key, a manifest, its signature), so it does not need to run
/// in constant time, and it cannot sign.
internal static class Ed25519
{
    // The field prime p = 2^255 - 19 and the order L of the base point.
    private static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
    private static readonly BigInteger L = BigInteger.Pow(2, 252) + BigInteger.Parse("27742317777372353535851937790883648493");
    // The curve is -x^2 + y^2 = 1 + d x^2 y^2 with d = -121665/121666.
    private static readonly BigInteger D = Mod(-121665 * BigInteger.ModPow(121666, P - 2, P));
    // A square root of -1, used when recovering x from y.
    private static readonly BigInteger SqrtM1 = BigInteger.ModPow(2, (P - 1) / 4, P);

    /// A curve point in extended coordinates: x = X/Z, y = Y/Z, T = XY/Z.
    internal readonly record struct Point(BigInteger X, BigInteger Y, BigInteger Z, BigInteger T);

    private static readonly Point Identity = new(0, 1, 1, 0);
    // The base point B: y = 4/5, x even. Its encoding is 0x58 followed by 31 bytes 0x66.
    private static readonly Point B = Decode(Convert.FromHexString("58" + new string('6', 62)))!.Value;

    private static BigInteger Mod(BigInteger v)
    {
        var r = v % P;
        return r.Sign < 0 ? r + P : r;
    }

    /// Decode a 32-byte point (RFC 8032, 5.1.3): 255 bits of y, little-endian, and the low bit of x in the top
    /// bit. Null when it is not 32 bytes, y is not below p, or no x on the curve goes with that y.
    internal static Point? Decode(ReadOnlySpan<byte> bytes)
    {
        if (bytes.Length != 32) return null;
        var xIsOdd = (bytes[31] & 0x80) != 0;
        var y = new BigInteger(bytes, isUnsigned: true) & ((BigInteger.One << 255) - 1);
        if (y >= P) return null;

        // x^2 = u/v with u = y^2 - 1 and v = d y^2 + 1; the candidate root is u v^3 (u v^7)^((p-5)/8).
        var yy = y * y % P;
        var u = Mod(yy - 1);
        var v = Mod(D * yy + 1);
        var v3 = v * v % P * v % P;
        var v7 = v3 * v3 % P * v % P;
        var x = u * v3 % P * BigInteger.ModPow(u * v7 % P, (P - 5) / 8, P) % P;
        var vxx = v * x % P * x % P;
        if (vxx != u)
        {
            if (vxx != Mod(-u)) return null; // u/v is not a square: no such point
            x = x * SqrtM1 % P;
        }
        if (x.IsZero && xIsOdd) return null;
        if (x.IsEven == xIsOdd) x = P - x;
        return new Point(x, y, 1, x * y % P);
    }

    /// Point addition (RFC 8032, 5.1.4). The formula is complete on this curve, so it also doubles a point.
    private static Point Add(Point p, Point q)
    {
        var a = Mod((p.Y - p.X) * (q.Y - q.X));
        var b = Mod((p.Y + p.X) * (q.Y + q.X));
        var c = Mod(p.T * 2 * D % P * q.T);
        var d = Mod(p.Z * 2 * q.Z);
        BigInteger e = b - a, f = d - c, g = d + c, h = b + a;
        return new Point(Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h));
    }

    /// k times the point p, by double-and-add from the lowest bit of k.
    private static Point Multiply(BigInteger k, Point p)
    {
        var sum = Identity;
        for (; !k.IsZero; k >>= 1)
        {
            if (!k.IsEven) sum = Add(sum, p);
            p = Add(p, p);
        }
        return sum;
    }

    private static bool Same(Point p, Point q) =>
        Mod(p.X * q.Z - q.X * p.Z).IsZero && Mod(p.Y * q.Z - q.Y * p.Z).IsZero;

    /// True when <paramref name="signature"/> (64 bytes: R, then S) is a valid Ed25519 signature of
    /// <paramref name="message"/> under the 32-byte <paramref name="publicKey"/>.
    ///
    /// False for a key or signature of the wrong length, a public key or R that is not the encoding of a curve
    /// point, and an S that is not below the group order L (a second encoding of an existing signature).
    public static bool Verify(ReadOnlySpan<byte> publicKey, ReadOnlySpan<byte> message, ReadOnlySpan<byte> signature)
    {
        if (publicKey.Length != 32 || signature.Length != 64) return false;
        var a = Decode(publicKey);
        var r = Decode(signature[..32]);
        if (a is null || r is null) return false;
        var s = new BigInteger(signature[32..], isUnsigned: true);
        if (s >= L) return false;

        // k = SHA-512(R || A || message), read as a little-endian number.
        using var sha = IncrementalHash.CreateHash(HashAlgorithmName.SHA512);
        sha.AppendData(signature[..32]);
        sha.AppendData(publicKey);
        sha.AppendData(message);
        var k = new BigInteger(sha.GetHashAndReset(), isUnsigned: true) % L;

        // The signature is valid when [S]B = R + [k]A.
        return Same(Multiply(s, B), Add(r.Value, Multiply(k, a.Value)));
    }
}
