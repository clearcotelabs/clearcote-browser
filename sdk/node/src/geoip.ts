// geoip: resolve the egress IP's geo (timezone + language + lat/lon) so the browser's
// timezone / Accept-Language / geolocation match the proxy actually in use — the way Camoufox
// and similar tools do it. When a proxy is configured the lookup is performed THROUGH that proxy
// (so we get the proxy's exit geo, not the local machine's); with no proxy it uses the direct
// connection. Uses ip-api.com (free, no key, HTTP) and a country -> Accept-Language table.
//
// Supports HTTP/HTTPS-CONNECT-less lookups through an **http(s) proxy** (the common case). For a
// SOCKS proxy the lookup is skipped (we never fall back to the local IP under a proxy, which would
// set the wrong region) — pass timezone/acceptLanguage explicitly in that case.

import http from "node:http";

const GEO_URL =
  "http://ip-api.com/json/?fields=status,message,countryCode,timezone,lat,lon,query";

export interface Geo {
  ip?: string;
  country?: string;
  timezone?: string;
  acceptLanguage?: string;
  location?: string; // "lat,lon"
}

// Playwright proxy object -> a proxy URL with embedded credentials.
function proxyUrl(proxy?: { server?: string; username?: string; password?: string }): string | null {
  if (!proxy?.server) return null;
  let server = proxy.server;
  if (!/:\/\//.test(server)) server = "http://" + server;
  if (proxy.username) {
    const u = new URL(server);
    u.username = encodeURIComponent(proxy.username);
    if (proxy.password) u.password = encodeURIComponent(proxy.password);
    return u.toString();
  }
  return server;
}

function httpGetJson(targetUrl: string, proxy: string | null, timeoutMs: number): Promise<any> {
  return new Promise((resolve, reject) => {
    const t = new URL(targetUrl);
    let opts: http.RequestOptions;
    if (proxy) {
      const p = new URL(proxy);
      opts = {
        host: p.hostname,
        port: p.port || 80,
        path: targetUrl, // absolute-form request target = HTTP proxying for an http:// target
        headers: { Host: t.host, "User-Agent": "clearcote-sdk", Accept: "application/json" },
        timeout: timeoutMs,
      };
      if (p.username) {
        const cred = Buffer.from(
          `${decodeURIComponent(p.username)}:${decodeURIComponent(p.password)}`
        ).toString("base64");
        (opts.headers as Record<string, string>)["Proxy-Authorization"] = `Basic ${cred}`;
      }
    } else {
      opts = {
        host: t.hostname,
        port: t.port || 80,
        path: t.pathname + t.search,
        headers: { Host: t.host, "User-Agent": "clearcote-sdk", Accept: "application/json" },
        timeout: timeoutMs,
      };
    }
    const req = http.request(opts, (res) => {
      let data = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          reject(new Error(`geoip: bad response (${res.statusCode}): ${data.slice(0, 120)}`));
        }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("geoip: request timed out")));
    req.end();
  });
}

/**
 * Resolve geo for the egress (through `proxy` if given, else direct). Never throws — returns
 * null on failure so a transient lookup error can't break launch().
 */
export async function resolveGeo(
  proxy?: { server?: string; username?: string; password?: string },
  opts: { quiet?: boolean; timeoutMs?: number } = {}
): Promise<Geo | null> {
  const log = (m: string) => {
    if (!opts.quiet) process.stderr.write(`[clearcote] ${m}\n`);
  };
  let proxyForLookup: string | null = null;
  if (proxy?.server) {
    const scheme = (proxy.server.match(/^([a-z0-9]+):\/\//i)?.[1] || "http").toLowerCase();
    if (scheme.startsWith("socks")) {
      log("geoip: SOCKS proxy can't be used for the geo lookup — set timezone/acceptLanguage explicitly. Skipping.");
      return null;
    }
    proxyForLookup = proxyUrl(proxy);
  }
  try {
    const j = await httpGetJson(GEO_URL, proxyForLookup, opts.timeoutMs ?? 8000);
    if (j?.status !== "success") {
      log(`geoip: lookup failed (${j?.message || "unknown"})`);
      return null;
    }
    const geo: Geo = {
      ip: j.query,
      country: j.countryCode,
      timezone: j.timezone,
      acceptLanguage: acceptLanguageForCountry(j.countryCode),
      location: j.lat != null && j.lon != null ? `${j.lat},${j.lon}` : undefined,
    };
    log(`geoip: ${geo.ip} -> ${geo.country} tz=${geo.timezone} lang=${geo.acceptLanguage}`);
    return geo;
  } catch (e) {
    log(`geoip: ${(e as Error).message}`);
    return null;
  }
}

// country (ISO-3166 alpha-2) -> Accept-Language. A plain comma-separated language list (NO ;q=
// weights — Chromium's --accept-lang adds those for the header itself). Heuristic primary locale;
// users can override with an explicit acceptLanguage. Falls back to en-US,en.
const COUNTRY_LANG: Record<string, string> = {
  US: "en-US,en", GB: "en-GB,en", CA: "en-CA,en,fr-CA", AU: "en-AU,en", NZ: "en-NZ,en",
  IE: "en-IE,en", IN: "en-IN,en,hi", ZA: "en-ZA,en", SG: "en-SG,en",
  DE: "de-DE,de,en", AT: "de-AT,de,en", CH: "de-CH,de,fr,en",
  FR: "fr-FR,fr,en", BE: "nl-BE,nl,fr,en", NL: "nl-NL,nl,en",
  ES: "es-ES,es,en", MX: "es-MX,es,en", AR: "es-AR,es,en", CL: "es-CL,es,en",
  CO: "es-CO,es,en", PT: "pt-PT,pt,en", BR: "pt-BR,pt,en",
  IT: "it-IT,it,en", PL: "pl-PL,pl,en", RU: "ru-RU,ru,en", UA: "uk-UA,uk,ru,en",
  SE: "sv-SE,sv,en", NO: "nb-NO,no,en", DK: "da-DK,da,en", FI: "fi-FI,fi,en",
  CZ: "cs-CZ,cs,en", RO: "ro-RO,ro,en", HU: "hu-HU,hu,en", GR: "el-GR,el,en",
  TR: "tr-TR,tr,en", IL: "he-IL,he,en", SA: "ar-SA,ar,en", AE: "ar-AE,ar,en",
  EG: "ar-EG,ar,en", JP: "ja-JP,ja,en", KR: "ko-KR,ko,en",
  CN: "zh-CN,zh,en", HK: "zh-HK,zh,en", TW: "zh-TW,zh,en",
  TH: "th-TH,th,en", VN: "vi-VN,vi,en", ID: "id-ID,id,en",
  MY: "ms-MY,ms,en", PH: "en-PH,en,fil",
};

export function acceptLanguageForCountry(cc?: string): string {
  if (!cc) return "en-US,en";
  return COUNTRY_LANG[cc.toUpperCase()] || "en-US,en";
}
