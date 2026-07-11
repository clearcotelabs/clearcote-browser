# Publishing the Clearcote .NET SDK to NuGet

The package id is **`Clearcote`**. It lives in `sdk/dotnet/` (library: `src/Clearcote`, tests:
`tests/Clearcote.Tests`). Versioned in lockstep with the npm/PyPI SDKs via the csproj `<Version>`.

There are two ways to publish: **CI (recommended)** and **manual**.

---

## 1. CI publish (recommended) — Trusted Publishing (OIDC), push a `dotnet-v*` tag

`.github/workflows/nuget.yml` fires on a tag `dotnet-v<version>`. It builds, runs the unit tests, packs,
mints a **short-lived** nuget.org key from the GitHub OIDC token (no stored secret), and pushes. It refuses
to publish if the tag version doesn't match the csproj `<Version>`.

**One-time setup — register the Trusted Publishing policy** (nuget.org → your avatar → **Trusted Publishing**
→ **Create**):

| Field | Value |
|---|---|
| Policy Name | `clearcote-github-actions` (any label) |
| Package Owner | `pim97` |
| Repository Owner | `clearcotelabs` |
| Repository | `clearcote-browser` |
| Workflow File | `nuget.yml` |
| Environment | *leave blank* (or set one + see below) |

Notes:
- The `Clearcote` package does **not** need to exist yet — the first trusted publish creates it under owner
  `pim97`.
- The workflow already has `permissions: id-token: write` and uses `NuGet/login@v1` (`user: pim97`) to get the
  temporary key — no `NUGET_API_KEY` secret needed. If you change your nuget.org username, update `user:`.
- **Optional approval gate:** put an environment name (e.g. `nuget`) in the policy's *Environment* field, then
  in `nuget.yml` uncomment `environment: nuget`, and create that environment under GitHub repo
  **Settings → Environments** (add required reviewers there for a manual approve-before-publish step).

**Release:**

```bash
# 1. bump the version in the csproj + the SDK constant, commit
#    - sdk/dotnet/src/Clearcote/Clearcote.csproj  <Version>0.15.4</Version>
#    - sdk/dotnet/src/Clearcote/Clearcote.cs        public const string Version = "0.15.4";
git commit -am "release(dotnet-sdk): 0.15.4"
git push origin main

# 2. tag + push -> triggers the workflow
git tag dotnet-v0.15.4
git push origin dotnet-v0.15.4
```

Watch it: **Actions → Publish NuGet SDK**. The package appears at
<https://www.nuget.org/packages/Clearcote> within a few minutes (nuget.org indexing/validation adds a short
delay before it's installable).

> **API-key alternative** (for local/unsupported CI): create a nuget.org API key scoped to push `Clearcote`
> (glob `*` for the first publish), and `dotnet nuget push --api-key <key>` — see §2.

---

## 2. Manual publish (from a machine with the .NET 8 SDK)

```bash
cd sdk/dotnet
dotnet test  Clearcote.sln -c Release            # must be green
dotnet pack  src/Clearcote/Clearcote.csproj -c Release -o ./artifacts
dotnet nuget push "./artifacts/Clearcote.<version>.nupkg" \
  --api-key "<YOUR_NUGET_API_KEY>" \
  --source https://api.nuget.org/v3/index.json \
  --skip-duplicate
```

`dotnet pack` also produces a `.snupkg` (symbols); `dotnet nuget push` uploads it alongside the `.nupkg`.

---

## Notes

- **First publish** creates the package and makes you its owner on nuget.org. Add co-owners there afterwards.
- **Versions are immutable** on nuget.org — you cannot overwrite `X.Y.Z`; bump to publish again (`--skip-duplicate`
  makes re-runs of the same version a no-op instead of an error).
- **README on the listing:** `Clearcote.csproj` sets `PackageReadmeFile=README.md`, so `src/Clearcote/README.md`
  is the package's front page — update it when features change.
- **Local smoke test of the package** before publishing:
  ```bash
  dotnet pack src/Clearcote/Clearcote.csproj -c Release -o ./artifacts
  dotnet new console -o /tmp/cc-consume && cd /tmp/cc-consume
  dotnet nuget add source "$(pwd)/../../artifacts" -n local-clearcote   # point at ./artifacts
  dotnet add package Clearcote --source local-clearcote
  ```
