# Credits & attributions

Clearcote is built with deep gratitude to the open-source projects below. Each retains its own license; Clearcote ships **no** third-party proprietary code.

## Upstream components

| Project | Role in Clearcote | License |
|---|---|---|
| [Chromium](https://www.chromium.org/) | The browser engine | BSD-3-Clause |
| [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) | De-Googled base + patch/build tooling | BSD-3-Clause |
| [fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium) (adryfish) | Pioneering engine-level fingerprint controls on an ungoogled base | BSD-3-Clause |

## Design inspiration

- **[Brave](https://brave.com/privacy-updates/3-fingerprint-randomization/)** — Brave's "farbling" (per-session, per-site deterministic randomization) is the conceptual model behind Clearcote's *coherent identity* approach. Brave's fingerprinting-defense write-ups and open implementation are an invaluable public reference.
- **[Camoufox](https://github.com/daijro/camoufox)** — a sibling open anti-detect browser (Firefox-based). Its breadth of signal coverage is a useful reference for completeness.

## A note on independence

Clearcote is an **independent project**. It is not affiliated with, sponsored by, or endorsed by Google, Brave, or any other organization, and it is not derived from any commercial/closed-source product. Where we name other projects, it is to credit open-source inspiration and to comply with license attribution — not to imply any association.

Chromium and Chrome are trademarks of Google LLC; Clearcote is an independent Chromium-based build and uses no Google/Chrome branding. Other names are the property of their respective owners.

## License attribution

Clearcote's own source and patches are licensed under BSD-3-Clause (see [LICENSE](LICENSE)). Redistributions of upstream components include their original copyright notices and licenses, as required.
