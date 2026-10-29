using System.Runtime.CompilerServices;

namespace Clearcote.Tests;

/// geoip is on by default (0.66.0): suites that launch must not look this machine's IP up for real.
/// GeoipDefaultTests covers the default with a stubbed resolver (sdk/node test/helpers/no-geoip.ts).
internal static class NoGeoip
{
    [ModuleInitializer]
    internal static void Off()
    {
        if (Environment.GetEnvironmentVariable(GeoIp.GeoipEnv) is null) Environment.SetEnvironmentVariable(GeoIp.GeoipEnv, "0");
    }
}
