# Enrichment Signals — Free + Deterministic Tier

A derived view of [enrichment-signals.md](./enrichment-signals.md), filtered
to signals achievable **without GenAI** (no LLM, vision models, or agents)
and **without paid APIs**.

## What "free + deterministic" means here

Allowed:
- `RAW-JSON` — fields we already have on disk from our existing Places API spend.
- `WEB-FETCH` / `WEB-HEADERS` / `HEADLESS` — plain HTTP/HTTPS, BeautifulSoup, Playwright (free).
- `DNS`, `WHOIS` / RDAP — free public protocols.
- `API-PSI` — Google PageSpeed Insights (free, 25K queries/day).
- `API-META-AD-LIB` — Meta Ad Library (free with FB developer access token).
- `API-SCHEMA-VALIDATOR` — Google's structured-data validator (free public endpoint).
- `API-SAFE-BROWSING` — free.
- `ARCHIVE-WAYBACK` — archive.org (free).
- `WHATSAPP-PROBE` — `wa.me/{phone}` HTTP redirect inspection.
- `EXIF-PARSE` — pixel-level EXIF metadata read.
- YouTube Data API — free 10K units/day.
- Reddit public API — free read-only.
- `SCRAPE-PRACTO` / `SCRAPE-JUSTDIAL` / `SCRAPE-IDA` — public web scraping (brittle, ToS-grey, but free and not GenAI).
- `SCRAPE-INSTAGRAM-PUBLIC` / `SCRAPE-FACEBOOK-PUBLIC` — limited; login walls bite.
- `LINKEDIN-VIA-SERP` — Google search for `site:linkedin.com/in/` URLs (brittle).
- `GBP-FRONTEND-SCRAPE` — Google Maps page scraping for fields the Places API doesn't return.
- Deterministic algorithms over text/images: regex, fuzzy matching (Levenshtein, Jaccard), perceptual image hashing.

Not allowed (would push to bucket 2):
- Any LLM / vision-LLM call (Claude, GPT, Gemini).
- Any agent (Claude Code sub-agent, Cowork, Computer Use, browser-use).
- Any paid API (SerpAPI, BuiltWith paid, Apify, Crustdata, Lusha, Clearbit, Apollo, Truecaller, RapidAPI scrapers).

Note on Places API: we're already paying for it for discovery. **All
existing `raw_json` is free to read.** We will not make *new* Places
calls just for enrichment in this tier.

## Headline counts

| Bucket | Count |
|---|---|
| ✓ Fully achievable, full fidelity | ~35 |
| ⚠ Achievable, partial fidelity (notes inline) | ~50 |
| ✗ Not achievable in this tier | ~25 |

The ⚠ rows are still in **Bucket 1** below — they're useful, just lower-quality than the LLM/paid alternative. Notes column flags what's lost.

---

## Bucket 1 — Achievable

### A. Acquisition health

| # | Signal | Method | Notes |
|---|---|---|---|
| ✓ A1 | Has website | `RAW-JSON.websiteUri` | — |
| ✓ A2 | Website tech stack | `WEB-HEADERS` (Server, X-Powered-By) + `WEB-FETCH` regex (e.g. `wp-content/`, `_wix`, `squarespace-cdn.com`, `cdn.shopify.com`) | Detects ~90% of common platforms; misses fully-custom builds. |
| ✓ A3 | Mobile-friendliness + Lighthouse score | `API-PSI` | Free up to 25K/day — plenty. |
| ✓ A4 | Page load speed (LCP, FID, CLS) | `API-PSI` | Same. |
| ✓ A5 | SSL cert validity + age | `WEB-FETCH` TLS handshake (`ssl.getpeercert`) | — |
| ✓ A6 | Domain age | `WHOIS` / RDAP | Some `.in` registrars hide creation date — fall back to `ARCHIVE-WAYBACK` first-snapshot. |
| ✓ A7 | Domain registrar | `WHOIS` | — |
| ✓ A8 | MX records (Workspace / Zoho / personal) | `DNS` | — |
| ⚠ A9 | Has Practo profile + URL | URL-pattern guessing (`practo.com/{city}/dentist/{slug}`) + `WEB-FETCH` to verify | Low recall. Without paid SerpAPI or an LLM-powered fuzzy match, we'll miss profiles whose slugs don't match the clinic name closely. |
| ⚠ A10 | Practo profile completeness (bio, edu, services, photos filled) | `SCRAPE-PRACTO` once URL known | Brittle to Practo redesigns. |
| ⚠ A11 | Listed on JustDial | URL-pattern + `SCRAPE-JUSTDIAL` | JustDial bot detection is aggressive; need polite rate-limit + retry. |
| ⚠ A12 | Listed on Sulekha | `SCRAPE-SULEKHA` | Same. |
| ⚠ A13 | Listed on Lybrate | `SCRAPE-LYBRATE` | Same. |
| ⚠ A14 | GBP last post date | `GBP-FRONTEND-SCRAPE` of google.com/maps page | Brittle: Maps DOM is heavy + obfuscated. Worth it for the signal but expect breakage every 6–12 mo. |
| ✓ A15 | GBP service categories | `RAW-JSON.types`, `primaryType` | We already have this. |
| ✓ A16 | GBP photos count | `RAW-JSON.photos[]` | Already have. |
| ⚠ A17 | GBP photos freshness (last upload) | `GBP-FRONTEND-SCRAPE` | Same brittleness as A14. |
| ⚠ A18 | GBP photos owner-uploaded vs user-uploaded ratio | `GBP-FRONTEND-SCRAPE` (Maps shows "Photo by owner" tags) | Counts work; LLM-VISION would also catch UGC patterns (selfies vs interior) for richer signal. |
| ✓ A20 | Currently running Meta ads | `API-META-AD-LIB` | Free, canonical. |
| ✓ A21 | Has Instagram handle | `WEB-FETCH` website + regex `instagram\.com/([\w._]+)`; or JSON-LD `sameAs` array | — |
| ⚠ A22 | Instagram follower count | `SCRAPE-INSTAGRAM-PUBLIC` (OG meta tags + JSON-LD on profile page) | Login walls increasingly aggressive. Works today, may break. |
| ⚠ A23 | Instagram post cadence (recent) | `SCRAPE-INSTAGRAM-PUBLIC` | Only the most recent few posts visible without login. Full cadence analysis would need paid scrapers. |
| ⚠ A24 | Instagram bio quality | `SCRAPE-INSTAGRAM-PUBLIC` bio text + regex (URL present? CTA word?) | Loses LLM nuance ("is this a polished branded bio?") but binary "branded?" via regex works. |
| ✓ A25 | Has Facebook Page + URL | `WEB-FETCH` + regex `facebook\.com/([\w.]+)` | — |
| ✓ A27 | Has YouTube channel | `WEB-FETCH` + regex `youtube\.com/(c\|@\|channel)/([\w-]+)` | — |
| ✓ A28 | YouTube subscriber count + last video date | YouTube Data API (free 10K units/day) | — |
| ✓ A29 | Has Twitter/X account | `WEB-FETCH` + regex | — |
| ✓ A30 | Multi-location SEO landing pages count | `WEB-FETCH` `sitemap.xml` + URL pattern matching | — |
| ✓ A31 | Schema.org LocalBusiness markup quality | `WEB-FETCH` + JSON-LD parse, validate against schema.org spec | Or free `API-SCHEMA-VALIDATOR`. |
| ✓ A32 | Multi-language site | `WEB-FETCH` `<link rel="alternate" hreflang="...">` | — |
| ⚠ A33 | Has blog / content marketing | `WEB-FETCH` `/blog/` path or RSS feed | Boolean "has blog?" works deterministically. "Quality of content marketing" needs LLM. |
| ✓ A34 | Listed in IDA member directory | `SCRAPE-IDA-DIRECTORY` | — |
| ✓ A35 | Listed in Dental Council of India registry | DCI public lookup scrape | — |

### B. Conversion health

| # | Signal | Method | Notes |
|---|---|---|---|
| ✓ B1 | WhatsApp click-to-chat link on website | `WEB-FETCH` + regex (`wa\.me/`, `api\.whatsapp\.com/send`) | — |
| ⚠ B2 | WhatsApp Business profile detected | `WHATSAPP-PROBE` GET on `wa.me/{phone}` | `wa.me` redirects regardless of Business vs personal — distinguishing the two reliably needs Meta's actual WhatsApp Business API or sending a probe message. We can detect "is on WhatsApp at all" cleanly; "is Business-grade" partially. |
| ✓ B3 | Online booking widget present + provider | `WEB-FETCH` HTML pattern match (Practo embed code, Setmore, Calendly, Zocdoc, etc.) | — |
| ⚠ B4 | GBP "Book online" button + provider | `GBP-FRONTEND-SCRAPE` | Brittle. |
| ⚠ B5 | Practo "Slots available today" | `SCRAPE-PRACTO` (when URL known) | Brittle. |
| ✓ B6 | Phone number publicly displayed on site | `RAW-JSON` + `WEB-FETCH` regex confirmation | — |
| ✓ B7 | Email displayed publicly on site | `WEB-FETCH` regex (mailto:, plain text) | Mind scraping etiquette — only use what's already public. |
| ✓ B8 | Live chat widget (Tawk / Tidio / Zendesk / Freshchat) | `WEB-FETCH` script-tag pattern match | — |
| ✓ B9 | Reviews mentioning *"didn't pick up" / "no reply" / "couldn't book" / "phone busy"* | `RAW-JSON.reviews[].text` + regex / keyword list | Direct value-jump evidence — keyword match is good enough; LLM adds nuance for ambiguous phrasings. |
| ✓ B10 | Reviews mentioning positive conversion ("called us back", "responded quickly") | `RAW-JSON.reviews` + keyword list | Same. |
| ✓ B12 | Auto-fill fields on booking form | `HEADLESS` render | Slow per page but free. |

### C. Retention health

| # | Signal | Method | Notes |
|---|---|---|---|
| ✓ C1 | Newsletter / email signup form | `WEB-FETCH` + HTML pattern (Mailchimp, ConvertKit form classes) | — |
| ⚠ C2 | Loyalty program / membership scheme | `WEB-FETCH` + keyword list ("membership", "loyalty", "club") | Boolean detection works; nuance needs LLM. |
| ⚠ C3 | Patient recall mentioned in reviews | `RAW-JSON.reviews` + keyword list ("called me after", "follow-up") | Recall on partial matches; LLM would catch paraphrases. |
| ✓ C4 | Long-cycle treatment focus (ortho, implants, Invisalign) | `WEB-FETCH` services pages + keyword list; also `RAW-JSON.types` | — |
| ⚠ C6 | Birthday / festival promo posts on social | `SCRAPE-INSTAGRAM-PUBLIC` recent post captions + keyword list | Login wall limits historical reach. |
| ⚠ C7 | "Returning patients" copy / testimonials on site | `WEB-FETCH` + keyword match | Partial. |
| ⚠ C8 | Membership badge / dental insurance partnership | `WEB-FETCH` HTML + image alt-text + keyword match (Star Health, Niva Bupa, etc.) | Catches text/alt; misses logo-only displays without OCR/vision. |

### D. Reputation health

| # | Signal | Method | Notes |
|---|---|---|---|
| ✓ D1 | Google rating | `RAW-JSON.rating` | — |
| ✓ D2 | Google review count | `RAW-JSON.userRatingCount` | — |
| ⚠ D3 | Google review velocity (30 / 90 / 180 d) | `RAW-JSON.reviews[].publishTime` (only 5 reviews returned) + `GBP-FRONTEND-SCRAPE` for full timeline | RAW-JSON gives partial signal; full historical timeline needs the brittle Maps scrape. |
| ⚠ D4 | Owner response rate to Google reviews | `GBP-FRONTEND-SCRAPE` (responses appear nested under reviews on the Maps page) | Brittle; reliable enough for "any responses at all?" boolean. |
| ⚠ D5 | Owner response latency | Same as D4 | — |
| ⚠ D6 | Templated / copy-paste response detection | String similarity (Levenshtein, Jaccard tokens, n-gram overlap) over the response texts | Catches blatant templates ("Thank you for your kind words!" repeated 100 times). LLM-EMBED catches subtler clustering. |
| ⚠ D7 | Negative review themes (no-show, billing, wait time, hygiene) | Keyword list per theme over `RAW-JSON.reviews[].text` | Theme detection via curated keyword lists works; LLM does the same with paraphrase tolerance. |
| ⚠ D8 | Practo rating + review count | `SCRAPE-PRACTO` | Brittle. |
| ⚠ D9 | Practo "Visit Recommended" badge | `SCRAPE-PRACTO` HTML class lookup | Brittle. |
| ⚠ D10 | JustDial rating + review count | `SCRAPE-JUSTDIAL` | Brittle, aggressive bot detection. |
| ✓ D11 | Cross-platform reputation consistency | Computed from D1, D8, D10 | — |
| ⚠ D12 | Reddit / forum mentions | Reddit free public API for `r/india`, `r/Punjab`, `r/dentistry` searches | Patchy coverage; LLM-free keyword/clinic-name search works. |

### E. Agency-engagement signals

| # | Signal | Method | Notes |
|---|---|---|---|
| ✓ E1 | Website footer "Designed by [Agency]" / "Powered by" | `WEB-FETCH` footer text + regex (`Designed by`, `Powered by`, `Web design by`, `© [Agency]`) | Catches direct credits; misses subtle ones. |
| ⚠ E2 | Photos with agency watermark / studio credit | `EXIF-PARSE` over downloaded photos for `Software`, `Artist`, `Copyright` fields | Most clinics' EXIF is stripped on Google upload. Caption-credit detection ("@studio_xyz") in IG captions via regex is more reliable. Vision model sees overlaid watermarks that EXIF misses. |
| ⚠ E3 | Branded logo consistency across platforms | Perceptual image hashing (pHash / dHash) across logo crops from website / GBP / Insta | Deterministic image-hash similarity catches identical/near-identical logos. Subtle variations + full visual identity check needs vision LLM. |
| ⚠ E4 | Practo Plus / Premium subscription | `SCRAPE-PRACTO` (badge HTML class) | Brittle. |
| ✓ E5 | Currently running Meta ads | `API-META-AD-LIB` | Free, canonical. |
| ✓ E7 | Multi-location SEO landing pages | `WEB-FETCH` `sitemap.xml` + URL-pattern detect | — |
| ✓ E8 | Schema.org LocalBusiness markup correctly populated | `API-SCHEMA-VALIDATOR` or `WEB-FETCH` + JSON-LD parse + validate | — |
| ✓ E9 | Live chat widget present | `WEB-FETCH` script-tag pattern | — |
| ✓ E10 | Email newsletter active (Mailchimp/Klaviyo signup form) | `WEB-FETCH` HTML pattern | — |
| ⚠ E11 | Templated owner-response style | String-similarity clustering on `GBP-FRONTEND-SCRAPE` responses | Same as D6. |
| ⚠ E12 | Posting cadence: 9-5 weekday-only on socials | Timestamp analysis on visible recent IG posts via `SCRAPE-INSTAGRAM-PUBLIC` | Sample size limited without login. |
| ⚠ E13 | Recent Instagram followers surge (>10x in <6 mo) | `ARCHIVE-WAYBACK` snapshots of IG profile page → parse follower count over time | Wayback's IG coverage is patchy; partial signal at best. |
| ⚠ E14 | Recent Google review surge (clustered uploads) | `RAW-JSON.reviews[].publishTime` clustering (5 reviews) + `GBP-FRONTEND-SCRAPE` for full history | Partial from RAW-JSON; full requires brittle scrape. |
| ⚠ E16 | Owner-uploaded photos vs user-uploaded ratio | `GBP-FRONTEND-SCRAPE` ("Photo by owner" tags on Maps page) | Brittle but free. |
| ⚠ E17 | Instagram bio mentions agency ("Marketing by @xyz") | `SCRAPE-INSTAGRAM-PUBLIC` bio + regex (`@\w+` mentions, "marketing by", "managed by") | Catches direct mentions; nuance needs LLM. |
| ✓ E20 | Domain registered via managed registrar (Wix/Squarespace/GoDaddy Pro) vs DIY | `WHOIS` registrar field; `DNS` NS records | — |
| ✓ E21 | Multi-platform polished presence (computed) | Boolean AND of E4 + A21+A22 + B3 + E1 | — |

### F. Ability-to-pay signals

| # | Signal | Method | Notes |
|---|---|---|---|
| ⚠ F1 | Practo consultation fee | `SCRAPE-PRACTO` | Brittle. |
| ⚠ F2 | Service mix tilt (cosmetic / implants / ortho / Invisalign) | `WEB-FETCH` services pages + curated keyword list | Boolean per-service detection works; LLM categorizes nuanced procedure language. |
| ✓ F3 | Years in operation | `WHOIS` domain age + `SCRAPE-PRACTO` "practising since" + `ARCHIVE-WAYBACK` first snapshot | Triangulate across sources for confidence. |
| ⚠ F4 | Number of dentists on staff | `WEB-FETCH` "Our Doctors" page + heuristic count (Dr./MDS/BDS occurrences, image count) | Works for clean pages; misses prose-style team pages. |
| ⚠ F6 | Insurance partners listed | `WEB-FETCH` + curated keyword list (Star Health, Max Bupa, Niva Bupa, ICICI Lombard) + image alt text | Misses logo-only without OCR. |
| ⚠ F7 | EMI / financing partners listed | `WEB-FETCH` + keyword list (Zest, Bajaj, Snapmint, EMI Financing) | Same. |
| ⚠ F8 | Premium neighborhood (lat/lng → tier) | Maintained dictionary of premium pincodes/neighborhoods per city | Deterministic once built; needs initial human curation per city. |
| ✓ F9 | Multilingual website | `WEB-FETCH` `hreflang` + language-switcher detection | — |
| ⚠ F10 | Awards / recognitions claimed | `WEB-FETCH` + keyword list ("award", "best", "rated") | Partial; clinic-claimed awards usually have boilerplate phrasing. |
| ✓ F11 | Hospital affiliation | `RAW-JSON` types + name-pattern regex | — |
| ✓ F12 | Equipment claims (CBCT, OPG, intraoral scanner, laser, RVG) | `WEB-FETCH` services pages + keyword list (deterministic technical terms) | High-precision keywords; works well. |
| ⚠ F13 | NABH accreditation | `WEB-FETCH` + keyword "NABH" + image alt text | — |
| ✓ F14 | Pricing transparency on site | `WEB-FETCH` + regex for ₹ amounts | Detects whether prices are listed at all; not whether they're competitive. |

### G. Owner / outreach signals

| # | Signal | Method | Notes |
|---|---|---|---|
| ⚠ G1 | Owner full name | `RAW-JSON.displayName` extraction (when "Dr. X's Clinic" pattern) + `SCRAPE-PRACTO` "Dr. X" + `WEB-FETCH` "About Us" + heuristic | Multiple sources triangulate; full automation imperfect. |
| ⚠ G5 | Owner BDS/MDS qualifications | `SCRAPE-PRACTO` profile field | Brittle. |
| ⚠ G7 | Owner direct WhatsApp / personal phone | `SCRAPE-PRACTO` (sometimes leaked) + `WEB-FETCH` site | Often hidden; partial. |
| ⚠ G8 | Owner email | `WEB-FETCH` site `mailto:` + `SCRAPE-IDA-DIRECTORY` | — |
| ⚠ G9 | Owner personal Instagram (separate from clinic) | `SCRAPE-INSTAGRAM-PUBLIC` search by name + city | Brittle, low precision without LLM disambiguation. |
| ⚠ G10 | Years since BDS (proxy for age) | `SCRAPE-PRACTO` + arithmetic | Direct from Practo profile when present. |
| ✓ G11 | Specializations listed | `RAW-JSON.types` + `SCRAPE-PRACTO` | — |
| ⚠ G12 | Owner-spoken languages | `SCRAPE-PRACTO` profile field | Practo lists this directly when filled. |
| ⚠ G13 | Owner has co-founder / partner | `WEB-FETCH` "Our Doctors" page + heuristic ("Our doctors" with N>1 entries) | Heuristic works; nuance needs LLM. |
| ⚠ G15 | Owner Insta DM availability | Implicit from G9 | — |

### H. Disqualifying signals

| # | Signal | Method | Notes |
|---|---|---|---|
| ✓ H1 | Brand name matches a chain (Clove, Apollo White, Sabka Dentist, FMS, Dentzz, Smileworks, etc.) | Substring + Levenshtein fuzzy match against a maintained list of chain brand names | Maintained list is a few dozen entries; deterministic and precise. |
| ✓ H2 | Embedded in a hospital | `RAW-JSON.types` (look for "hospital") + name pattern (`Hospital`, `Multi-speciality`) | — |
| ✓ H3 | Government / public clinic | Name pattern (`Civil Hospital`, `ESIC`, `DGHS`, `Government`, `Sarkari`) | — |
| ✓ H4 | University / dental college affiliated | Name pattern (`Dental College`, `University`, `Institute of Dental Sciences`) | — |
| ✓ H5 | `business_status` not OPERATIONAL | `RAW-JSON.businessStatus` | — |
| ✓ H6 | Very small + very young (no website, <2y, <50 reviews) | Computed from F3 + A1 + D2 | — |
| ✓ H7 | Already-perfect digital presence | Computed from E4 + A22-A23 + B3 + D2 | — |

---

## Bucket 2 — Not achievable in this tier

These signals require either an LLM, a vision model, an autonomous agent, or a paid API. Listed for completeness so we know what we're trading away.

### A. Acquisition

- **A19 — Currently running Google Ads on dental keywords.** Plain HTTP Google searches are aggressively rate-limited and bot-detected; reliable detection effectively requires `API-SERPAPI` or a browser agent.
- **A26 — Facebook Page age.** FB's login wall blocks public scraping reliably.

### C. Retention

- **C5 — WhatsApp broadcast / Status frequency.** Not externally observable without sending a probe message and being on a broadcast list.

### D. Reputation

- **D13 — Press / news coverage of the clinic.** No free reliable news search index; would need `BING-SEARCH-API` or `API-SERPAPI` news vertical, both paid.
- **D14 — Negative press / malpractice / court coverage.** Same.

### E. Agency engagement

- **E6 — Currently running Google Ads on dental keywords.** See A19.
- **E15 — YouTube videos production quality (drone, b-roll, color grading).** Vision model required to assess production value.
- **E18 — PR coverage on local newspapers / lifestyle sites.** Same as D13.
- **E19 — Press release distribution boilerplate detection.** Needs LLM-TEXT to recognize PR boilerplate patterns.

### F. Ability to pay

- **F5 — Number of chairs visible in interior photos.** Vision counting required.
- **F15 — Patient throughput indicators (post-treatment social posts).** Needs Insta history or LLM caption analysis.

### G. Owner / outreach

- **G2 — Owner LinkedIn profile URL.** Possible via `LINKEDIN-VIA-SERP` (free Google search) but rate-limited and fragile; reliable scale requires paid SerpAPI.
- **G3 — Owner LinkedIn activity level (last post, frequency).** LinkedIn login walls and aggressive blocking; reliable scale requires `API-APIFY` or similar paid scraper.
- **G4 — Owner LinkedIn job title + bio.** Same.
- **G6 — Owner age range estimate.** Vision model on headshot OR computed from BDS year (which is in ⚠ G10 if Practo has it).
- **G14 — Owner publications / speaking / IDA roles.** Needs free reliable academic/news search; not available without paid.

### H. Disqualifying

- **H8 — Currently in legal / dental council action / malpractice court coverage.** Same as D13/D14.

---

## Cross-cutting operational notes (for the free tier specifically)

| Topic | Note |
|---|---|
| **Brittleness budget** | A meaningful chunk of bucket 1 lives in ⚠ rows that depend on web scraping (Practo, JustDial, Instagram, GBP front-end). Plan on each scraper breaking once every 6–12 months as the target sites change. We'll need a "scraper health" monitor and graceful degradation (one bad scraper shouldn't kill the whole enrichment run). |
| **Rate-limit posture** | Polite back-off + caching is essential — 60 leads × 8 scraping targets each = 480 requests we don't want firing in 30s. A per-source per-IP rate limit (e.g. 1 req/sec to JustDial) is non-negotiable. Caching enriched data with a TTL (e.g. 30 days) cuts re-fetch volume by orders of magnitude. |
| **User-Agent + headers** | Bots that send the default `python-requests/X.Y.Z` UA get blocked. We'd send a real-looking browser UA on every scrape. |
| **Confidence scoring** | Most ⚠ signals are partial-quality. Each enriched field should carry a confidence number (0–1) so downstream scoring can weight a high-confidence Practo Plus badge differently from a low-confidence "maybe this is their LinkedIn." |
| **Headless browser cost** | Playwright/HEADLESS adds ~3–10s per page and uses substantial CPU. Only use when JS-rendered content is genuinely required (some IG / FB / GBP cases). Plain `WEB-FETCH` for everything else. |
| **What we explicitly lose vs the GenAI tier** | Mostly: paraphrase tolerance in review/text classification (D7, C2, C3, E17), vision-based assessments (F5, A18, E2, E3, E15, G6), agentic recovery from layout changes (anything that depends on `SCRAPE-PRACTO` or `GBP-FRONTEND-SCRAPE`), and the Google-Ads-running signal (A19, E6). Everything else has a useable free-tier method. |
| **What unlocks if we add ONE thing** | **`API-META-AD-LIB` is already free** — easy quick-win for active media-buy detection. Adding the **Anthropic Batch API for cheap LLM-TEXT** would unlock D6, D7, C2, C3, E11, E17, F2, G14 (paraphrase-tolerant text classification) at <$0.001/lead. That's the highest-leverage upgrade if we ever choose to relax the constraint. |

---

## What's intentionally NOT in this doc

- Prioritization of which Bucket 1 signals to capture first.
- The order in which to build per-source scrapers.
- Storage schema for enriched fields.

Those are the next exercises.
