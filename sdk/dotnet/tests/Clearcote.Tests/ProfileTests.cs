using System.Text.Json.Nodes;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// Saved personas (Node test/profile.test.ts, Python tests/test_profile.py) and their launch wiring: the
/// LocalProfile option, the merge under the caller's options, and the cloud/Docker refusals.
public class ProfileTests
{
    private static JsonObject Obj(string json) => (JsonObject)JsonNode.Parse(json)!;

    [Fact]
    public void Round_trips_name_and_options_through_save_and_load()
    {
        using var sb = new Sandbox();
        var dir = sb.TempHome();
        var path = Path.Combine(dir, "acct-1.json");
        new Profile("acct-1", Obj("""{"fingerprint":"acct-1","gpuRenderer":"ANGLE (Intel)","canvasBridge":{"url":"ws://127.0.0.1:9099","auth":"user:secret"}}""")).Save(path);
        var loaded = Profile.Load(path);
        Assert.Equal("acct-1", loaded.Name);
        Assert.True(JsonNode.DeepEquals(Obj("""{"fingerprint":"acct-1","gpuRenderer":"ANGLE (Intel)","canvasBridge":{"url":"ws://127.0.0.1:9099","auth":"user:secret"}}"""), loaded.Options));
        // the file is the Node/Python format: {"name", "options"}, indented, newline-terminated
        var text = File.ReadAllText(path);
        Assert.EndsWith("\n", text);
        Assert.Equal("acct-1", JsonNode.Parse(text)!["name"]!.GetValue<string>());
    }

    [Fact]
    public void Set_merges_and_chains()
    {
        var prof = new Profile("p").Set(Obj("""{"fingerprint":"s"}""")).Set(Obj("""{"brand":"Edge"}"""));
        Assert.True(JsonNode.DeepEquals(Obj("""{"fingerprint":"s","brand":"Edge"}"""), prof.Options));
    }

    [Fact]
    public void A_profile_from_LaunchOptions_holds_what_they_set_in_camelCase()
    {
        var prof = new Profile("p", new LaunchOptions
        {
            Fingerprint = "s", GpuVendor = "X", HardwareConcurrency = 8, Headless = false,
            CanvasBridge = new CanvasBridgeOptions { Url = "ws://h:1" }, Args = new[] { "--a" },
        });
        Assert.True(JsonNode.DeepEquals(
            Obj("""{"fingerprint":"s","gpuVendor":"X","hardwareConcurrency":8,"canvasBridge":{"url":"ws://h:1"},"headless":false,"args":["--a"]}"""),
            prof.Options), prof.Options.ToJsonString());
        // and back
        var o = prof.ToLaunchOptions();
        Assert.Equal("s", o.Fingerprint);
        Assert.Equal(8, o.HardwareConcurrency);
        Assert.Equal("ws://h:1", o.CanvasBridge!.Url);
        Assert.False(o.Headless);
        Assert.Equal(new[] { "--a" }, o.Args);
    }

    [Fact]
    public void Resolves_a_Profile_instance_or_a_path()
    {
        using var sb = new Sandbox();
        var dir = sb.TempHome();
        var prof = new Profile("p", Obj("""{"gpuVendor":"X"}"""));
        Assert.True(JsonNode.DeepEquals(Obj("""{"gpuVendor":"X"}"""), ((LocalProfile)prof).ResolveOptions()));
        var path = Path.Combine(dir, "p.json");
        prof.Save(path);
        Assert.True(JsonNode.DeepEquals(Obj("""{"gpuVendor":"X"}"""), ((LocalProfile)path).ResolveOptions()));
    }

    [Fact]
    public void ListProfiles_reads_CLEARCOTE_PROFILE_DIR()
    {
        using var sb = new Sandbox();
        var dir = sb.TempHome();
        sb.Env("CLEARCOTE_PROFILE_DIR", dir);
        Assert.Empty(Profile.ListProfiles());
        new Profile("b").Save();
        new Profile("a").Save();
        File.WriteAllText(Path.Combine(dir, "notes.txt"), "x");
        Assert.Equal(new[] { "a", "b" }, Profile.ListProfiles());
        Assert.Equal(Path.Combine(dir, "a.json"), new Profile("a").Path);
        sb.Env("CLEARCOTE_PROFILE_DIR", Path.Combine(dir, "missing"));
        Assert.Empty(Profile.ListProfiles());
    }

    [Fact]
    public void Loads_a_snake_case_profile_from_the_Python_SDK()
    {
        using var sb = new Sandbox();
        var dir = sb.TempHome();
        var path = Path.Combine(dir, "py.json");
        File.WriteAllText(path, """{"name":"py","options":{"fingerprint":"s","gpu_renderer":"ANGLE (Intel)","canvas_bridge":{"url":"ws://h:1"}}}""");
        var prof = Profile.Load(path);
        Assert.True(JsonNode.DeepEquals(Obj("""{"fingerprint":"s","gpuRenderer":"ANGLE (Intel)","canvasBridge":{"url":"ws://h:1"}}"""), prof.Options));
        var args = Fingerprint.Args(prof.ToLaunchOptions());
        Assert.Contains("--fingerprint-gpu-renderer=ANGLE (Intel)", args);
        Assert.Contains("--canvas-bridge-url=ws://h:1", args);
    }

    [Fact]
    public void Name_falls_back_to_the_file_name()
    {
        using var sb = new Sandbox();
        var dir = sb.TempHome();
        var path = Path.Combine(dir, "nameless.json");
        File.WriteAllText(path, """{"options":{"brand":"Edge"}}""");
        Assert.Equal("nameless", Profile.Load(path).Name);
    }

    [Fact]
    public void Rejects_an_unsafe_profile_name()
    {
        Assert.Throws<ArgumentException>(() => new Profile("..").Save());
        Assert.Throws<ArgumentException>(() => Profile.Load("a b"));
    }

    [Fact]
    public void Save_writes_a_private_file()
    {
        if (OperatingSystem.IsWindows()) return;   // POSIX modes only
        using var sb = new Sandbox();
        var dir = sb.TempHome();
        var path = new Profile("secret", Obj("""{"canvasBridge":{"auth":"u:p"}}""")).Save(Path.Combine(dir, "sub", "secret.json"));
        Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite, File.GetUnixFileMode(path));
        Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute, File.GetUnixFileMode(Path.GetDirectoryName(path)!));
    }

    // ── LocalProfile ─────────────────────────────────────────────────────────

    [Fact]
    public void LocalProfile_converts_from_a_name_auto_or_a_Profile()
    {
        LocalProfile auto = "auto";
        Assert.True(auto.IsAuto);
        Assert.Same(LocalProfile.Auto, auto);
        LocalProfile named = "acct-1";
        Assert.False(named.IsAuto);
        Assert.Equal("acct-1", named.Name);
        var p = new Profile("auto");   // a Profile instance is never "auto", whatever its name
        LocalProfile inst = p;
        Assert.False(inst.IsAuto);
        Assert.Same(p, inst.Saved);
    }

    [Fact]
    public void Profile_auto_on_a_local_launch_means_LocalProfile_auto()
    {
        Assert.True(ProfileAuto.LocalProfileOf(new LaunchOptions { Profile = "auto" })!.IsAuto);
        Assert.Null(ProfileAuto.LocalProfileOf(new LaunchOptions { Profile = "cookies" }));   // a cloud cookie store
        Assert.Null(ProfileAuto.LocalProfileOf(new LaunchOptions { Profile = new CloudProfile { Name = "auto", Persist = true } }));
        Assert.Equal("acct", ProfileAuto.LocalProfileOf(new LaunchOptions { LocalProfile = "acct", Profile = "auto" })!.Name);
    }

    [Fact]
    public async Task A_saved_profile_fills_what_the_caller_left_unset()
    {
        using var sb = new Sandbox();
        var dir = sb.TempHome();
        sb.Env("CLEARCOTE_PROFILE_DIR", dir);
        new Profile("acct", Obj("""{"fingerprint":"saved","brand":"Edge","gpu_vendor":"V","timezone":"Europe/Berlin","headless":false}""")).Save();
        var caller = new LaunchOptions { LocalProfile = "acct", Brand = "Chrome", Quiet = true };
        var o = await Clearcote.PrepareAsync(caller);
        Assert.Equal("saved", o.Fingerprint);
        Assert.Equal("Chrome", o.Brand);   // explicit options win
        Assert.Equal("V", o.GpuVendor);
        Assert.Equal("Europe/Berlin", o.Timezone);
        Assert.False(o.Headless);
        Assert.Null(caller.Fingerprint);   // the caller's object is never mutated
    }

    [Fact]
    public async Task A_Profile_instance_is_merged_the_same_way()
    {
        var o = await Clearcote.PrepareAsync(new LaunchOptions { LocalProfile = new Profile("x", Obj("""{"platform":"linux"}""")), Quiet = true });
        Assert.Equal("linux", o.Platform);
    }

    [Fact]
    public async Task Unknown_saved_options_are_named_and_skipped()
    {
        using var err = new StderrCapture();
        var o = await Clearcote.PrepareAsync(new LaunchOptions
        {
            LocalProfile = new Profile("x", Obj("""{"brand":"Edge","nodeOnlyThing":true,"hardwareConcurrency":"many","profile":"other","profile_select":{}}""")),
        });
        Assert.Equal("Edge", o.Brand);
        Assert.Null(o.HardwareConcurrency);
        // the selector keys are the caller's: dropped silently
        Assert.Contains("saved profile 'x': ignored nodeOnlyThing, hardwareConcurrency (", err.Text);
    }

    [Fact]
    public async Task Auto_is_never_loaded_as_a_saved_profile_name()
    {
        using var sb = new Sandbox();
        sb.Env("CLEARCOTE_PROFILE_DIR", Path.Combine(sb.TempHome(), "none"));
        // would throw FileNotFoundException for <dir>/auto.json if it reached Profile.Load
        var o = await Clearcote.PrepareAsync(new LaunchOptions { LocalProfile = "auto", Quiet = true });
        Assert.Null(o.FingerprintProfile);
        await Clearcote.PrepareAsync(new LaunchOptions { Profile = "auto", Quiet = true });
    }

    [Fact]
    public async Task A_missing_saved_profile_fails_before_anything_starts()
    {
        using var sb = new Sandbox();
        sb.Env("CLEARCOTE_PROFILE_DIR", sb.TempHome());
        await Assert.ThrowsAsync<FileNotFoundException>(() => Clearcote.PrepareAsync(new LaunchOptions { LocalProfile = "nope" }));
    }

    // ── classification: cloud and Docker refuse the local selector (Node: profileSelect is local-only,
    //    and a saved Profile / "auto" are refused by name) ─────────────────────

    [Fact]
    public void Cloud_refuses_LocalProfile_and_ProfileSelect()
    {
        Assert.Contains(CloudLaunch.LocalOnlyOptions, p => p.Name == nameof(LaunchOptions.LocalProfile));
        Assert.Contains(CloudLaunch.LocalOnlyOptions, p => p.Name == nameof(LaunchOptions.ProfileSelect));
        Assert.StartsWith("LocalProfile \"auto\" picks a local persona",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { LocalProfile = "auto" })).Message);
        Assert.StartsWith("a saved local Profile cannot be used for a cloud browser",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { LocalProfile = "acct" })).Message);
        Assert.Equal("ProfileSelect is not available for cloud browsers",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { ProfileSelect = new AutoOptions() })).Message);
        // the cloud's own Profile = "auto" refusal is unchanged
        Assert.Throws<ArgumentException>(() => Cloud.ProfileNode("auto"));
    }

    [Fact]
    public void Docker_refuses_LocalProfile_ProfileSelect_and_Profile_auto()
    {
        Assert.Contains(DockerLaunch.RefusedOptions, p => p.Name == nameof(LaunchOptions.LocalProfile));
        Assert.Contains(DockerLaunch.RefusedOptions, p => p.Name == nameof(LaunchOptions.ProfileSelect));
        Assert.StartsWith("LocalProfile is not available",
            Assert.Throws<ArgumentException>(() => DockerLaunch.ContainerEnv(new LaunchOptions { LocalProfile = "acct" })).Message);
        Assert.StartsWith("Profile \"auto\" is not available",
            Assert.Throws<ArgumentException>(() => DockerLaunch.ContainerEnv(new LaunchOptions { Profile = "auto" })).Message);
        DockerLaunch.ContainerEnv(new LaunchOptions { Profile = "cookies" });   // a cloud option: ignored there, as before
    }
}
