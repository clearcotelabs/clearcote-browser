using Microsoft.Playwright;

namespace Clearcote;

/// The humanized actions on a frame, by selector: <c>frame.HumanClickAsync("#x")</c> is
/// <c>frame.Locator("#x").First.HumanClickAsync()</c> (frame actions are not strict, as Playwright's). The main frame
/// takes the page route and a child frame the frame route (Frames.cs), as their locators do. The Node and Python SDKs
/// route Playwright's own frame and element-handle actions on a humanized page; .NET humanizes only the Human*
/// calls, and these give a frame the same ones a page has.
public static partial class Humanize
{
    private static ILocator FirstIn(IFrame frame, string selector)
    {
        ArgumentNullException.ThrowIfNull(frame);
        ArgumentNullException.ThrowIfNull(selector);
        return frame.Locator(selector).First;
    }

    /// <inheritdoc cref="HumanClickAsync(IPage,string,HumanClickOptions)"/>
    public static Task HumanClickAsync(this IFrame frame, string selector, HumanClickOptions? options = null)
        => FirstIn(frame, selector).HumanClickAsync(options ?? new HumanClickOptions());

    /// <inheritdoc cref="HumanDblClickAsync(IPage,string,HumanClickOptions)"/>
    public static Task HumanDblClickAsync(this IFrame frame, string selector, HumanClickOptions? options = null)
        => FirstIn(frame, selector).HumanDblClickAsync(options);

    /// <inheritdoc cref="HumanHoverAsync(IPage,string,HumanClickOptions)"/>
    public static Task HumanHoverAsync(this IFrame frame, string selector, HumanClickOptions? options = null)
        => FirstIn(frame, selector).HumanHoverAsync(options);

    /// <inheritdoc cref="HumanFillAsync(ILocator,string,LocatorFillOptions)"/>
    public static Task HumanFillAsync(this IFrame frame, string selector, string value, LocatorFillOptions? options = null)
        => FirstIn(frame, selector).HumanFillAsync(value, options);

    /// <inheritdoc cref="HumanTypeAsync(ILocator,string,float?)"/>
    public static Task HumanTypeAsync(this IFrame frame, string selector, string text, float? delay = null)
        => FirstIn(frame, selector).HumanTypeAsync(text, delay);

    /// <inheritdoc cref="HumanPressAsync(ILocator,string,int,float?)"/>
    public static Task HumanPressAsync(this IFrame frame, string selector, string key, int repeat = 1, float? delay = null)
        => FirstIn(frame, selector).HumanPressAsync(key, repeat, delay);

    /// <inheritdoc cref="HumanFocusAsync(ILocator,float?)"/>
    public static Task HumanFocusAsync(this IFrame frame, string selector, float? timeout = null)
        => FirstIn(frame, selector).HumanFocusAsync(timeout);

    /// <inheritdoc cref="HumanCheckAsync(ILocator,LocatorCheckOptions)"/>
    public static Task HumanCheckAsync(this IFrame frame, string selector, LocatorCheckOptions? options = null)
        => FirstIn(frame, selector).HumanCheckAsync(options);

    /// <inheritdoc cref="HumanUncheckAsync(ILocator,LocatorUncheckOptions)"/>
    public static Task HumanUncheckAsync(this IFrame frame, string selector, LocatorUncheckOptions? options = null)
        => FirstIn(frame, selector).HumanUncheckAsync(options);

    /// <inheritdoc cref="HumanSetCheckedAsync(ILocator,bool,LocatorSetCheckedOptions)"/>
    public static Task HumanSetCheckedAsync(this IFrame frame, string selector, bool checkedState, LocatorSetCheckedOptions? options = null)
        => FirstIn(frame, selector).HumanSetCheckedAsync(checkedState, options);

    /// <inheritdoc cref="HumanSelectOptionAsync(ILocator,string)"/>
    public static Task<IReadOnlyList<string>> HumanSelectOptionAsync(this IFrame frame, string selector, string value)
        => FirstIn(frame, selector).HumanSelectOptionAsync(value);
}
