// The device id added to an old capture's WebGL renderer before it goes to the engine.
using System.IO.Compression;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Xunit;

namespace Clearcote.Tests;

public class AngleDeviceIdTests
{
    private const string Old = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)";
    private const string New = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti (0x00002782) Direct3D11 vs_5_0 ps_5_0, D3D11)";

    private static string Capture(string renderer = Old) =>
        "{\"webgl\":{\"webgl1\":{\"debug\":{\"UNMASKED_VENDOR_WEBGL\":\"Google Inc. (NVIDIA)\",\"UNMASKED_RENDERER_WEBGL\":\"" + renderer + "\"}}," +
        "\"webgl2\":{\"debug\":{\"UNMASKED_RENDERER_WEBGL\":\"" + renderer + "\"}}}," +
        "\"webgpu\":{\"high_performance\":{\"info\":{\"vendor\":\"nvidia\",\"architecture\":\"lovelace\"}}},\"hardware_concurrency\":16}";

    private static JsonObject Decode(string b64)
    {
        using var gz = new GZipStream(new MemoryStream(Convert.FromBase64String(b64)), CompressionMode.Decompress);
        using var sr = new StreamReader(gz, Encoding.UTF8);
        return (JsonObject)JsonNode.Parse(sr.ReadToEnd())!;
    }

    private static string R(JsonObject o, string ctx) => (string)o["webgl"]![ctx]!["debug"]!["UNMASKED_RENDERER_WEBGL"]!;

    [Fact]
    public void Adds_the_id_real_captures_print()
    {
        Assert.Equal(New, AngleDeviceId.WithDeviceId(Old));
        Assert.Equal(New, AngleDeviceId.WithDeviceId(Old, "lovelace"));
        Assert.Equal(New.Replace("Direct3D11 ", "Direct3D11on12 "), AngleDeviceId.WithDeviceId(Old.Replace("Direct3D11 ", "Direct3D11on12 ")));
    }

    [Fact]
    public void Leaves_alone_what_it_should()
    {
        Assert.Equal(New, AngleDeviceId.WithDeviceId(New));
        const string gl = "ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 4070 Ti/PCIe/SSE2, OpenGL 4.5.0)";
        Assert.Equal(gl, AngleDeviceId.WithDeviceId(gl));
        const string odd = "ANGLE (NVIDIA, NVIDIA GeForce Imaginary 9999 Direct3D11 vs_5_0 ps_5_0, D3D11)";
        Assert.Equal(odd, AngleDeviceId.WithDeviceId(odd));
    }

    [Fact]
    public void Generic_name_follows_the_chip_generation()
    {
        const string amd = "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)";
        Assert.Contains($"({AngleDeviceIds.ByArch["AMD|AMD Radeon(TM) Graphics|gcn-5"]}) Direct3D11", AngleDeviceId.WithDeviceId(amd, "gcn-5"));
        Assert.Contains($"({AngleDeviceIds.ByName["AMD|AMD Radeon(TM) Graphics"]}) Direct3D11", AngleDeviceId.WithDeviceId(amd, "none"));
        foreach (var id in AngleDeviceIds.ByArch.Values.Concat(AngleDeviceIds.ByName.Values))
            Assert.Matches(new Regex("^0x[0-9A-F]{8}$"), id);
    }

    [Fact]
    public void EncodeProfile_gives_the_engine_the_fixed_renderer()
    {
        var fromString = Decode(Fingerprint.EncodeProfile(Capture()));
        Assert.Equal(New, R(fromString, "webgl1"));
        Assert.Equal(New, R(fromString, "webgl2"));
        Assert.Equal(16, (int)fromString["hardware_concurrency"]!);

        var dir = Directory.CreateTempSubdirectory("cc-angle-");
        try
        {
            var f = Path.Combine(dir.FullName, "cap.json");
            File.WriteAllText(f, Capture());
            Assert.Equal(New, R(Decode(Fingerprint.EncodeProfile(f)), "webgl2"));
        }
        finally { dir.Delete(true); }

        var obj = JsonNode.Parse(Capture())!.AsObject();
        Assert.Equal(New, R(Decode(Fingerprint.EncodeProfile(obj)), "webgl1"));
        Assert.Equal(Old, R(obj, "webgl1")); // the caller's object is not touched
    }

    [Fact]
    public void Fine_capture_and_non_json_pass_through_byte_for_byte()
    {
        var fine = Encoding.UTF8.GetBytes(Capture(New));
        Assert.Same(fine, AngleDeviceId.CaptureForEngine(fine));
        var junk = Encoding.UTF8.GetBytes("not json");
        Assert.Same(junk, AngleDeviceId.CaptureForEngine(junk));
    }
}
