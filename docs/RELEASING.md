# Releasing Clearcote — the standing runbook

**This is the canonical process for cutting a Clearcote release. Follow it every time, top to bottom.** It exists so that every published build is *reproducible, checksummed, and GPG-signed against a pinned key* — a user must always be able to prove, without trusting us, that what they downloaded is exactly what we built and that it hasn't been tampered with.

If you deviate, document why in the release notes. Do not silently skip a step — especially a verification or signing step.

> **Keep infrastructure out of this repo.** Build-host addresses, personal file paths, and personal emails must never be committed. This runbook uses placeholders (`$BOX`, `$STAGE`); set them in your shell, not in the file.

---

## 0. Principles (non-negotiable)

1. **Every artifact is signed and checksummed.** No release without `SHA256SUMS.txt` + a GPG detached signature + the public key attached. This is the whole point of the project (see [VERIFY.md](VERIFY.md)).
2. **Trust anchors to the pinned key, not the release.** The signing-key fingerprint `CA96F185 F96A 693A EDB3 AC1F CB00 D851 B7A8 6B0F` is published **out-of-band** in [README.md](../README.md) and [VERIFY.md](VERIFY.md), and does not change between releases. Verification means: import the key, **confirm its fingerprint equals that pinned value**, then check the signature — never trust whatever key is merely attached to a release. A release is blocked unless README **and** VERIFY.md both carry this fingerprint and the `gpg --verify` steps. *(Otherwise an attacker who controls the release swaps the zip, the sigs, and the key together and every check still passes — the GPG layer would add nothing over a bare checksum.)*
3. **Verify at every boundary.** On the build box (before moving anything), after transfer to local (re-hash), after publish (`gh api`), and once as a clean-room user would. The signed `SHA256SUMS.txt` is the source of truth.
4. **Reproducible & auditable — but don't over-claim.** The build is rebuildable from the pinned `UPSTREAM_REVISION` + the readable patch set + the published `config/args.gn`. Chromium cross-builds are **not yet bit-for-bit deterministic**, so frame the guarantee as "every change is an auditable patch and the config is public," not "rebuild and the hash matches." Reserve the hash-match/attestation promise for when a deterministic build lands (ROADMAP Phase 4).
5. **Pre-release until validated.** Any build not yet passed against live detection sites on real Windows is `--prerelease` with the caveats spelled out. No exceptions.
6. **Honest caveats, gated on the real artifact.** The release notes must state what is *not* done (§9). Drop a caveat only when the **actual built artifact's `args.gn`** (not the repo's reference config) justifies it.
7. **Brand is `clearcote`.** Public-facing names, asset names, SDK import, and docs all say *clearcote* (org `clearcotelabs`, repo `clearcote-browser`). "clearcoat" is only the internal build codename on the host — it must never leak into a published asset *name*. (Note: the current signing key's UID still reads "clearcoat release signing" — see §6; that prints on `gpg --verify`. Rotating to a `clearcote`-named key is tracked as a cleanup.)

---

## 1. Where everything lives (set the private bits in your shell)

Set these in your environment per release — **never commit them**:

```bash
export BOX=user@your-build-host      # the Linux build/signing host (private; keep its address out of the repo)
```
```powershell
$STAGE = "C:\clearcote\release"        # local staging dir for the artifacts (your machine; not committed)
```

| Thing | Location |
|---|---|
| Build host | `$BOX` (key-based SSH) |
| Build tree (host) | `~/clearcoat/build/src`, output in `out/Default` |
| Dist/staging dir (host) | `~/clearcoat/dist/` |
| VC++ runtime DLLs (host) | `~/clearcoat/dist/vcredist/` |
| GPG signing key | ed25519, fingerprint `CA96F185F96A693AEDB3AC1FCB00D851B7A86B0F`, **no passphrase**, in the host keyring |
| Local staging (Windows) | `$STAGE` |
| Repo clone (Windows) | a fresh temp dir, re-cloned per release (§11) |
| `gh` CLI | the GitHub CLI, authenticated as a repo **ADMIN** |
| Repo | `clearcotelabs/clearcote-browser`, default branch `main` |

> The private key lives **only on the build host** and has no passphrase, so it signs non-interactively. Treat the host as the signing authority. Never copy the private key off it; never print it.
>
> Keep `dist/` tidy: remove any stale **codename-named** leftovers (`clearcoat-signing-key.asc`, old `clearcoat-*.zip`) so a later wildcard `scp`/upload can't accidentally pick up a `clearcoat`-named file.

---

## 2. Versioning & tags

- Tag format: `vMAJOR.MINOR.PATCH` with a `-pre.N` suffix while unvalidated, e.g. `v0.1.0-pre.1`, … then `v0.1.0` once a build passes Windows validation (§13).
- The **Chromium version** goes in the *asset* name; the tag tracks Clearcote's own release line.
- **One tag = one immutable artifact set.** To fix a published release, cut a **new** `-pre.N+1`; never re-point an existing tag others may have pulled (§14).

---

## 3. Asset naming (must match VERIFY.md)

For Chromium version `$V` (e.g. `149.0.7827.114`) and platform `$PLAT` (e.g. `windows-x64`):

```
clearcote-$V-$PLAT.zip            # the build
clearcote-$V-$PLAT.zip.sha256     # per-artifact checksum (sha256sum -c compatible)
clearcote-$V-$PLAT.zip.sha256.asc # GPG detached sig of the above
SHA256SUMS.txt                    # aggregate: zip + chrome.exe + chrome.dll
SHA256SUMS.txt.asc                # GPG detached sig of SHA256SUMS.txt
clearcote-signing-key.asc         # ASCII-armored public signing key (convenience copy; trust the pinned fingerprint, §0.2)
```

[VERIFY.md](VERIFY.md) documents `clearcote-<version>-<platform>.zip` + `.sha256` to users — the published names **must** match it, or the verify instructions break.

---

## 4. Build + stage the artifact

Full recipe: [BUILDING.md](BUILDING.md); deep cross-build gotchas: [RESEARCH-DOSSIER.md](RESEARCH-DOSSIER.md). On the build host: ungoogled-chromium `$V` → prune → apply ungoogled patches → apply Clearcote patches → copy `config/args.gn` → `gn gen out/Default` → **`ninja -C out/Default chrome`** (same `out/Default` as BUILDING.md/AGENTS.md — do not use `out/Release`, which only holds the in-tree `gn`).

Pre-flight checks:

- [ ] `cat <repo>/UPSTREAM_REVISION` equals `$V` (the repo-root file is the canonical pin).
- [ ] `config/args.gn` matches the args the artifact is actually built with — in particular `is_official_build`. The published `config/args.gn` is `is_official_build = false`, matching the pre-release binary. To promote to a **stable** stealth-grade build, flip the config **and** the build to `true` together — they must stay identical for the "rebuild it" claim (§0.4).
- [ ] `out/Default/chrome.exe` and `chrome.dll` are freshly built.

**Stage the build into the codename zip** (this is the input §5 expects — produce it here if missing/stale). Mirror the known-good archive contents (`unzip -l` an existing release zip to confirm; ~700 entries):

```bash
ssh "$BOX" 'bash -s' <<'EOF'
set -e
cd ~/clearcoat/build/src/out/Default
zip -r ~/clearcoat/dist/clearcoat-win-x64.zip \
    chrome.exe chrome.dll chrome_elf.dll chrome_wer.dll \
    *.manifest \
    *.pak *.bin *.dat *.json locales \
    libEGL.dll libGLESv2.dll d3dcompiler_47.dll \
    vk_swiftshader.dll vulkan-1.dll VkICD_mock_icd.dll VkLayer_khronos_validation.dll
echo "staged:"; unzip -l ~/clearcoat/dist/clearcoat-win-x64.zip | tail -1
EOF
```

> Include the **full runtime set** the build produces (paks, ICU `icudtl.dat`, snapshot blobs, the entire `locales/` tree, ANGLE + SwiftShader/Vulkan DLLs). If a build/packaging helper already produces this zip, use it and just verify the contents.

---

## 4a. Patch-integrity gate — MANDATORY (blocks packaging, signing, and publish)

Clearcote *is* its patch set, and a lost stealth patch **fails open** — the browser still
launches, just less stealthy — so before you package or sign anything, prove that **every patch
is actually in the tree that built this binary AND compiled into the binary itself.** This is
the standard; see [PATCH-INTEGRITY.md](PATCH-INTEGRITY.md). Run from your Clearcote repo checkout
(the one whose `patches/` produced this build), against the build tree and the freshly-built
`chrome.dll`:

```bash
# $SRC = the build tree (e.g. ~/clearcote-build/build/src on the build host)
python3 scripts/verify_patches.py \
  --tree "$SRC" --target windows \
  --binary "$SRC/out/Default/chrome.dll"
```

**Acceptance:** exit 0 — *"every checked layer is clean."* Do **not** proceed to §5 (package),
§6 (sign), or §10 (publish) until it does.

- **Layer 1 (source)** proves the committed `patches/` reproduce the built tree, so a third party
  rebuilding from the repo gets *this* binary (§0.4). If it fails, the tree drifted from the
  committed set — run `gen_patches.sh` (§11.0) and commit the refreshed `patches/` **before
  continuing**, then re-run this gate. Never skip a patch to make it pass.
- **Layer 2 (binary)** proves each patch's code is in `chrome.dll`. If it fails, the artifact is
  stale/mis-built (an incremental build that didn't recompile a touched file, or the wrong
  `out/` dir) — rebuild (`ninja -C out/Default chrome`), re-stage (§4), and re-run.

> The same gate runs automatically at build time in [`scripts/01-apply-patches.sh`](../scripts/01-apply-patches.sh)
> (Layer 1, right after applying — a silent reject aborts before compiling) and in CI
> ([`.github/workflows/patch-integrity.yml`](../.github/workflows/patch-integrity.yml): Layer 0
> on every push, Layer 2 against the pinned release binary). This §4a run is the human-gated
> release checkpoint that ties both to the exact artifact you are about to sign.

---

## 5. Repackage to the public name + BUNDLE THE VC++ RUNTIME (easy to forget)

A stock Chromium build does **not** include the MSVC runtime. Without it, `chrome.exe` fails to start on a clean Windows 10/11 box. These five DLLs **must** be inside the zip at the archive root, next to `chrome.exe`:

```
concrt140.dll  msvcp140.dll  ucrtbase.dll  vcruntime140.dll  vcruntime140_1.dll
```

```bash
ssh "$BOX" 'bash -s' <<'EOF'
set -e
cd ~/clearcoat/dist
V=149.0.7827.114            # <-- set to this release's Chromium version
PLAT=windows-x64
ASSET=clearcote-${V}-${PLAT}.zip

test -f clearcoat-win-x64.zip || { echo 'staged build zip not found — run the §4 staging step'; exit 1; }
cp -f clearcoat-win-x64.zip "$ASSET"          # internal codename -> public name

# add the VC++ 2015-2022 runtime at the archive root (-j junks paths)
zip -j "$ASSET" vcredist/concrt140.dll vcredist/msvcp140.dll vcredist/ucrtbase.dll \
                vcredist/vcruntime140.dll vcredist/vcruntime140_1.dll

# sanity: confirm all five are present
unzip -l "$ASSET" | grep -iE "msvcp140|vcruntime140|vcruntime140_1|concrt140|ucrtbase"
EOF
```

> **Never publish a zip without confirming the five VC++ DLLs *and* the SxS `*.manifest` are present** — `unzip -l "$ASSET" | grep -iE "msvcp140|vcruntime140|vcruntime140_1|concrt140|ucrtbase|\.manifest"` should list all six. The `149.*.manifest` is as essential as the DLLs: without it `chrome.exe` fails with *"the side-by-side configuration is incorrect"* (`spawn UNKNOWN` via Playwright). Verify on a clean Windows VM that `chrome.exe` actually launches.

---

## 6. Checksums + signatures (on the build host)

```bash
ssh "$BOX" 'bash -s' <<'EOF'
set -e
cd ~/clearcoat/dist
V=149.0.7827.114; PLAT=windows-x64
ASSET=clearcote-${V}-${PLAT}.zip
FPR=CA96F185F96A693AEDB3AC1FCB00D851B7A86B0F

# hash the zip; hash the two key binaries (extracted names, unchanged by repackaging)
ZIPHASH=$(sha256sum "$ASSET" | awk '{print $1}')
EXEHASH=$(unzip -p "$ASSET" chrome.exe | sha256sum | awk '{print $1}')
DLLHASH=$(unzip -p "$ASSET" chrome.dll | sha256sum | awk '{print $1}')

printf '%s  %s\n%s  chrome.exe\n%s  chrome.dll\n' \
  "$ZIPHASH" "$ASSET" "$EXEHASH" "$DLLHASH" > SHA256SUMS.txt
printf '%s  %s\n' "$ZIPHASH" "$ASSET" > "${ASSET}.sha256"

# GPG detached signatures (key has NO passphrase -> loopback + empty passphrase)
SIGN() { gpg --batch --yes --pinentry-mode loopback --passphrase "" \
             --local-user "$FPR" --armor --detach-sign -o "$2" "$1"; }
rm -f SHA256SUMS.txt.asc "${ASSET}.sha256.asc"
SIGN SHA256SUMS.txt        SHA256SUMS.txt.asc
SIGN "${ASSET}.sha256"     "${ASSET}.sha256.asc"

# export the public key under the PUBLIC name, and remove any codename-named copy
gpg --armor --export "$FPR" > clearcote-signing-key.asc
rm -f clearcoat-signing-key.asc

echo "ZIPHASH=$ZIPHASH"
EOF
```

> The key's UID currently prints as **`clearcoat release signing …`** on `gpg --verify` — expected for now (it's a UID, not an asset name). Before a *stable* release, rotate to a `clearcote`-named key and re-sign so the codename doesn't surface to users (§0.7).

---

## 7. Verify on the build host (before you move anything)

```bash
ssh "$BOX" 'cd ~/clearcoat/dist && \
  gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt && \
  sha256sum -c clearcote-*-windows-x64.zip.sha256'
```

**Acceptance:** `Good signature` **and** the signing key's fingerprint equals the pinned `CA96F185…B7A86B0F` (on the host it shows `[ultimate]` because the key is in the keyring — that trust marker is *not* the proof; the fingerprint match is). Checksum must report `OK`. If either fails, stop — do not transfer or publish.

---

## 8. Transfer to local + verify again

Run from the parent of `$STAGE` so the destination is a **relative** path — `scp` treats a `C:\...` destination as a remote host because of the colon. The trailing-glob grabs the zip + its `.sha256`/`.asc` together.

```bash
# ensure the local dest dir exists first (scp errors "not a directory" otherwise)
mkdir -p release
scp -o StrictHostKeyChecking=no \
  "$BOX:~/clearcoat/dist/clearcote-149.0.7827.114-windows-x64.zip*" \
  "$BOX:~/clearcoat/dist/SHA256SUMS.txt*" \
  "$BOX:~/clearcoat/dist/clearcote-signing-key.asc" \
  release/
```

> The glob is expanded by the **remote** shell. On OpenSSH 9+ (SFTP-backed scp) that can fail — if so, add `-O` to force the legacy protocol: `scp -O -o ... "$BOX:...zip*" release/`.

Then confirm the transfer is byte-identical to the signed value (PowerShell):

```powershell
$z = "$STAGE\clearcote-149.0.7827.114-windows-x64.zip"
(Get-FileHash $z -Algorithm SHA256).Hash.ToLower()   # must equal ZIPHASH / the zip line in SHA256SUMS.txt
```

If it doesn't match, re-transfer. Never upload a local file whose hash doesn't match the signed checksum.

---

## 9. Write the release notes

Create `RELEASE_NOTES.md` in `$STAGE`. It **must** contain: the download table, the verify block (GPG + checksum commands + the actual hashes), the working quickstart, and the caveats checklist. **Pin the notes to this build** by including the exact `ZIPHASH`.

Canonical quickstart (keep byte-consistent with the README — Python `executable_path`, snake_case; `clearcote`-branded path):

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(
        executable_path=r"C:\clearcote\chrome.exe",
        args=["--fingerprint=seed-123", "--fingerprint-platform=windows"],
    )
    b.new_page().goto("https://abrahamjuliot.github.io/creepjs/")
```

**Mandatory caveats checklist** — include each item while it's still true; drop a line only when the **actual artifact's `args.gn`** (grep `is_official_build`/`proprietary_codecs` in the build) justifies it:

- [ ] **Pre-release / not stealth-validated** — drop once §13 validation passes on real Windows.
- [ ] **Single global seed (cross-site linkable)** — drop once per-eTLD+1 farbling ships.
- [ ] **`proprietary_codecs=true`** redistribution caveat — keep while H.264/AAC are enabled.
- [ ] **`is_official_build=false`** — keep until the shipped binary is actually built official (and `config/args.gn` matches it).

---

## 10. Publish the GitHub release

Build a full PowerShell **arg array and splat it** — no backtick line-continuations to a native exe (a stray space after a backtick silently truncates the arg list).

```powershell
$gh   = "gh"                              # GitHub CLI on PATH (or its full path)
$git  = "git"
$repo = "$env:TEMP\clearcote-pub"         # the fresh clone (§11)
$tag  = "v0.1.0-pre.1"
$V    = "149.0.7827.114"

# pre-publish: main must be at the intended release commit (tags are immutable, §2)
& $git -C $repo fetch --quiet
& $git -C $repo rev-parse origin/main      # confirm == the commit you intend to tag; if not, fix main first, don't publish

$assets = @(
 "$STAGE\clearcote-$V-windows-x64.zip",
 "$STAGE\clearcote-$V-windows-x64.zip.sha256",
 "$STAGE\clearcote-$V-windows-x64.zip.sha256.asc",
 "$STAGE\SHA256SUMS.txt",
 "$STAGE\SHA256SUMS.txt.asc",
 "$STAGE\clearcote-signing-key.asc"
)
$ghArgs  = @('release','create',$tag,'--repo','clearcotelabs/clearcote-browser')
$ghArgs += $assets
$ghArgs += @('--title', "Clearcote Browser $tag (Chromium $V, Windows x64)")
$ghArgs += @('--notes-file', "$STAGE\RELEASE_NOTES.md", '--prerelease')
& $gh @ghArgs
```

> Drop `--prerelease` only for a validated stable release. `gh release create` creates the tag on `main`'s current HEAD — that's why the pre-publish `rev-parse` check matters.

Amend notes after publishing: `& $gh release edit $tag --repo clearcotelabs/clearcote-browser --notes-file "$STAGE\RELEASE_NOTES.md"`.

---

## 11. Update the repo so the build is visible

```powershell
# fresh clone (avoid Remove-Item -Recurse on protected paths)
& $gh repo clone clearcotelabs/clearcote-browser "$env:TEMP\clearcote-pub"

# one-time, in this clone: keep commits on the public handle only — no personal email
& $git -C $repo config user.name  pim97
& $git -C $repo config user.email pim97@users.noreply.github.com
```

### 11.0 Regenerate + commit the patch set (MANDATORY — the repo must build this exact binary)

Every release MUST refresh `patches/` so a third party can rebuild the published binary from source. The committed set **drifts** — the build tree gets edited between releases without re-capturing — so **always regenerate; never assume `patches/` is current.** On the build host:

```bash
ssh "$BOX" 'cd ~/clearcoat && bash gen_patches.sh'   # diffs tree vs pristine baseline, groups, self-validates
```

`gen_patches.sh` reconstructs a pristine `149 → prune → ungoogled → windows-overlay` baseline, diffs the build tree against it (fetched toolchain **and** `*.cfbak*`/`*.bak`/`*.orig`/`*.rej` excluded), groups each changed file into exactly one concern-patch, writes `series`, then **self-validates that every patch re-applies with ZERO rejects** and leak-scans. **Acceptance:** `VALIDATION fail=0`, nothing left in `950-misc-REVIEW` (add a `group_for` mapping for any new file and re-run), and `leak scan clean`. Then copy `out_patches_full/*.patch` + `series` over `patches/`, update `patches/README.md`'s series table (new rows + revised descriptions), and commit them **in the same push** as the README banner below. Because each source file lives in exactly one patch, zero-reject per-patch equals a clean sequential apply — so applying the series to a fresh baseline reproduces this build's source tree. After regenerating, **re-run the §4a patch-integrity gate**: Layer 1 must now be clean (the refreshed `patches/` reverse-apply to the build tree), which is the machine-checkable proof that the committed set reproduces this binary. `gen_patches.sh` validates re-apply against a *pristine* baseline; the §4a gate validates it against the *actual built tree* + the *actual binary* — keep both.

1. **Update the EXISTING README status banner in place** (README already carries a `> [!NOTE]` banner) — change the tag and release link to this release; do not add a second banner.
2. Commit + push (one line, no backticks, `$tag` in the message, no `Co-Authored-By: Claude`):

```powershell
& $git -C $repo add README.md
& $git -C $repo commit -m "docs: announce $tag"
& $git -C $repo push
```

3. Repo metadata (arg array + splat; **update the Chromium version in the description each major bump**):

```powershell
$desc = "Open-source anti-detect Chromium <MAJOR> with engine-level fingerprint spoofing - de-Googled, drop-in Playwright, Windows x64, fully buildable and verifiable from source."
$editArgs  = @('repo','edit','clearcotelabs/clearcote-browser','--description',$desc)
$editArgs += @('--homepage','https://github.com/clearcotelabs/clearcote-browser')
foreach ($t in 'anti-detect-browser','chromium','fingerprint','playwright','ungoogled-chromium','web-scraping','stealth-browser','anti-fingerprinting') { $editArgs += @('--add-topic', $t) }
& $gh @editArgs
```

> `docs/RELEASING.md` is linked from `CONTRIBUTING.md` and listed in `AGENTS.md` — keep those pointers intact so this runbook stays discoverable.

---

## 12. Post-publish verification

```powershell
# comma-free jq only (a jq expr with top-level commas gets mis-split by the shell)
& $gh api repos/clearcotelabs/clearcote-browser/releases/tags/v0.1.0-pre.1 --jq '.assets[].name'
& $gh api repos/clearcotelabs/clearcote-browser/releases/tags/v0.1.0-pre.1 --jq '.prerelease'
```

Confirm all **6** assets present and `prerelease` is `true` (until validated). Then do a **clean-room verify as a user would** — download the assets to a scratch dir and:

```bash
gpg --with-fingerprint --show-keys clearcote-signing-key.asc   # fingerprint must equal CA96F185…6B0F
gpg --import clearcote-signing-key.asc
gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt                  # Good signature (a "not certified" WARNING is expected)
sha256sum -c clearcote-149.0.7827.114-windows-x64.zip.sha256    # OK
unzip -p clearcote-149.0.7827.114-windows-x64.zip chrome.exe | sha256sum   # matches chrome.exe in SHA256SUMS.txt
```

Then run the **stealth-coherence gate** against the shipped binary on real Windows — it asserts the persona/farble layer doesn't betray itself (text metrics on the 1/512 grid, main thread == worker, BCR == Range, render bytes origin-invariant, WebGPU vendor coheres with WebGL). It must report **no contract violation** (no REQUIRED check failing = no regression; no `KNOWN_GAP` newly passing without being promoted). See [docs/STEALTH-COHERENCE.md](STEALTH-COHERENCE.md).

```powershell
py -3 -m pip install playwright
py -3 scripts\stealth_coherence.py --binary "$STAGE\chrome.exe"   # exit 0 = OK; or run the "Stealth coherence" GH workflow on the pinned release
```

Finally, update the project's private build notes (not in this repo) with the tag, the new zip hash, and anything that changed.

---

## 13. Promote pre-release → stable

Drop the `-pre.N` suffix and `--prerelease` only when **all** hold:

- [ ] Built + signed + verified per §§4–8.
- [ ] **Validated on real Windows** against CreepJS / BrowserScan / Pixelscan: same seed ⇒ stable fingerprint, different seeds differ, `navigator.webdriver=false`, canvas/WebGL/audio shift with the seed.
- [ ] **Stealth-coherence gate passes** (`scripts/stealth_coherence.py`, or the "Stealth coherence" workflow): no REQUIRED check regressed, and any `KNOWN_GAP` that now passes has been promoted into `REQUIRED`. See [docs/STEALTH-COHERENCE.md](STEALTH-COHERENCE.md).
- [ ] `config/args.gn` matches the shipped binary (the `is_official_build` discrepancy in §4 resolved).
- [ ] Signing key rotated to a `clearcote`-named UID (§6) so verification output is on-brand.
- [ ] The caveats that gated "pre-release" (§9) are genuinely resolved or explicitly accepted.

---

## 14. Fixing / yanking a bad release

- **Bad assets, tag not widely pulled:** `gh release delete <tag> --cleanup-tag`, fix, re-cut. Only immediately after publishing.
- **Already in the wild:** do **not** rewrite it. Cut a superseding `-pre.N+1` (or patch) and, if needed, mark the bad one a draft / note the issue in its body. Immutable tags keep the trust model intact.

---

## Environment gotcha cheat-sheet (Windows / PowerShell 5.1)

- **Pass many args to a native exe via an array + `@splat`, never backtick line-continuation** — a stray space after a backtick silently truncates the arg list. Applies to `gh` *and* `git`.
- **`scp` to a `C:\...` destination fails** — the colon reads as a remote host. Run from the parent dir and use a **relative** destination, which must already exist. On OpenSSH 9+, add `-O` if the remote glob doesn't expand.
- **`gh ... --jq` with top-level commas gets mis-split** ("accepts 1 arg(s), received N") — use a **comma-free** jq expression, or call `gh` once per field.
- **`Remove-Item -Recurse -Force` on a path under `C:\Program...` is sandbox-blocked** — clone to a fresh temp dir instead of force-deleting.
- **GPG signing is non-interactive** because the key has no passphrase — `--batch --pinentry-mode loopback --passphrase ""`. If you rotate to a passphrased key, these signing commands must change.
- **Never commit infrastructure** — build-host addresses, personal paths, personal emails. Use `$BOX`/`$STAGE` placeholders and the `pim97@users.noreply.github.com` commit identity.

---

## One-screen checklist

```
[ ] <repo>/UPSTREAM_REVISION == release Chromium version
[ ] config/args.gn matches the artifact's actual args.gn (esp. is_official_build)
[ ] built in out/Default; staged into clearcoat-win-x64.zip (full runtime, ~700 entries)
[ ] PATCH-INTEGRITY GATE green (§4a): verify_patches.py --tree (Layer 1) + --binary (Layer 2) exit 0 — BLOCKS publish
[ ] repackaged to clearcote-$V-windows-x64.zip WITH the 5 VC++ runtime DLLs (unzip -l | grep)
[ ] SHA256SUMS.txt (zip+exe+dll) + per-asset .sha256 generated
[ ] both signed; clearcote-signing-key.asc exported; codename leftovers removed from dist/
[ ] gpg --verify OK + fingerprint == CA96F185…6B0F + sha256sum -c OK  (ON THE HOST)
[ ] scp to local (dest dir exists); Get-FileHash matches signed value
[ ] RELEASE_NOTES.md written incl. caveats checklist + correct hashes + pinned ZIPHASH
[ ] README & VERIFY.md carry the pinned fingerprint + gpg --verify steps
[ ] commit identity is pim97 <pim97@users.noreply.github.com> (no personal email)
[ ] main at intended commit (git rev-parse origin/main)
[ ] gh release create (@splat args) --prerelease
[ ] patches/ REGENERATED (gen_patches.sh: VALIDATION fail=0, no 950-misc-REVIEW, leak-clean) + patches/README.md series table updated + committed
[ ] README banner updated in place + pushed; repo description/topics set
[ ] 6 assets + prerelease=true confirmed via gh api
[ ] clean-room user verify (fingerprint check, gpg --verify, sha256sum -c, inner exe hash)
[ ] no infrastructure (host address / personal path / personal email) committed anywhere
```
