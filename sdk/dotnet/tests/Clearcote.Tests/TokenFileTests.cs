using Xunit;

namespace Clearcote.Tests;

/// SC 283: a browser still open when the process ended kept its run-token file (clearcote-rt-*.tok), and a
/// process killed outright runs no exit hook at all. Live files are removed at ProcessExit; the next process
/// sweeps a killed one's.
public class TokenFileTests
{
    private const string Guid32 = "12390cc97546487c8321b3a52ee2ddc6";

    private static string NewDir()
    {
        var dir = Path.Combine(Path.GetTempPath(), "cc-toktest-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return dir;
    }

    [Fact]
    public void Name_carries_pid_and_host_and_exit_removal_takes_a_still_bound_file()
    {
        var set = new TokenFileSet();
        var lt = set.Bind("tok.sig");
        try
        {
            Assert.Matches($"^clearcote-rt-{Environment.ProcessId}-{TokenFileSet.HostTag()}-[0-9a-f]{{32}}\\.tok$",
                Path.GetFileName(lt.File));
            Assert.True(File.Exists(lt.File));
            TokenFileSet.RemoveLive(); // what ProcessExit runs
            Assert.False(File.Exists(lt.File));
        }
        finally { lt.Release(); } // still safe after the exit removal
    }

    [Fact]
    public void Sweep_removes_only_this_hosts_files_whose_process_is_gone()
    {
        var dir = NewDir();
        try
        {
            var tag = TokenFileSet.HostTag();
            var other = tag == "00000000" ? "11111111" : "00000000";
            var dead = $"clearcote-rt-999999-{tag}-{Guid32}.tok";
            var keep = new[]
            {
                $"clearcote-rt-{Environment.ProcessId}-{tag}-{Guid32}.tok", // this process
                $"clearcote-rt-999999-{other}-{Guid32}.tok",                // another host's (shared temp dir)
                $"clearcote-rt-{Guid32}.tok",                               // pre-fix name: no owner, never touched
            };
            foreach (var n in keep.Append(dead)) File.WriteAllText(Path.Combine(dir, n), "t");
            Assert.Equal(0, TokenFileSet.SweepDead(dir, _ => true)); // a live (or reused) pid keeps its file
            Assert.Equal(1, TokenFileSet.SweepDead(dir, _ => false));
            Assert.Equal(keep.OrderBy(n => n), Directory.GetFiles(dir).Select(Path.GetFileName).OrderBy(n => n));
        }
        finally { Directory.Delete(dir, true); }
    }
}
