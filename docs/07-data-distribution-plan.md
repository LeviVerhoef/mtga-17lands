# 07 — Data Distribution Plan (how users get set data cheaply)

> **Status:** Planning. Companion to `docs/06-overwolf-overlay-plan.md`.
> **Core principle:** users never download raw 17Lands data or run the pipeline.
> We compute centrally and ship tiny per-set bundles from a CDN; clients sync
> only what changed.

## 1. The cadence finding that drives everything

17Lands updates each set's **bulk** S3 dataset (the source of trophy /
co-occurrence / synergy) only while the set is actively drafted, then **freezes
it permanently**. Measured `Last-Modified` on `draft_data_public.<SET>.csv.gz`:

| Set | Released | Dataset last-modified | State |
|---|---|---|---|
| BLB | Aug 2024 | 2024-10-08 | frozen |
| DSK | Sep 2024 | 2024-10-30 | frozen |
| FDN | Nov 2024 | 2024-12-19 | frozen |
| DFT | Feb 2025 | 2025-03-23 | frozen |
| TDM | Apr 2025 | 2025-05-20 | frozen |
| SOS | Apr 2026 | 2026-05-14 | active (still moving) |

Implications:
- **Frozen sets = compute once, ever.** No ongoing cost.
- **Active set(s) = refresh periodically**, but only when the data actually moved.
- A cheap **HEAD request** on the S3 file returns `Last-Modified`; comparing it to
  what we last built from decides whether an expensive rebuild is needed. This is
  the low-cost sync trigger.

## 2. Two data streams (different cadence)

| Stream | Source | Changes | Size/set | Who fetches |
|---|---|---|---|---|
| **Context** (trophy/cooc/synergy) | S3 bulk datasets | only while active, then frozen | ~0.3–0.9 MB JSON | us → CDN → client |
| **Ratings** (GIHWR/ALSA/… win rates) | live `card_ratings` endpoint | daily-ish while active | ~0.25 MB JSON (all-colors) | us → CDN → client |

Card identity/images come from Scryfall and only change on a new set (handled by
the existing identity layer).

## 3. Architecture

```
         ┌─────────────── Central build (GitHub Actions, scheduled) ────────────────┐
         │  for each ACTIVE set/format:                                              │
         │    HEAD S3 dataset → Last-Modified                                        │
         │    if advanced since manifest:                                            │
         │        download bulk → analysis.export → analysis.export_web → context.json│
         │    daily: fetch card_ratings → ratings.json                               │
         │  frozen sets: built once, never recomputed                                │
         │  write/update manifest.json (version, source_last_modified, sha256, size) │
         └──────────────┬──────────────────────────────────────────────────────────┘
                        │ publish (only changed files)
                        ▼
         CDN: GitHub Releases / data branch fronted by jsDelivr  (or Cloudflare R2)
                        │
                        ▼  client checks manifest, downloads only changed bundles
         ┌─────────────────────── Overlay app (per user) ───────────────────────────┐
         │  on launch / daily: GET manifest.json (KB)                                │
         │  diff vs local cache by sha256 → download only new/updated set bundles    │
         │  ships with current-set bundles pre-bundled → works offline immediately   │
         └───────────────────────────────────────────────────────────────────────────┘
```

## 4. The manifest (client sync contract)

One small `manifest.json` at a stable URL:

```json
{
  "generated_at": "2026-06-02T...",
  "sets": [
    {
      "set": "SOS", "format": "PremierDraft", "state": "active",
      "context": { "url": ".../SOS.PremierDraft.context.json",
                   "sha256": "...", "bytes": 891298,
                   "source_last_modified": "2026-05-14T12:12:22Z",
                   "generated_at": "2026-06-02T..." },
      "ratings": { "url": ".../SOS.PremierDraft.ratings.json",
                   "sha256": "...", "bytes": 258662, "snapshot_date": "2026-06-02" }
    },
    { "set": "BLB", "format": "PremierDraft", "state": "frozen", "context": { ... } }
  ]
}
```

Client logic: fetch manifest → for each set the user needs (or all), compare
`sha256` to local; download only on mismatch. Frozen sets download once and never
again. Typical daily delta: the manifest + maybe one active set = well under 1 MB.

## 5. Hosting — low/zero cost options

1. **GitHub Releases + jsDelivr (start here).** Publish bundles as release assets
   (or a `data` branch); jsDelivr fronts GitHub with a free global CDN, no egress
   fees, no rate-limit pain. Zero infra. Best for the slow context bundles.
2. **Cloudflare R2 (scale here).** Zero egress fees, true CDN, custom domain.
   Worth it once daily ratings snapshots + large user counts make egress matter.
3. Avoid: serving from `raw.githubusercontent.com` directly (rate-limited, not a
   real CDN) or any egress-billed store (S3/CloudFront) at scale.

Keep the published data **out of the code repo's git history** (daily updates
would bloat it): use Releases, an orphan `data` branch, or a separate
`mtga-17lands-data` repo. Keep only a couple of sample bundles in the code repo
for offline dev defaults (we already have `web/data/{BLB,SOS}` for that).

## 6. Scope: ship ALL sets, refresh only active ones

- **Back-catalog:** one-time build of every set 17Lands has bulk data for. Total
  is tiny (~50 sets × ~0.5 MB ≈ 25 MB) and users only download the sets they
  draft. Mark each `frozen`.
- **Active sets:** the scheduled job only touches sets whose S3 `Last-Modified`
  advanced (usually just the current Standard set + whatever's in Quick Draft).
- Net: "all sets fully synced" with near-zero recurring compute — frozen sets are
  never rebuilt, active sets rebuild only on real change.

## 7. Centralize the live ratings too (17Lands goodwill)

Today the Tk app hits the live `card_ratings` endpoint per user (cached daily).
At scale that's thousands of users hammering 17Lands — against both their stated
preference (they discourage third-party API use) and the goodwill the project
depends on (plan §11).

**Recommendation:** our scheduled job fetches `card_ratings` **once per day per
active set** and publishes a `ratings.json` snapshot to our CDN. Users then pull
**everything from us**, never touching 17Lands directly. Benefits: 17Lands sees 1
request/day from our CI instead of N from users; clients work offline; we control
caching. Cost: keep only the latest snapshot per active set (~0.25 MB each).
- v1: snapshot the all-colors view (what the panel needs first).
- v2: add color-pair filters (`colors=WU…`) for the "Auto" color filter — a few
  MB/active set/day; only if/when that feature lands.

> **ToS note (plan §11):** publishing *derived aggregates* of the public bulk
> datasets is their intended use. Re-hosting `card_ratings` snapshots is more
> sensitive (it's their live product surface) — message the 17Lands Discord about
> intent before shipping the ratings mirror, and keep all of it free per FCP.

## 8. Build order

1. **Manifest + uploader** — small Python script: builds/updates `manifest.json`
   (sha256, size, source_last_modified) for whatever bundles exist; pushes to the
   chosen host. (Mac-doable now.)
2. **Conditional refresh driver** — wraps `analysis.export` + `analysis.export_web`
   with the HEAD/`Last-Modified` check so it only rebuilds changed active sets.
   (Mac-doable now; this is the heart of the low-cost guarantee.)
3. **Back-catalog one-time build** — run (2) over all available sets once.
4. **GitHub Actions cron** — schedule (2)+(1) daily; publish deltas.
5. **Ratings snapshotter** — daily `card_ratings` → `ratings.json` (after pinging
   17Lands).
6. **Client sync** — in the overlay app: fetch manifest, diff by sha256, download
   changed bundles; ship current sets pre-bundled for offline first-run.

Steps 1–3 and 5 are pure Python and can be built/tested on Mac before June 25;
step 6 lands with the Overwolf front-end on Windows.
