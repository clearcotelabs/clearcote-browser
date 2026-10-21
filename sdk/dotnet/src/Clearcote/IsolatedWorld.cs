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
    private string? _frame;

    private IsolatedWorld(IPage page) => _page = page;

    /// The page's isolated world (created on first use).
    public static IsolatedWorld For(IPage page) => Worlds.GetValue(page, p => new IsolatedWorld(p));

    private async Task EnsureAsync()
    {
        _cdp ??= await _page.Context.NewCDPSessionAsync(_page).ConfigureAwait(false);
        if (_ctx is null)
        {
            var tree = await _cdp.SendAsync("Page.getFrameTree").ConfigureAwait(false);
            _frame = tree!.Value.GetProperty("frameTree").GetProperty("frame").GetProperty("id").GetString();
            var created = await _cdp.SendAsync("Page.createIsolatedWorld",
                new Dictionary<string, object> { ["frameId"] = _frame! }).ConfigureAwait(false);
            _ctx = created!.Value.GetProperty("executionContextId").GetInt32();
        }
    }

    private static JsonElement? Value(JsonElement? r)
    {
        if (r is not { } res || res.ValueKind != JsonValueKind.Object || res.TryGetProperty("exceptionDetails", out _))
            return null;
        return res.TryGetProperty("result", out var result) && result.ValueKind == JsonValueKind.Object
               && result.TryGetProperty("value", out var value)
            ? value.Clone()
            : null;
    }

    /// Run <c>(fnSource)(arg)</c> (<paramref name="arg"/> JSON-serialisable) and return its JSON value,
    /// or null.
    public async Task<JsonElement?> EvaluateAsync(string fnSource, object? arg = null)
    {
        for (int attempt = 0; attempt < 2; attempt++)   // a stale context (navigation) is re-created once
        {
            try
            {
                await EnsureAsync().ConfigureAwait(false);
                var r = await _cdp!.SendAsync("Runtime.evaluate", new Dictionary<string, object>
                {
                    ["expression"] = $"({fnSource})({JsonSerializer.Serialize(arg)})",
                    ["contextId"] = _ctx!.Value,
                    ["returnByValue"] = true,
                }).ConfigureAwait(false);
                return Value(r);
            }
            catch (Exception)
            {
                _ctx = null;
            }
        }
        return null;
    }

    /// As <see cref="EvaluateAsync"/>, awaiting the promise the function returns (the engine route's frame wait).
    internal async Task<JsonElement?> EvaluatePromiseAsync(string fnSource, object? arg = null)
    {
        for (int attempt = 0; attempt < 2; attempt++)   // a stale context (navigation) is re-created once
        {
            try
            {
                await EnsureAsync().ConfigureAwait(false);
                var r = await _cdp!.SendAsync("Runtime.evaluate", new Dictionary<string, object>
                {
                    ["expression"] = $"({fnSource})({JsonSerializer.Serialize(arg)})",
                    ["contextId"] = _ctx!.Value,
                    ["returnByValue"] = true,
                    ["awaitPromise"] = true,
                }).ConfigureAwait(false);
                return Value(r);
            }
            catch (Exception)
            {
                _ctx = null;
            }
        }
        return null;
    }

    /// A raw CDP call on this page's session (DOM reads that run no script), or null when it fails.
    internal async Task<JsonElement?> SendAsync(string method, Dictionary<string, object>? args = null)
    {
        try
        {
            _cdp ??= await _page.Context.NewCDPSessionAsync(_page).ConfigureAwait(false);
            var r = await _cdp.SendAsync(method, args ?? new Dictionary<string, object>()).ConfigureAwait(false);
            return r?.Clone();
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// The main frame's (scrollX, scrollY), read in the isolated world; (0, 0) if unknown.
    internal async Task<(double X, double Y)> ScrollAsync()
    {
        var s = await EvaluateAsync(Scroll).ConfigureAwait(false);
        return s is { ValueKind: JsonValueKind.Array } a && a.GetArrayLength() == 2
               && a[0].ValueKind == JsonValueKind.Number && a[1].ValueKind == JsonValueKind.Number
            ? (a[0].GetDouble(), a[1].GetDouble())
            : (0.0, 0.0);
    }

    // DOM.getNodeForLocation hit-tests in DOCUMENT coordinates (the viewport point plus the scroll
    // offset); Playwright's boxes, CDP's box model and the mouse are all VIEWPORT coordinates. Measured
    // on r37: a field scrolled 547 px down answered "No node found" at its viewport centre and the input
    // itself at centre + scroll.
    //
    // No ignorePointerEventsNone: in CDP `true` means "hit-test pointer-events:none elements TOO", so a
    // decorative layer with pointer-events:none over a control was returned instead of the control and every
    // form route fell back to stock Playwright (SC 209: 6/6 untrusted change events). The default (false)
    // skips such layers, exactly as a real press does.
    internal static Dictionary<string, object> NodeAtParams(double x, double y, (double X, double Y)? scroll)
    {
        var (sx, sy) = scroll ?? (0, 0);
        return new Dictionary<string, object>
        {
            // Math.Round's default (to even) is Python's round().
            ["x"] = (int)Math.Round(x + sx), ["y"] = (int)Math.Round(y + sy),
            ["includeUserAgentShadowDOM"] = false,
        };
    }

    /// The frame directly under <paramref name="main"/> that contains <paramref name="frameId"/> (itself for
    /// a direct child), from a Page.getFrameTree answer's <c>frameTree</c>; null when it is not below the main frame.
    internal static string? TopChildFrame(JsonElement tree, string frameId, string main)
    {
        var parents = new Dictionary<string, string?>();
        void Walk(JsonElement t, string? parent)
        {
            if (t.ValueKind != JsonValueKind.Object || !t.TryGetProperty("frame", out var f)
                || f.ValueKind != JsonValueKind.Object || !f.TryGetProperty("id", out var idEl)
                || idEl.GetString() is not { Length: > 0 } id)
                return;
            parents[id] = parent;
            if (t.TryGetProperty("childFrames", out var kids) && kids.ValueKind == JsonValueKind.Array)
                foreach (var k in kids.EnumerateArray()) Walk(k, id);
        }
        Walk(tree, null);
        var fid = frameId;
        for (int n = 0; n < 64; n++)
        {
            if (!parents.TryGetValue(fid, out var up) || up is null) return null;
            if (up == main) return fid;
            fid = up;
        }
        return null;
    }

    /// backendNodeId of the MAIN-FRAME element at viewport point (x, y) -- for a point inside a child frame,
    /// the &lt;iframe&gt; element that holds it --, or null. <paramref name="scroll"/>:
    /// the page's (scrollX, scrollY) if the caller already has it (read here otherwise).
    ///
    /// <para>CDP's hit test (DOM.getNodeForLocation) runs in the renderer with no script at all, so this is
    /// how humanize finds the element a Playwright locator means -- GetByRole, GetByLabel, <c>&gt;&gt; nth=</c>,
    /// Filter() -- without document.querySelector, which cannot parse those selectors.</para>
    internal async Task<int?> NodeAtAsync(double x, double y, (double X, double Y)? scroll = null)
    {
        try
        {
            await EnsureAsync().ConfigureAwait(false);
        }
        catch (Exception)
        {
            _ctx = null;
            return null;
        }
        var hit = await SendAsync("DOM.getNodeForLocation",
            NodeAtParams(x, y, scroll ?? await ScrollAsync().ConfigureAwait(false))).ConfigureAwait(false);
        if (hit is not { ValueKind: JsonValueKind.Object } h || !h.TryGetProperty("backendNodeId", out var id)
            || id.ValueKind != JsonValueKind.Number)
            return null;
        if (h.TryGetProperty("frameId", out var f) && f.ValueKind == JsonValueKind.String
            && !string.IsNullOrEmpty(f.GetString()) && _frame is not null && f.GetString() != _frame)
            return await OwnerInMainAsync(f.GetString()!).ConfigureAwait(false);
        return id.GetInt32();
    }

    /// A point inside a child frame: the &lt;iframe&gt; element in the MAIN document that holds it (through
    /// every nesting level), or null. To the main document a press there lands on that element: a control the
    /// iframe covers is covered, a target that is (or contains) the iframe is reached. This replaced the only
    /// Playwright trial humanize still ran (it moved the pointer and pressed, and page listeners registered
    /// first saw it).
    private async Task<int?> OwnerInMainAsync(string frameId)
    {
        var tree = await SendAsync("Page.getFrameTree").ConfigureAwait(false);
        if (_frame is null || tree is not { ValueKind: JsonValueKind.Object } t || !t.TryGetProperty("frameTree", out var ft))
            return null;
        if (TopChildFrame(ft, frameId, _frame) is not { } top) return null;
        var own = await SendAsync("DOM.getFrameOwner", new Dictionary<string, object> { ["frameId"] = top }).ConfigureAwait(false);
        return own is { ValueKind: JsonValueKind.Object } o && o.TryGetProperty("backendNodeId", out var b)
               && b.ValueKind == JsonValueKind.Number ? b.GetInt32() : null;
    }

    /// Run <paramref name="fnSource"/> -- a <c>function (arg) { ... }</c> whose <c>this</c> is the node --
    /// in the isolated world and return its JSON value, or null.
    internal async Task<JsonElement?> CallOnNodeAsync(int backendNodeId, string fnSource, object? arg = null)
    {
        for (int attempt = 0; attempt < 2; attempt++)
        {
            try
            {
                await EnsureAsync().ConfigureAwait(false);
                var resolved = await _cdp!.SendAsync("DOM.resolveNode", new Dictionary<string, object>
                {
                    ["backendNodeId"] = backendNodeId, ["executionContextId"] = _ctx!.Value,
                }).ConfigureAwait(false);
                var objectId = resolved!.Value.GetProperty("object").GetProperty("objectId").GetString()!;
                try
                {
                    var r = await _cdp.SendAsync("Runtime.callFunctionOn", new Dictionary<string, object>
                    {
                        ["objectId"] = objectId,
                        ["functionDeclaration"] = fnSource,
                        ["arguments"] = new object[] { new Dictionary<string, object?> { ["value"] = arg } },
                        ["returnByValue"] = true,
                    }).ConfigureAwait(false);
                    return Value(r);
                }
                finally
                {
                    try
                    {
                        await _cdp.SendAsync("Runtime.releaseObject",
                            new Dictionary<string, object> { ["objectId"] = objectId }).ConfigureAwait(false);
                    }
                    catch (Exception) { /* best effort */ }
                }
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
    // Two animation frames, at most `ms` (remembered per document): the engine walks without waiting for the page,
    // and what it walks into a page that has not drawn yet is lost (a fresh headless browser's first page, SC 216).
    internal const string Frames = @"(ms) => window.__ccFramed ? true : new Promise((done) => {
  const t = setTimeout(() => { window.__ccFramed = 1; done(false); }, ms);
  requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(t); window.__ccFramed = 1; done(true); }));
})";
    // The platform the page believes (the persona's), for the wheel notch size.
    internal const string Platform = "() => { const d = navigator.userAgentData; return (d && d.platform) || navigator.platform || ''; }";
    internal const string FocusedRect = @"() => {
        const el = document.activeElement;
        if (!el || el === document.body || el === document.documentElement) return null;
        const b = el.getBoundingClientRect();
        return (b.width && b.height) ? { x: b.x, y: b.y, w: b.width, h: b.height } : null;
    }";

    // The sources below are copied verbatim from sdk/python/clearcote/_isolated.py.
    internal const string Scroll = "() => [scrollX, scrollY]";

    // The value of whatever has focus (input/textarea value, or a contenteditable's text): how the typing
    // path checks that a fat-finger key actually landed before it deletes anything.
    internal const string ActiveValue = @"() => { let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e || e.tagName === 'IFRAME' || e.tagName === 'FRAME') return null;
  if (typeof e.value === 'string' && e.tagName !== 'BUTTON') return e.value;
  return e.isContentEditable ? e.textContent : null; }";
    // What the typo gate needs about the focused field (type / inputmode / autocomplete), or null.
    // Focus inside an open shadow root is followed to the real field; focus inside a frame cannot be read
    // from here, so it reports {frame: true} and the gate fails CLOSED (no slips in a card or login form
    // that lives in an iframe).
    internal const string ActiveField = @"() => { let e = document.activeElement; if (!e || e === document.body) return null;
  while (e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (e.tagName === 'IFRAME' || e.tagName === 'FRAME') return { frame: true };
  return { type: e.tagName === 'INPUT' ? e.type : '', inputmode: e.getAttribute('inputmode'),
           autocomplete: e.getAttribute('autocomplete') }; }";
    // Everything the form routes need about ONE element, read from the node the hit test found (a CDP
    // callFunctionOn target: `this` is that node). `want` walks up to the control the locator means (a
    // click on a <select>'s padding hits the select; one on a label's text does not, and then the caller
    // tries another point). Returns null when no such ancestor exists.
    internal const string Describe = @"function (a) {
  let e = this.nodeType === 1 ? this : this.parentElement;
  if (!e) return null;
  if (a && a.want) { const c = e.closest(a.want); if (!c) return null; e = c; }
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e), tag = e.tagName.toLowerCase();
  const o = { tag, type: tag === 'input' ? e.type : '', ce: !!e.isContentEditable,
    rect: [r.x, r.y, r.width, r.height], focused: e === document.activeElement,
    value: typeof e.value === 'string' ? e.value : (e.isContentEditable ? e.textContent : null),
    min: e.getAttribute('min'), max: e.getAttribute('max'), step: e.getAttribute('step'),
    maxlength: typeof e.maxLength === 'number' ? e.maxLength : -1,
    inputmode: e.getAttribute('inputmode'), autocomplete: e.getAttribute('autocomplete'),
    readonly: !!e.readOnly, disabled: !!e.disabled, dir: cs.direction, wm: cs.writingMode,
    bl: e.clientLeft || 0, bt: e.clientTop || 0 };
  if (tag === 'select') {
    o.multiple = e.multiple; o.size = e.size; o.selectedIndex = e.selectedIndex;
    o.options = Array.from(e.options, x => ({ value: x.value, label: x.label,
      disabled: x.disabled || !!(x.parentElement && x.parentElement.tagName === 'OPTGROUP' && x.parentElement.disabled),
      selected: x.selected }));
    if (e.multiple || e.size > 1)
      o.optRects = Array.from(e.options, x => { const q = x.getBoundingClientRect(); return [q.x, q.y, q.width, q.height]; });
  }
  return o; }";
    // The controls a locator can mean for the form routes.
    internal const string FormControl = "input, textarea, select, [contenteditable=''], [contenteditable='true'], [contenteditable='plaintext-only']";
    // Does a press at a click point reach the element the caller holds? `this` = the node the browser's
    // hit test found there (DOM.getNodeForLocation, pointer-events:none skipped, as for a real press; for a
    // point inside a child frame, the <iframe> element that holds it); `b` = the element's box, plus `sel`
    // when the caller has a plain-CSS selector for it.
    //
    // With `sel` (its first match, which must still fill `b`), the answer is exact, as Playwright's own
    // hit-target check decides it: the hit is the element or inside it (across open shadow roots), or inside
    // a <label> whose control it is (the label passes the press on). Anything else on top -- an overlay
    // with exactly the element's box, a badge -- covers it.
    //
    // Without one, by the box: the hit element itself, or an ancestor that has exactly that box, with no
    // OTHER element of that same box beside the hit's path inside it that the press would miss. That twin
    // is a wrapper's control under an overlay (<span class=wrap><input><span overlay></span></span>: the
    // wrap has the input's box, the press lands on the overlay). A twin the press reaches anyway, or that no
    // press could be meant for, does not count (SC 210 fix 2; counting every twin made a MUI-style button
    // "covered" and sent its click to Playwright): one that takes no pointer events (pointer-events:none,
    // not visibility:visible -- a ripple or focus-ring span, an icon layer), a control whose <label> holds
    // the hit (a custom checkbox), and, inside an element that is itself a control, a decorative twin with
    // no control in it (a background span under a button's text).
    //
    // The covered-point check without a Playwright trial: a trial MOVES the real pointer onto the element
    // and back and performs a press whose events Playwright blocks, so a page saw a mousemove inside the
    // target, a jump back and a pointerdown/click before every humanized click (verifier B).
    internal const string HitInBox = @"function (b) {
  const same = r => Math.abs(r.x - b.x) <= 1.5 && Math.abs(r.y - b.y) <= 1.5 &&
    Math.abs(r.width - b.width) <= 1.5 && Math.abs(r.height - b.height) <= 1.5;
  const up = n => { const p = n.parentNode; return n.parentElement || (p && p.nodeType === 11 && p.host) || null; };
  const h = this.nodeType === 1 ? this : this.parentElement;
  let t = null;
  if (b.sel) { try { t = document.querySelector(b.sel); } catch (x) { t = null; } }
  if (t && same(t.getBoundingClientRect())) {
    for (let e = h, n = 0; e && n < 128; e = up(e), n++)
      if (e === t || (e.tagName === 'LABEL' && e.control === t)) return true;
    return false;
  }
  const CTL = 'a[href], area[href], button, input, select, textarea, label, summary, iframe, [contenteditable], ' +
    '[tabindex], [role=button], [role=link], [role=checkbox], [role=radio], [role=switch], [role=tab], ' +
    '[role=menuitem], [role=option], [role=combobox], [role=textbox]';
  const takes = d => { const s = getComputedStyle(d); return s.pointerEvents !== 'none' && s.visibility === 'visible'; };
  const path = new Set();
  for (let e = h, n = 0; e && n < 128; e = up(e), n++) {
    path.add(e);
    if (same(e.getBoundingClientRect())) {
      const all = e.getElementsByTagName('*'), ctl = e.matches(CTL);
      if (all.length <= 4000)
        for (const d of all) {
          if (path.has(d) || !same(d.getBoundingClientRect()) || !takes(d)) continue;
          if (d.labels && Array.prototype.some.call(d.labels, l => l.contains(h))) continue;
          if (ctl && !d.matches(CTL) && !d.querySelector(CTL)) continue;
          return false;
        }
      return true;
    }
  }
  return false; }";
    // A list box's scroll state and one row's box, from any node inside the <select> (`this`): where its
    // visible rows are (top / bottom of the client area), how far it is scrolled (st, of max), the row's
    // rect, and which rows are selected.
    internal const string ListboxRow = @"function (a) {
  const n = this.nodeType === 1 ? this : this.parentElement;
  const e = n && n.closest('select');
  if (!e || !e.options[a.i]) return null;
  const r = e.getBoundingClientRect(), q = e.options[a.i].getBoundingClientRect();
  return { box: [r.x, r.y, r.width, r.height], row: [q.x, q.y, q.width, q.height],
    top: r.y + e.clientTop, bottom: r.y + e.clientTop + e.clientHeight,
    st: e.scrollTop, max: e.scrollHeight - e.clientHeight,
    sel: Array.from(e.options, x => x.selected) }; }";
    // The first match of a plain-CSS selector, by the browser's own querySelectorAll in this world (the
    // page's hooks on its prototypes never see it): how many match, and the first one's box -- scrolled
    // into view first when `scroll` is set (centred, only if needed: what Playwright's
    // scroll_into_view_if_needed does). null when the selector is not CSS the browser accepts.
    internal const string CssBox = @"(a) => { let l;
  try { l = document.querySelectorAll(a.sel); } catch (e) { return null; }
  if (!l.length) return { n: 0 };
  const e = l[0];
  if (a.scroll) { if (e.scrollIntoViewIfNeeded) e.scrollIntoViewIfNeeded(true);
                  else e.scrollIntoView({ block: 'center', inline: 'center' }); }
  const r = e.getBoundingClientRect();
  return { n: l.length, rect: [r.x, r.y, r.width, r.height] }; }";
    // Scroll the window by [dx, dy] at once (no smooth-scroll animation to wait out) and return how far
    // it actually moved: the box humanize already holds moves by exactly that much.
    // A checkbox / radio and its <label>, from the input node (`this`): mirrors _isolated.py LABEL_OF (is the
    // input itself clickable, and the label the browser associates with it, with the label's box and its text's
    // box). Byte-identical to Python and Node (lane F, SC 232).
    internal const string LabelOf = @"function () {
  const e = this.nodeType === 1 ? this : this.parentElement;
  if (!e || e.tagName !== 'INPUT' || (e.type !== 'checkbox' && e.type !== 'radio')) return null;
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
  const clip = /rect\(\s*0(px)?[ ,]+0(px)?[ ,]+0(px)?[ ,]+0(px)?\s*\)/.test(cs.clip || '');
  const hidden = cs.display === 'none' || cs.visibility !== 'visible' || parseFloat(cs.opacity) < 0.05 ||
    r.width <= 1 || r.height <= 1 || clip || r.right < 0 || r.bottom < 0 ||
    r.left > innerWidth + 1 || r.top > innerHeight + 1;
  const o = { tag: 'input', type: e.type, rect: [r.x, r.y, r.width, r.height], hidden,
    checked: !!e.checked, disabled: !!e.disabled, label: null };
  const lab = (e.labels && e.labels.length) ? e.labels[0] : e.closest('label');
  if (!lab) return o;
  const lr = lab.getBoundingClientRect(), ls = getComputedStyle(lab);
  if (ls.display === 'none' || ls.visibility !== 'visible' || lr.width < 2 || lr.height < 2) return o;
  // The TEXT's box: the union of the label's content rects clear of the control's own box. A label that WRAPS its
  // control lists the control's border box first (SC 239 box probe: text == the input's rect), and a click there is an
  // input click, not a label click. Text wrapped to a line under the control: the first clear rect alone.
  const wraps = lab.contains(e);
  const over = q => wraps && q.left < r.right - 0.5 && q.right > r.left + 0.5 && q.top < r.bottom - 0.5 && q.bottom > r.top + 0.5;
  let text = null;
  try {
    const rg = document.createRange(); rg.selectNodeContents(lab);
    const clear = Array.from(rg.getClientRects()).filter(q => q.width >= 2 && q.height >= 2 && !over(q));
    if (clear.length) {
      const u = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
      for (const q of clear) {
        u.left = Math.min(u.left, q.left); u.top = Math.min(u.top, q.top);
        u.right = Math.max(u.right, q.right); u.bottom = Math.max(u.bottom, q.bottom);
      }
      const t = over(u) ? clear[0] : u;
      text = [t.left, t.top, t.right - t.left, t.bottom - t.top];
    }
  } catch (x) { text = null; }
  o.label = { rect: [lr.x, lr.y, lr.width, lr.height], text, contains: wraps };
  return o; }";
    // LabelOf for the first match of a plain-CSS selector (a hidden input has no box to hit-test), plus how many
    // match; null when the selector is not CSS.
    internal const string LabelBySel = @"(a) => { let l;
  try { l = document.querySelectorAll(a.sel); } catch (e) { return null; }
  if (!l.length) return { n: 0 };
  const o = (" + LabelOf + @").call(l[0]);
  if (!o) return { n: l.length };
  o.n = l.length; return o; }";
    internal const string ScrollBy = @"(d) => { const x = scrollX, y = scrollY;
  scrollBy({ left: d[0], top: d[1], behavior: 'instant' });
  return [scrollX - x, scrollY - y]; }";
}
