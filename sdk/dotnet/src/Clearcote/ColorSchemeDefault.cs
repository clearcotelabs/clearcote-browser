using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// Leave <c>prefers-color-scheme</c> to the engine's persona.
/// </summary>
/// <remarks>
/// From engine r32 the persona decides the colour scheme a Clearcote browser reports (about one
/// persona in three is dark; <c>--fingerprint-color-scheme=light|dark</c> overrides), and every
/// surface follows it: CSS and <c>matchMedia</c>, the <c>Sec-CH-Prefers-Color-Scheme</c> request
/// header and the system colours of <c>color-scheme: light dark</c> content. Playwright, by default,
/// emulates <c>prefers-color-scheme: light</c> in every context it creates; that reaches the page but
/// not the <c>Critical-CH</c> restart a site can ask for, so a dark persona would tell the page
/// "light" and that request "dark". <see cref="ColorScheme.Null"/> turns the emulation off and the
/// three agree. Only for an engine that has the switch (an older engine keeps Playwright's default
/// exactly as before), and never over a colour scheme the caller set.
/// </remarks>
internal static class ColorSchemeDefault
{
    public const string Switch = "fingerprint-color-scheme";

    /// The context's <c>ColorScheme</c> option: the caller's, else no emulation on an engine that
    /// picks the colour scheme from the persona, else unset (Playwright's default).
    public static ColorScheme? Resolve(ColorScheme? requested, string? exe)
        => requested ?? (LaunchOpts.EngineSupportsSwitch(exe, Switch) ? ColorScheme.Null : null);
}
