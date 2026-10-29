// geoip is on by default (0.66.0): suites that launch must not look this machine's IP up for real.
// test/geoip-default.test.ts covers the default with a stubbed resolver.
process.env.CLEARCOTE_GEOIP ??= "0";
