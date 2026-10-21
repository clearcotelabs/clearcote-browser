using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.ExceptionServices;
using Microsoft.Playwright;

namespace Clearcote;

/// What a launch's <see cref="LaunchOptions.Humanize"/> / <see cref="LaunchOptions.HumanizeOptions"/> /
/// <see cref="LaunchOptions.ShowCursor"/> ask for (Node's HumanizeLaunchOptions, Python's humanize= / show_cursor=):
/// the options every page gets attached with (null: humanize off), the cursor overlay, and the persona seed (the
/// fingerprint seed, so an identity moves the same way in every SDK and on every launch; null: a random persona).
///
/// C# cannot wrap <c>browser.newPage</c> the way Node and Python do, so a browser comes back as a
/// <see cref="DispatchProxy"/> whose NewPageAsync / NewContextAsync install on what they return, and a context is
/// hooked through its <c>Page</c> event, which also delivers every popup (window.open, target=_blank).
internal sealed record HumanizeLaunch(HumanizeOptions? Options, bool ShowCursor, object? Seed)
{
    /// <summary>Anything to install: humanize on, or the cursor overlay.</summary>
    public bool Active => Options is not null || ShowCursor;

    /// <summary>
    /// The launch's setting, validated (a bad <see cref="LaunchOptions.HumanizeOptions"/> throws before anything
    /// starts). Humanize = true, or HumanizeOptions set, turns it on; false turns it off. Unset it is OFF for a local
    /// or Docker launch, as in Node and Python, and ON for a cloud launch (cloud.ts: the SDK humanizes, and the
    /// session's own mouse humanizer is switched off).
    /// </summary>
    internal static HumanizeLaunch Of(LaunchOptions? o, bool cloud, object? seed = null)
    {
        o?.HumanizeOptions?.Validate();
        bool on = o?.Humanize ?? (cloud || o?.HumanizeOptions is not null);
        return new HumanizeLaunch(on ? o?.HumanizeOptions ?? new HumanizeOptions() : null, o?.ShowCursor == true, seed);
    }

    /// <summary>The browser, wrapped so every page and context it makes is humanized; its present contexts are
    /// hooked now (a browser connected over CDP already has one). Unchanged when there is nothing to install.</summary>
    internal IBrowser Wrap(IBrowser browser)
    {
        if (!Active) return browser;
        foreach (var c in browser.Contexts) Humanize.Install(c, this);
        var p = DispatchProxy.Create<IBrowser, BrowserProxy>();
        var bp = (BrowserProxy)(object)p;
        bp.Target = browser;
        bp.Hz = this;
        return p;
    }

    /// <summary>A launched browser: Playwright's own IBrowser (or the cloud / Docker proxy around it), except that
    /// NewPageAsync and NewContextAsync install the launch's humanize on what they return.</summary>
    public class BrowserProxy : DispatchProxy
    {
        internal IBrowser Target = null!;
        internal HumanizeLaunch Hz = null!;

        protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
        {
            ArgumentNullException.ThrowIfNull(targetMethod);
            object? r;
            try { r = targetMethod.Invoke(Target, args); }
            catch (TargetInvocationException e) when (e.InnerException is not null)
            {
                ExceptionDispatchInfo.Capture(e.InnerException).Throw();
                throw;
            }
            return targetMethod.Name switch
            {
                "NewPageAsync" when r is Task<IPage> page => AfterPageAsync(page),
                "NewContextAsync" when r is Task<IBrowserContext> ctx => AfterContextAsync(ctx),
                _ => r,
            };
        }

        private async Task<IPage> AfterPageAsync(Task<IPage> made)
        {
            var page = await made.ConfigureAwait(false);
            if (page is null) return page!;
            Humanize.AttachLaunched(page, Hz);
            // NewPageAsync makes its context inside Playwright: hook that one too, so a popup it opens is humanized.
            try { Humanize.Install(page.Context, Hz); }
            catch (Exception) { /* a page-like object without a context */ }
            return page;
        }

        private async Task<IBrowserContext> AfterContextAsync(Task<IBrowserContext> made)
        {
            var ctx = await made.ConfigureAwait(false);
            if (ctx is not null) Humanize.Install(ctx, Hz);
            return ctx!;
        }
    }
}

public static partial class Humanize
{
    /// <summary>
    /// The cursor overlay of <see cref="LaunchOptions.ShowCursor"/>: a red dot that follows real mousemove events (a
    /// visualization). An idempotent script, so it works both as an init script and evaluated on a loaded page.
    /// The same source as Node's <c>CURSOR_OVERLAY</c> and Python's.
    /// </summary>
    public const string CursorOverlay = """
        (() => {
          if (window.__clearcoteCursor) return; window.__clearcoteCursor = 1;
          const make = () => {
            if (document.getElementById('__clearcote_cursor')) return;
            const d = document.createElement('div'); d.id = '__clearcote_cursor';
            d.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:20px;margin:-10px 0 0 -10px;' +
              'border-radius:50%;border:2px solid #ff3b3b;background:rgba(255,59,59,.22);' +
              'box-shadow:0 0 10px rgba(255,59,59,.6);pointer-events:none;z-index:2147483647';
            (document.body || document.documentElement).appendChild(d);
          };
          if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make); else make();
          document.addEventListener('mousemove', (e) => {
            const d = document.getElementById('__clearcote_cursor');
            if (d) { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }
          }, true);
        })();
        """;

    // What each hooked context installs on its pages (one Page handler per context, whatever installs it again).
    private sealed class ContextHz
    {
        public HumanizeLaunch Hz = new(null, false, null);
    }

    private static readonly ConditionalWeakTable<IBrowserContext, ContextHz> HookedContexts = new();
    private static readonly ConditionalWeakTable<IPage, object> LaunchAttached = new();
    private static readonly ConditionalWeakTable<IPage, object> CursorPages = new();

    /// <summary>
    /// Attach a motor persona to every page of a context: the pages it has now and every page it opens later, popups
    /// included (window.open, a link with target=_blank), through its <c>Page</c> event. <paramref name="seed"/> as
    /// for <see cref="Attach(IPage, object?)"/>; every page gets the same seed, so the same persona. A page that
    /// already has a persona keeps it. Python <c>install_humanize_on_context</c>, Node <c>installHumanizeOnContext</c>.
    /// </summary>
    public static void Attach(IBrowserContext context, object? seed = null) => Attach(context, null, seed);

    /// <summary>
    /// <see cref="Attach(IBrowserContext, object?)"/> with the humanize options every page is attached with
    /// (<see cref="Attach(IPage, HumanizeOptions?, object?)"/>); null is <c>new HumanizeOptions()</c>. Hooking a
    /// context twice adds no second handler: the first options win, as in Node and Python.
    /// </summary>
    public static void Attach(IBrowserContext context, HumanizeOptions? options, object? seed = null)
    {
        ArgumentNullException.ThrowIfNull(context);
        var o = options ?? new HumanizeOptions();
        o.Validate();
        Install(context, new HumanizeLaunch(o, false, seed));
    }

    /// <summary>Hook <paramref name="context"/> for <paramref name="hz"/>: its present pages now, later ones (popups
    /// included) through its Page event, the cursor overlay as an init script.</summary>
    internal static void Install(IBrowserContext context, HumanizeLaunch hz)
    {
        if (!hz.Active) return;
        var holder = HookedContexts.GetValue(context, _ => new ContextHz());
        bool first, cursor;
        lock (holder)
        {
            first = !holder.Hz.Active;
            cursor = hz.ShowCursor && !holder.Hz.ShowCursor;   // one init script per context
            // a second install adds what the first did not ask for (the overlay, humanize); never replaces it
            holder.Hz = new HumanizeLaunch(holder.Hz.Options ?? hz.Options, holder.Hz.ShowCursor || hz.ShowCursor,
                holder.Hz.Options is null ? hz.Seed : holder.Hz.Seed);
        }
        if (cursor) _ = Quietly(() => context.AddInitScriptAsync(CursorOverlay));
        if (first) context.Page += (_, p) => AttachLaunched(p, holder.Hz);
        foreach (var p in context.Pages) AttachLaunched(p, holder.Hz);
    }

    /// <summary>One launched page: the cursor overlay (once per page) and, with humanize on, the persona and options
    /// (once: a page reached twice, or one the caller attached itself, keeps what it has).</summary>
    internal static void AttachLaunched(IPage? page, HumanizeLaunch hz)
    {
        if (page is null) return;
        if (hz.ShowCursor && CursorPages.TryAdd(page, new object()))
        {
            void Inject() => _ = Quietly(() => page.EvaluateAsync(CursorOverlay));
            Inject();
            page.Load += (_, _) => Inject();
            page.FrameNavigated += (_, f) => { if (f == page.MainFrame) Inject(); };
        }
        if (hz.Options is null) return;
        if (Options.TryGetValue(page, out _) || Personas.TryGetValue(page, out _)) return;
        if (!LaunchAttached.TryAdd(page, new object())) return;
        Attach(page, hz.Options, hz.Seed);
    }

    private static async Task Quietly(Func<Task> call)
    {
        try { await call().ConfigureAwait(false); }
        catch (Exception) { /* best effort: the page or context may be gone */ }
    }
}
