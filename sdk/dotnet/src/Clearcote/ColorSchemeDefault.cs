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
/// <para>
/// A browser the SDK CONNECTS to (the Clearcote Docker container, over CDP) gets the same default, and
/// also for the context(s) that exist already, which callers use directly
/// (<c>browser.Contexts[0].NewPageAsync()</c>): the emulation is per page, so there it is turned off on
/// each page as it opens (<c>EmulateMediaAsync</c> with <see cref="ColorScheme.Null"/>) and on the pages
/// open at connect time. Measured 2026-10-21 with the fingerprint audit: through Docker every persona
/// seed reported light while the same engine launched natively reports dark on about one seed in three.
/// </para>
/// </remarks>
internal static class ColorSchemeDefault
{
    public const string Switch = "fingerprint-color-scheme";

    /// The context's <c>ColorScheme</c> option: the caller's, else no emulation on an engine that
    /// picks the colour scheme from the persona, else unset (Playwright's default).
    public static ColorScheme? Resolve(ColorScheme? requested, string? exe)
        => requested ?? (LaunchOpts.EngineSupportsSwitch(exe, Switch) ? ColorScheme.Null : null);

    /// The option for a new page or context of a browser the SDK CONNECTED to (the Docker container):
    /// the caller's, else no emulation. No engine gate here: an engine too old to decide the scheme
    /// from the persona shows the container's (light) host theme either way.
    public static ColorScheme? Connected(ColorScheme? requested) => requested ?? ColorScheme.Null;

    /// An EXISTING context's pages (a connected browser's default context, which callers use directly:
    /// <c>browser.Contexts[0].NewPageAsync()</c>): the pages open now, and every page the context raises
    /// its <c>Page</c> event for, <c>NewPageAsync</c>'s included. A context the SDK did not create has
    /// no options to set, so the emulation is turned off per page instead. (Python and Node wrap the
    /// context's new_page; an <see cref="IBrowserContext"/> is Playwright's own object, so its event
    /// stands in: the emulation is sent before the caller's next call reaches the page.)
    public static async Task InstallOnContextAsync(IBrowserContext context)
    {
        context.Page += (sender, page) => _ = EmulationOffAsync(page);
        foreach (var page in context.Pages.ToList()) await EmulationOffAsync(page).ConfigureAwait(false);
    }

    /// Turn the page's colour-scheme emulation off; a page closed in the meantime is left alone.
    private static async Task EmulationOffAsync(IPage page)
    {
        try { await page.EmulateMediaAsync(new() { ColorScheme = ColorScheme.Null }).ConfigureAwait(false); }
        catch { /* closed already: nothing left to emulate */ }
    }
}
