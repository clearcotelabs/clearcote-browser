using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

/// Evaluate humanize's DOM reads in an isolated JavaScript world, not the page's own.
///
/// <para><c>page.EvaluateAsync</c> runs in the page's main world: every DOM call it makes goes through
/// the page's prototypes, so a page that wraps <c>Document.prototype.querySelector</c> or reads
/// <c>activeElement</c> through a hooked getter sees each read, with a Playwright
/// <c>UtilityScript.evaluate</c> frame on its stack (measured on r28 with humanize on: 3 hook hits per
/// session; 0 with humanize off and on genuine Chrome).</para>
///
/// <para>An isolated world (CDP <c>Page.createIsolatedWorld</c>, the mechanism extensions' content
/// scripts use) shares the DOM but has its own JavaScript globals and prototypes, so the page cannot
/// observe what runs there. The world is created lazily per page and again after a navigation destroys
/// it. <see cref="EvaluateAsync"/> returns null when the read is impossible (no CDP session, page closed,
/// a navigation mid-call); callers fall back to their safe default, never to the page world.</para>
///
/// <para>Mirrors sdk/python/clearcote/_isolated.py and sdk/node/src/isolated.ts.</para>
public sealed class IsolatedWorld
{
    private static readonly ConditionalWeakTable<IPage, IsolatedWorld> Worlds = new();
    private readonly IPage _page;
    private ICDPSession? _cdp;
    private int? _ctx;

    private IsolatedWorld(IPage page) => _page = page;

    /// The page's isolated world (created on first use).
    public static IsolatedWorld For(IPage page) => Worlds.GetValue(page, p => new IsolatedWorld(p));

    /// Run <c>(fnSource)(arg)</c> (<paramref name="arg"/> JSON-serialisable) and return its JSON value,
    /// or null.
    public async Task<JsonElement?> EvaluateAsync(string fnSource, object? arg = null)
    {
        for (int attempt = 0; attempt < 2; attempt++)   // a stale context (navigation) is re-created once
        {
            try
            {
                _cdp ??= await _page.Context.NewCDPSessionAsync(_page).ConfigureAwait(false);
                if (_ctx is null)
                {
                    var tree = await _cdp.SendAsync("Page.getFrameTree").ConfigureAwait(false);
                    var frameId = tree!.Value.GetProperty("frameTree").GetProperty("frame").GetProperty("id").GetString();
                    var created = await _cdp.SendAsync("Page.createIsolatedWorld",
                        new Dictionary<string, object> { ["frameId"] = frameId! }).ConfigureAwait(false);
                    _ctx = created!.Value.GetProperty("executionContextId").GetInt32();
                }
                var r = await _cdp.SendAsync("Runtime.evaluate", new Dictionary<string, object>
                {
                    ["expression"] = $"({fnSource})({JsonSerializer.Serialize(arg)})",
                    ["contextId"] = _ctx.Value,
                    ["returnByValue"] = true,
                }).ConfigureAwait(false);
                if (r is not { } res || res.TryGetProperty("exceptionDetails", out _)) return null;
                return res.TryGetProperty("result", out var result) && result.TryGetProperty("value", out var value)
                    ? value.Clone()
                    : null;
            }
            catch (Exception)
            {
                _ctx = null;
            }
        }
        return null;
    }

    // The reads humanize makes, as functions of one JSON argument.
    internal const string Viewport = "() => [innerWidth, innerHeight]";
    // The platform the page believes (the persona's), for the wheel notch size.
    internal const string Platform = "() => { const d = navigator.userAgentData; return (d && d.platform) || navigator.platform || ''; }";
    internal const string FocusedRect = @"() => {
        const el = document.activeElement;
        if (!el || el === document.body || el === document.documentElement) return null;
        const b = el.getBoundingClientRect();
        return (b.width && b.height) ? { x: b.x, y: b.y, w: b.width, h: b.height } : null;
    }";
}
