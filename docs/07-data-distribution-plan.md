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
| **Ratings** (GIHWR/ALSA/… win rates) | **same S3 bulk datasets** (computed, not the API — see §7) | same cadence as context | folded into the bundle | us → CDN → client |

Both streams therefore come from the **bulk datasets** and share one refresh
trigger. Card identity/images come from Scryfall and only change on a new set
(handled by the existing identity layer).

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

## 7. Win rates: derive from the bulk datasets, not the API

**Policy check (2026-06-02).** 17Lands' own guidance:
- **Public bulk datasets — encouraged.** "Available to literally anyone," they
  "definitely encourage anyone to try." Using them whenever we want is the
  intended use.
- **Live API (`card_ratings`) — discouraged.** "Strongly discourage scraping,"
  "unsupported for external/third-party use," and they will "secure the API" on a
  pattern of abuse; they steer third parties to the website aggregates or the
  bulk datasets instead.
(Sources: 17lands.com/usage_guidelines, /public_datasets, /faq.)

**Decision: compute the headline win-rate stats ourselves from the bulk datasets
and drop the `card_ratings` API entirely.** We already ingest `game_data`
(`opening_hand_<card>`, `drawn_<card>`, `won` → GIHWR/OHWR/GDWR) and `draft_data`
(→ ALSA/ATA), so the full 17Lands metric suite is derivable from the *encouraged*
data — no scraping, no re-hosting of their live product surface, no goodwill ask.

- Add these per-card metrics to the context bundle (or a sibling `ratings.json`),
  produced by the same pipeline that builds trophy/cooc/synergy.
- Tradeoff vs. the live API: freshness lags by the bulk cadence (~weekly while a
  set is active, then frozen). For Limited the numbers stabilize quickly; label
  each bundle with its snapshot date and the source dataset's `Last-Modified`.
- Color-pair filters (the "Auto" filter, plan §4) are also computable from
  `game_data` (`main_colors`) — a later addition, still bulk-only.

> Keep everything free (WotC FCP, plan §11) and **attribute 17Lands as the data
> source** in the app — good practice and appreciated by them. No API mirror, so
> no need to clear anything with them beyond normal courtesy attribution.

## 8. Build order

1. **Manifest + uploader** — small Python script: builds/updates `manifest.json`
   (sha256, size, source_last_modified) for whatever bundles exist; pushes to the
   chosen host. (Mac-doable now.)
2. **Conditional refresh driver** — wraps `analysis.export` + `analysis.export_web`
   with the HEAD/`Last-Modified` check so it only rebuilds changed active sets.
   (Mac-doable now; this is the heart of the low-cost guarantee.)
3. **Back-catalog one-time build** — run (2) over all available sets once.
4. **GitHub Actions cron** — schedule (2)+(1) daily; publish deltas.
5. **Win-rate metrics from bulk** — extend the pipeline to compute GIHWR/OHWR/
   GDWR/ALSA/ATA per card from `game_data`/`draft_data` and fold them into the
   bundle (no API). Same refresh trigger as context.
6. **Client sync** — in the overlay app: fetch manifest, diff by sha256, download
   changed bundles; ship current sets pre-bundled for offline first-run.

Steps 1–3 and 5 are pure Python and can be built/tested on Mac before June 25;
step 6 lands with the Overwolf front-end on Windows.
