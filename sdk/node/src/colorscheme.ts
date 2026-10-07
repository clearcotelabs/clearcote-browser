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
