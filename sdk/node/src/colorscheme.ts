/**
 * Leave `prefers-color-scheme` to the engine's persona.
 *
 * From engine r32 the persona decides the colour scheme a Clearcote browser reports (about one
 * persona in three is dark; `--fingerprint-color-scheme=light|dark` overrides), and every surface
 * follows it: CSS and `matchMedia`, the `Sec-CH-Prefers-Color-Scheme` request header and the system
 * colours of `color-scheme: light dark` content.
 *
 * Playwright, by default, emulates `prefers-color-scheme: light` in every context it creates. That
 * emulation reaches the page but not the `Critical-CH` restart a site can ask for, so a dark persona
 * would tell the page "light" and that request "dark" (measured on r31 with `--force-dark-mode`).
 * Context options with `colorScheme: null` turn the emulation off and the three agree.
 *
 * Only for an engine that has the switch (an older engine keeps Playwright's default exactly as
 * before), and never over a `colorScheme` the caller passed.
 *
 * A browser the SDK CONNECTS to (the Clearcote Docker container, over CDP) gets the same default, and
 * also for the context(s) that exist already, which callers use directly
 * (`browser.contexts()[0].newPage()`): the emulation is per page, so there it is turned off on each
 * page as it opens (`emulateMedia({ colorScheme: null })`) and on the pages open at connect time.
 * Measured 2026-10-21 with the fingerprint audit: through Docker every persona seed reported light
 * while the same engine launched natively reports dark on about one seed in three.
 */
import type { Browser, BrowserContext, Page } from "playwright-core";
import { engineSupportsSwitch } from "./launchopts.js";

export const COLOR_SCHEME_SWITCH = "fingerprint-color-scheme";

/** Whether the engine binary that will run picks the colour scheme from the persona. */
export function engineDecidesColorScheme(exe: string | null | undefined): boolean {
  return engineSupportsSwitch(exe ?? "", COLOR_SCHEME_SWITCH);
}

/** Context options with the colour-scheme emulation off, unless the caller chose a colour scheme. */
export function defaultColorScheme<T extends object>(options: T): T & { colorScheme?: unknown } {
  return "colorScheme" in options ? options : { ...options, colorScheme: null };
}

/** Default a browser's new pages/contexts to no colour-scheme emulation. */
export function installColorSchemeDefault(browser: Browser): void {
  const origNewPage = browser.newPage.bind(browser);
  const origNewContext = browser.newContext.bind(browser);
  (browser as unknown as { newPage: (o?: Record<string, unknown>) => Promise<Page> }).newPage =
    (o = {}) => origNewPage(defaultColorScheme(o) as Parameters<typeof origNewPage>[0]);
  (browser as unknown as { newContext: (o?: Record<string, unknown>) => Promise<BrowserContext> }).newContext =
    (o = {}) => origNewContext(defaultColorScheme(o) as Parameters<typeof origNewContext>[0]);
}

/** Turn the page's colour-scheme emulation off; a page closed in the meantime is left alone. */
async function emulationOff(page: Page): Promise<void> {
  try {
    await page.emulateMedia({ colorScheme: null });
  } catch {
    // closed already: nothing left to emulate
  }
}

/**
 * Default an EXISTING context's pages to no colour-scheme emulation: the pages open now and every
 * page its `newPage` opens. A context the SDK did not create has no options to set, so the emulation
 * is turned off per page instead.
 */
export async function installColorSchemeDefaultOnContext(context: BrowserContext): Promise<void> {
  const origNewPage = context.newPage.bind(context);
  (context as unknown as { newPage: () => Promise<Page> }).newPage = async () => {
    const page = await origNewPage();
    await emulationOff(page);
    return page;
  };
  for (const page of [...context.pages()]) await emulationOff(page);
}

/**
 * The default for a browser the SDK connected to over CDP (the Docker container): its new
 * pages/contexts ({@link installColorSchemeDefault}) and the context(s) it already has.
 */
export async function installColorSchemeDefaultConnected(browser: Browser): Promise<void> {
  installColorSchemeDefault(browser);
  for (const ctx of [...browser.contexts()]) await installColorSchemeDefaultOnContext(ctx);
}
