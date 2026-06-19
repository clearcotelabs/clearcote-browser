/* clearcote fingerprint — console one-paste collector.
 * In a REAL Chrome: open DevTools console on any page, paste the ENTIRE contents of collect.js,
 * then paste this. It captures the profile and downloads clearcote-profile.json.
 * (No server needed; this is the JS-observable layer only — for HTTP-header-order + TLS/HTTP2,
 *  use the hosted collect.html which POSTs to a collector endpoint.) */
collectFingerprint().then((fp) => {
  const json = JSON.stringify(fp, null, 1);
  const blob = new Blob([json], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "clearcote-profile-" + (fp.meta.chrome_version || "unknown") + "-" + Date.now() + ".json";
  document.body.appendChild(a); a.click(); a.remove();
  try { navigator.clipboard.writeText(json); } catch (e) {}
  console.log("%cclearcote profile captured (" + json.length + " bytes) — downloaded + copied to clipboard.", "color:#0a0;font-weight:bold");
  return fp;
});
