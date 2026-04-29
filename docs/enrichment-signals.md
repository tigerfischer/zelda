# Enrichment Signal Catalog

Working document. Lists every signal we could capture per dental clinic
lead, and every method we could plausibly use to capture it. **No
prioritization here** — that's the next exercise. The goal of this pass
is exhaustiveness; we'll cut later.

A "signal" is one fact about a clinic. A "method" is one way to obtain
that fact. Most signals have multiple viable methods; we list them so we
can later pick the right trade-off (cost / reliability / brittleness /
ToS risk) for each.

---

## Method legend

Used as short codes in the per-signal tables below.

### Free / deterministic methods

| Code | What it is | Notes |
|---|---|---|
| `RAW-JSON` | Read fields from the `raw_json` we already store per lead | Free, instant, lossless. Whatever the Places API returned is here. |
| `WEB-FETCH` | Plain HTTPS GET via httpx/requests, parse with BeautifulSoup/lxml | Free, fast. Some sites block bots; needs a real-looking User-Agent. |
| `WEB-HEADERS` | HEAD request to read response headers (Server, X-Powered-By) | Free, rarely blocked. |
| `HEADLESS` | Playwright/Puppeteer headless browser for JS-rendered pages | Free but slow (~3–10s/page). Bypasses most bot detection. |
| `DNS` | Direct DNS queries (MX, A, NS, TXT) via dnspython | Free, instant. |
| `WHOIS` | RDAP / WHOIS lookup for domain age + registrant | Free, public; some TLDs hide registrant under privacy. |
| `API-PLACES` | Google Places API (already integrated) | $0.025–0.032/call, paid by us already. |
| `API-PSI` | Google PageSpeed Insights API | Free, 25K queries/day. |
| `API-META-AD-LIB` | Meta Ad Library Graph API | Free; needs Facebook developer access token. |
| `API-SCHEMA-VALIDATOR` | schema.org validator (Google's structured-data testing tool) | Free public endpoint. |
| `API-SAFE-BROWSING` | Google Safe Browsing API | Free, useful as a sanity check on URLs. |
| `ARCHIVE-WAYBACK` | archive.org Wayback Machine API | Free, ~1s latency, gives historical snapshots. |
| `BING-SEARCH-API` | Bing Web Search API | $1–4 per 1,000 queries (cheap). |
| `WHATSAPP-PROBE` | GET `https://wa.me/{phone}` and inspect redirect | Free, lightweight; tells us whether the number is on WhatsApp. |
| `WHATSAPP-CATALOG-PROBE` | GET catalog page; presence implies Business profile | Free; brittle — Meta changes URL formats. |
| `EXIF-PARSE` | Read EXIF metadata from photos (camera model, GPS, software) | Free; Google strips most EXIF on upload, but not always. |

### Free / brittle (ToS-grey) methods

| Code | What it is | Notes |
|---|---|---|
| `SCRAPE-PRACTO` | HTML scrape of public practo.com profile pages | Brittle to layout changes; light usage tolerated, bulk scraping triggers blocks. |
| `SCRAPE-JUSTDIAL` | HTML scrape of justdial.com listing | Aggressive bot detection; may need rotating IPs. |
| `SCRAPE-SULEKHA` | HTML scrape of sulekha.com listing | Similar to JustDial. |
| `SCRAPE-LYBRATE` | HTML scrape of lybrate.com profile | Similar. |
| `SCRAPE-INSTAGRAM-PUBLIC` | Public Instagram profile page (no auth) | Login wall increasingly aggressive; works for handles + bio + follower count + recent post count, fails for full feed. |
| `SCRAPE-FACEBOOK-PUBLIC` | Public FB Page page | Login wall similar to Instagram. |
| `SCRAPE-IDA-DIRECTORY` | Indian Dental Association member lookup | Lightweight scrape; reliable. |
| `SCRAPE-LINKEDIN-PROFILE` | Direct linkedin.com/in/ scrape | Heavily blocked; risky. |
| `LINKEDIN-VIA-SERP` | Google `site:linkedin.com/in/` search → URL | Way more reliable than direct scraping; doesn't load full profile though. |
| `GBP-FRONTEND-SCRAPE` | Scrape google.com/maps page for fields the Places API doesn't return (e.g., post freshness, Q&A) | Brittle; Maps DOM is heavy + obfuscated. |

### Paid services / data brokers

| Code | What it is | Notes |
|---|---|---|
| `API-SERPAPI` | SerpAPI — programmatic Google SERPs incl. Maps | $50/mo for 5,000 searches. Bypasses search-rate limits. |
| `API-BUILTWITH` | BuiltWith — site tech stack lookup | Free tier: 1 lookup/day. Paid: ~$295/mo. |
| `API-WAPPALYZER` | Wappalyzer alternative | Free CLI + paid SaaS. |
| `API-APIFY` | Apify scraper marketplace (Instagram, LinkedIn, JustDial scrapers) | Pay-per-result, ~$0.20–1.00 per profile. |
| `API-RAPIDAPI-INSTA` | RapidAPI third-party Instagram scrapers | $5–30/mo entry tier. |
| `API-CRUSTDATA` | Crustdata B2B intel | Pricey ($1K+/mo); has India SMB data. |
| `API-LUSHA` | Lusha contact enrichment | Per-contact pricing. |
| `API-CLEARBIT` | Clearbit company / person enrichment | Per-record pricing; weak on Indian SMB. |
| `API-APOLLO` | Apollo.io B2B database | Better Indian coverage than Clearbit. |
| `API-TRUECALLER` | Reverse phone lookup (controversial) | Not officially API'd; resellers exist. |
| `API-NUMVERIFY` | Phone number metadata (carrier, type) | Free tier: 100/mo. |

### LLM-based methods

| Code | What it is | Notes |
|---|---|---|
| `LLM-TEXT` | Send text to Claude/GPT for classification or extraction | Per-call cost. Use Haiku for bulk-classify (~$0.0001–0.001 per lead). |
| `LLM-VISION` | Vision-capable LLM analyzes screenshots / photos | More expensive; ~$0.01–0.03 per image. |
| `LLM-BATCH` | Anthropic Batch API (50% discount, async) | Best for whole-city enrichment passes. |
| `LLM-EMBED` | Generate embeddings (clinic name, review text) | For dedupe, clustering, similarity scoring. |

### Agent-based / autonomous methods

| Code | What it is | Notes |
|---|---|---|
| `AGENT-CLAUDE-CODE` | Spawn a Claude Code sub-agent with web tools | Slower (~10–60s) but robust to layout changes; great for one-off "find X for clinic Y" tasks. |
| `AGENT-COWORK` | Claude Cowork — autonomous, longer-horizon multi-step task | Best for multi-source enrichment: "for this clinic, find their Practo profile, Instagram, owner LinkedIn, and pricing — return JSON." |
| `AGENT-COMPUTER-USE` | Claude Computer Use — drives a real browser, screenshots, clicks | Slowest + most expensive, but handles arbitrary UI; survives DOM changes. |
| `AGENT-BROWSER-USE` | `browser-use` Python library — LLM-driven Playwright | Cheaper than Computer Use; same idea, less general. |
| `AGENT-MCP-TOOLS` | Compose existing MCP servers (Drive, Notion, web fetch) inside an agent loop | Useful for storing enriched output to Notion. |

### Mixed / human-in-the-loop

| Code | What it is | Notes |
|---|---|---|
| `HUMAN-SDR` | Manual lookup by an SDR or VA | Slow, expensive, high-quality. Good for top N leads after scoring. |
| `HUMAN-VERIFY` | Pipeline flags low-confidence enrichments for human verification | Hybrid pattern; cheap insurance against bad data poisoning the score. |

---

## Signal categories

Eight categories. Within each, one row per signal.

### A. Acquisition health — "are they findable?"

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| A1 | Has website | Basic digital presence | `RAW-JSON` (websiteUri); fallback `LINKEDIN-VIA-SERP` |
| A2 | Website tech stack (WordPress / Wix / custom / static / Squarespace) | DIY vs agency build proxy | `WEB-HEADERS` + `WEB-FETCH` regex; `API-BUILTWITH`; `API-WAPPALYZER` |
| A3 | Website mobile-friendliness + Lighthouse score | Will Google rank them? | `API-PSI` (canonical); `HEADLESS` + measure manually |
| A4 | Page load speed (LCP, FID, CLS) | Conversion drops on slow sites | `API-PSI`; `HEADLESS` |
| A5 | SSL certificate validity + age | Basic hygiene | `WEB-FETCH` (TLS handshake); `API-SAFE-BROWSING` |
| A6 | Domain age (years registered) | Time-in-business proxy | `WHOIS` / RDAP; `ARCHIVE-WAYBACK` first-snapshot date as fallback |
| A7 | Domain registrar | GoDaddy/BigRock = self; managed = agency-built | `WHOIS`; `DNS` (NS records as proxy) |
| A8 | MX records (Google Workspace / Zoho / personal) | Pro-grade email vs personal Gmail | `DNS` |
| A9 | Has Practo profile + URL | Critical Indian directory | `SCRAPE-PRACTO` via search; `API-SERPAPI` (`site:practo.com [name] [city]`); `LLM-TEXT` to fuzzy-match results to clinic; `AGENT-CLAUDE-CODE` |
| A10 | Practo profile completeness (bio, education, photos, services) | Engagement marker | `SCRAPE-PRACTO`; `AGENT-COMPUTER-USE` for full-fidelity extract |
| A11 | Listed on JustDial | Secondary Indian directory | `SCRAPE-JUSTDIAL`; `API-SERPAPI`; `AGENT-CLAUDE-CODE` |
| A12 | Listed on Sulekha | Secondary directory | `SCRAPE-SULEKHA`; `API-SERPAPI` |
| A13 | Listed on Lybrate | Doctor-focused directory | `SCRAPE-LYBRATE`; `API-SERPAPI` |
| A14 | GBP last post date | If 12+ months stale, no top-of-funnel work | NOT in `RAW-JSON`; `GBP-FRONTEND-SCRAPE`; `AGENT-COMPUTER-USE` |
| A15 | GBP service categories filled in | Cosmetic/ortho/implants flagged or just "dentist"? | `RAW-JSON` (`types`, `primaryType`); `GBP-FRONTEND-SCRAPE` for service-list field |
| A16 | GBP photos count | Already partial; engagement signal | `RAW-JSON` (`photos[]`); fuller via `GBP-FRONTEND-SCRAPE` |
| A17 | GBP photos freshness (most recent upload date) | Stagnant vs active | `GBP-FRONTEND-SCRAPE`; `AGENT-COMPUTER-USE` |
| A18 | GBP photos: owner-uploaded vs user-uploaded ratio | Engaged owner vs passive | `GBP-FRONTEND-SCRAPE`; `LLM-VISION` to classify (selfie/staff/clinic interior vs UGC patterns) |
| A19 | Currently running Google Ads on dental keywords | Active media spend | `API-SERPAPI` (look for ad slot results); `AGENT-CLAUDE-CODE` (drive a browser search) |
| A20 | Currently running Meta ads (Facebook/Instagram) | Active media spend | `API-META-AD-LIB` (canonical, free); `AGENT-CLAUDE-CODE` |
| A21 | Has Instagram handle | Modern acquisition channel | `WEB-FETCH` website footer/socials; `RAW-JSON` (rare); `SCRAPE-PRACTO` bio; `AGENT-CLAUDE-CODE` |
| A22 | Instagram follower count | Reach proxy | `SCRAPE-INSTAGRAM-PUBLIC`; `API-APIFY`; `API-RAPIDAPI-INSTA` |
| A23 | Instagram post cadence (last 30 days) | Active or dormant marketing | `SCRAPE-INSTAGRAM-PUBLIC`; `API-APIFY` |
| A24 | Instagram bio quality (branded, link in bio, CTA) | Pro vs personal | `SCRAPE-INSTAGRAM-PUBLIC`; `LLM-TEXT` for "branded?" classification |
| A25 | Has Facebook Page + URL | Older platform, still relevant for India | `WEB-FETCH` website; `SCRAPE-FACEBOOK-PUBLIC`; `API-META-AD-LIB` |
| A26 | Facebook Page age | Establishment proxy | `SCRAPE-FACEBOOK-PUBLIC`; `API-META-AD-LIB` |
| A27 | Has YouTube channel | Video content investment | `WEB-FETCH` site; YouTube Data API (free 10K units/day) |
| A28 | YouTube subscriber count + last video date | Engagement | YouTube Data API |
| A29 | Has Twitter/X account | Rare but signals tech-savvy | `WEB-FETCH` site; Twitter API (limited free) |
| A30 | Multi-location SEO landing pages count | Local SEO investment (agency hallmark) | `WEB-FETCH` + sitemap.xml; `API-SERPAPI` `site:` |
| A31 | Schema.org LocalBusiness markup quality | SEO-aware build | `API-SCHEMA-VALIDATOR`; `WEB-FETCH` + parse JSON-LD |
| A32 | Multi-language site (Punjabi/Hindi versions) | Audience investment | `WEB-FETCH` `<link rel="alternate" hreflang>`, `LLM-TEXT` |
| A33 | Has blog / content marketing | Content investment | `WEB-FETCH`, `LLM-TEXT` to classify pages |
| A34 | Listed in IDA member directory | Professional credibility | `SCRAPE-IDA-DIRECTORY`; `AGENT-CLAUDE-CODE` |
| A35 | Listed in NEET-MDS / Dental Council of India registry | Verifies credentials | DCI public lookup (`SCRAPE` or via `AGENT-CLAUDE-CODE`) |

### B. Conversion health — "do inquiries become bookings?"

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| B1 | WhatsApp click-to-chat link on website | Direct capture channel | `WEB-FETCH` for `wa.me/` or `https://api.whatsapp.com/send` patterns |
| B2 | WhatsApp Business profile detected on the listed phone | Auto-reply / catalog active? | `WHATSAPP-PROBE`; `WHATSAPP-CATALOG-PROBE`; `AGENT-COMPUTER-USE` |
| B3 | Online booking widget present + provider | Practo / Setmore / custom / none | `WEB-FETCH` HTML pattern match for known widgets; `API-BUILTWITH`; `AGENT-CLAUDE-CODE` |
| B4 | GBP "Book online" button + provider | Direct conversion path | `GBP-FRONTEND-SCRAPE`; `RAW-JSON` doesn't expose this |
| B5 | Practo "Slots available today" / busy slots | Real-time demand proxy | `SCRAPE-PRACTO`; `AGENT-COMPUTER-USE` |
| B6 | Phone number publicly displayed on site | Front-desk capture | `RAW-JSON`; `WEB-FETCH` regex |
| B7 | Email displayed publicly on site | Backup channel | `WEB-FETCH` regex (`mailto:` or text); careful re: scraping etiquette |
| B8 | Live chat widget (Tawk / Tidio / Zendesk / Freshchat) | Tooling investment | `WEB-FETCH` HTML pattern; `API-BUILTWITH` |
| B9 | Reviews mentioning "didn't pick up" / "no reply" / "couldn't book" / "phone busy" | **Direct evidence of leak** | Already in `RAW-JSON.reviews` (5 most recent); regex first pass; `LLM-TEXT` for nuance |
| B10 | Reviews mentioning "called us back" / "responded quickly" | Positive conversion signal | `RAW-JSON.reviews` + `LLM-TEXT` |
| B11 | Real-world response latency | Test by sending an inquiry | `HUMAN-SDR` (one-off) — out of automated pipeline scope |
| B12 | Auto-fill fields on booking form (name/phone) | UX investment | `HEADLESS` to render the form |

### C. Retention health — "do past patients come back?"

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| C1 | Newsletter / email signup form on site | Email retention infrastructure | `WEB-FETCH` HTML pattern (Mailchimp, ConvertKit, etc.); `API-BUILTWITH` |
| C2 | Loyalty program / membership scheme | Mature retention | `WEB-FETCH`; `LLM-TEXT` to classify content |
| C3 | Patient recall mentioned in reviews ("they called me after 6 months") | Doing recall manually | `RAW-JSON.reviews` + `LLM-TEXT` |
| C4 | Long-cycle treatment focus (ortho / implants / Invisalign) | Built-in retention, high LTV | `RAW-JSON` types + `WEB-FETCH` services pages; `LLM-TEXT` |
| C5 | WhatsApp broadcast / Status frequency | Retention via WhatsApp | Hard to detect externally; `HUMAN-SDR` after WhatsApp probe |
| C6 | Birthday / festival promo posts on Instagram/FB | Retention-driven content | `SCRAPE-INSTAGRAM-PUBLIC`; `LLM-TEXT` over post captions |
| C7 | "Returning patients" copy / testimonials on site | Self-claimed retention | `WEB-FETCH` + `LLM-TEXT` |
| C8 | Membership badge / dental insurance partnership | Revenue stickiness | `WEB-FETCH` + `LLM-VISION` over logos |

### D. Reputation health — "what does their digital reputation look like?"

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| D1 | Google rating | Already have | `RAW-JSON` |
| D2 | Google review count | Already have | `RAW-JSON` |
| D3 | Google review velocity (reviews in last 30 / 90 / 180 days) | Active patient flow vs dying | `RAW-JSON.reviews[].publishTime` (only 5 reviews returned, partial); `GBP-FRONTEND-SCRAPE` for full timeline; `AGENT-COMPUTER-USE` |
| D4 | Owner response rate to Google reviews | Engagement | `RAW-JSON.reviews[]` text inspection (responses sometimes embedded); `GBP-FRONTEND-SCRAPE`; `AGENT-COMPUTER-USE` |
| D5 | Owner response latency (avg days between review and reply) | Attentiveness | Same as D4 |
| D6 | Templated / copy-paste response detection | Suggests outsourced VA / agency | `LLM-TEXT` — cluster response texts and look for near-duplicates; `LLM-EMBED` cosine similarity |
| D7 | Negative review themes (no-show, billing, wait time, hygiene) | Operational issues | `RAW-JSON.reviews` + `LLM-TEXT` classification |
| D8 | Practo rating + Practo review count (separate from Google) | Cross-platform reputation | `SCRAPE-PRACTO`; `API-APIFY`; `AGENT-CLAUDE-CODE` |
| D9 | Practo "Visit Recommended" badge | Practo-validated patient sat | `SCRAPE-PRACTO` |
| D10 | JustDial rating + review count | Tertiary reputation | `SCRAPE-JUSTDIAL`; `API-APIFY` |
| D11 | Cross-platform reputation consistency (Google vs Practo vs JD ratings within 0.3) | Authentic vs manipulated reviews | Compute from D1, D8, D10 |
| D12 | Reddit / forum mentions (r/india, r/Punjab, local forums) | Unfiltered patient sentiment | `API-SERPAPI`; `BING-SEARCH-API`; Reddit's free public API |
| D13 | Press / news coverage of the clinic | PR activity | `BING-SEARCH-API`; `API-SERPAPI` news vertical |
| D14 | Negative press (malpractice, complaints, court cases) | Risk flag | Same as D13 + `LLM-TEXT` sentiment |

### E. Agency-engagement signals — "are they already paying somebody?"

This category drives the pitch entirely: replacement vs first-hire.

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| E1 | Website footer "Designed by [Agency]" / "Powered by" | Direct credit | `WEB-FETCH` footer text + regex; `LLM-TEXT` to extract agency name |
| E2 | Photos with agency watermark or studio credit caption | Photographer / agency hand | `LLM-VISION` for watermark text in images; `LLM-TEXT` over captions for "@studio_xyz" patterns |
| E3 | Branded logo consistent across GBP / Instagram / Facebook / website / Practo | Centralized brand mgmt | `LLM-VISION` cross-image comparison; `LLM-EMBED` over logo crops |
| E4 | Practo Plus / Premium subscription | Practo-as-agency play, ₹2-5K/mo investment | `SCRAPE-PRACTO` (badge visible on profile); `AGENT-COMPUTER-USE` |
| E5 | Currently running Meta ads (Ad Library evidence) | Active media buy | `API-META-AD-LIB` (canonical); `AGENT-CLAUDE-CODE` |
| E6 | Currently running Google Ads on dental keywords | Active media buy | `API-SERPAPI`; `AGENT-CLAUDE-CODE` |
| E7 | Multi-location SEO landing pages | SEO consultant work | `WEB-FETCH` sitemap.xml + URL pattern; `API-SERPAPI` `site:` |
| E8 | Schema.org LocalBusiness markup correctly populated | SEO-aware build | `API-SCHEMA-VALIDATOR`; `WEB-FETCH` + JSON-LD parse |
| E9 | Live chat widget present (Tawk, Tidio, Zendesk, Freshchat) | Tooling investment, often agency-installed | `WEB-FETCH` HTML pattern; `API-BUILTWITH` |
| E10 | Email newsletter active (Mailchimp/Klaviyo/Convertkit signup form) | CRM tool in play | `WEB-FETCH` HTML pattern |
| E11 | Templated owner-response style across reviews | VA/agency handling reviews | `LLM-EMBED` over response texts + cluster |
| E12 | Posting cadence: 9-5 weekday-only on Instagram/FB | Office-hours pattern of an agency, not a dentist | `SCRAPE-INSTAGRAM-PUBLIC`; `API-APIFY` + post timestamp analysis |
| E13 | Recent surge in Instagram followers (>10x in <6 months) | Paid promotion | `SCRAPE-INSTAGRAM-PUBLIC` historical via `ARCHIVE-WAYBACK`; `API-APIFY` (some give time-series) |
| E14 | Recent surge in Google review count (clustered uploads) | Review-collection campaign | `RAW-JSON.reviews[].publishTime` + `GBP-FRONTEND-SCRAPE` for full history |
| E15 | YouTube videos professionally produced (drone, b-roll, color grading) | Production house involved | YouTube Data API + `LLM-VISION` over thumbnails + sample frame |
| E16 | Owner-uploaded photos vs user-uploaded ratio | High owner share = engaged owner doing it themselves; low = passive or agency | `GBP-FRONTEND-SCRAPE` |
| E17 | Instagram bio mentions agency ("Marketing by @xyz") | Direct credit | `SCRAPE-INSTAGRAM-PUBLIC` + `LLM-TEXT` |
| E18 | PR / press coverage on local newspaper sites | PR retainer signal | `BING-SEARCH-API`; `API-SERPAPI` news vertical |
| E19 | Press release distribution detected (PRWire-style boilerplate) | Paid PR distribution | `LLM-TEXT` over found press content |
| E20 | Domain registered via "managed" registrar (Wix / Squarespace / GoDaddy Pro) vs DIY | DIY proxy | `WHOIS`; `DNS` |
| E21 | Has multiple polished platforms simultaneously (good site + active Insta + Practo Plus) | Agency-handled rollout | Computed from E4, A21, A22, E1 |

### F. Ability-to-pay signals — "can they afford ₹60K/yr?"

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| F1 | Practo consultation fee | Direct pricing power proxy | `SCRAPE-PRACTO`; `AGENT-COMPUTER-USE` |
| F2 | Service mix tilt (cosmetic / implants / ortho / Invisalign) | High-LTV procedures = bigger budgets | `WEB-FETCH` services pages + `LLM-TEXT` classification; `RAW-JSON` types |
| F3 | Years in operation | Established practice = more revenue | `WHOIS` + `SCRAPE-PRACTO` "practising since" + `ARCHIVE-WAYBACK` first snapshot |
| F4 | Number of dentists on staff (from "Our Doctors" page) | Scale proxy | `WEB-FETCH` + `LLM-TEXT` count; `AGENT-CLAUDE-CODE` |
| F5 | Number of chairs visible in clinic interior photos | Scale proxy | `LLM-VISION` over GBP / website / Practo photos |
| F6 | Insurance partners listed (Star Health, Max Bupa, Niva Bupa) | Operational maturity | `WEB-FETCH` + `LLM-VISION` over partner-logo strip; `LLM-TEXT` over copy |
| F7 | EMI / financing partners listed (Zest, Bajaj, Snapmint) | Doing high-ticket procedures | `WEB-FETCH` + `LLM-TEXT` |
| F8 | Premium neighborhood (Sarabha Nagar, Model Town in Ludhiana) | Higher-tier clientele | Lat/lng → custom dictionary of premium pincodes; OR `LLM-TEXT` "is this a premium neighborhood in [city]?" |
| F9 | Multilingual website (Punjabi/Hindi versions present) | Audience investment | `WEB-FETCH` `hreflang` / language switcher |
| F10 | Awards / recognitions claimed ("Best Dental Clinic in [city] 2024") | Self-claimed status | `WEB-FETCH` + `LLM-TEXT` |
| F11 | Hospital affiliation displayed | Procurement context shifts | `WEB-FETCH` + `LLM-TEXT`; `RAW-JSON` (sometimes in name) |
| F12 | Equipment claims (CBCT, OPG, intraoral scanner, laser) | Capital investment proxy | `WEB-FETCH` + `LLM-TEXT` over services pages |
| F13 | NABH accreditation (clinical accreditation body) | Premium tier | `WEB-FETCH` + `LLM-VISION` over logos |
| F14 | Pricing transparency on site (consult fee, RCT, cleaning) | Premium often hides; budget shows | `WEB-FETCH` + `LLM-TEXT` |
| F15 | Indicators of patient throughput (post-treatment social posts, busy reviews) | Indirect revenue proxy | `SCRAPE-INSTAGRAM-PUBLIC` + post frequency |

### G. Owner / outreach signals — "who do we contact and how?"

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| G1 | Owner full name | Personalize outreach | `RAW-JSON` (sometimes in business name); `WEB-FETCH` "About Us"; `SCRAPE-PRACTO` |
| G2 | Owner LinkedIn profile URL | Best non-cold channel | `LINKEDIN-VIA-SERP` `site:linkedin.com/in/ "[name]" "[city]" dentist`; `AGENT-CLAUDE-CODE` |
| G3 | Owner LinkedIn activity level (last post date, frequency) | Are they reachable? | `SCRAPE-LINKEDIN-PROFILE` (risky); `API-APIFY` LinkedIn scraper; `AGENT-COMPUTER-USE` |
| G4 | Owner LinkedIn job title + bio | Founder / CEO vs employee | Same as G3 |
| G5 | Owner BDS/MDS qualifications | Educational signal — Manipal/MAMC/etc. = premium positioning | `SCRAPE-PRACTO`; `WEB-FETCH` "About"; `AGENT-CLAUDE-CODE` |
| G6 | Owner age range estimate | Younger = more digital-tool adoption | `LLM-VISION` over headshot photo; or compute from "BDS year" |
| G7 | Owner direct WhatsApp / personal phone | Direct line | `SCRAPE-PRACTO` (sometimes leaked); `WEB-FETCH` site contact page |
| G8 | Owner email | Backup outreach | `WEB-FETCH` site contact; `SCRAPE-IDA-DIRECTORY`; `API-APOLLO` |
| G9 | Owner personal Instagram (separate from clinic) | Sometimes more active than clinic account | `SCRAPE-INSTAGRAM-PUBLIC` via name search; `AGENT-CLAUDE-CODE` |
| G10 | Years since BDS | Career stage proxy | `SCRAPE-PRACTO` "practising since" |
| G11 | Specializations listed (orthodontist, endodontist, implantologist, pedodontist, cosmetic) | High-LTV signal + persona | `RAW-JSON` types; `SCRAPE-PRACTO`; `WEB-FETCH` |
| G12 | Owner-spoken languages | Helps decide outreach language | `SCRAPE-PRACTO` |
| G13 | Owner has a co-founder / partner | Multi-stakeholder signal | `WEB-FETCH` "Our Doctors" + `LLM-TEXT` |
| G14 | Owner publications / speaking / IDA roles | Thought-leader profile (stronger pitch) | `BING-SEARCH-API`; `API-SERPAPI`; `AGENT-CLAUDE-CODE` |
| G15 | Owner direct Instagram DM availability | Secondary outreach channel | Implicit if owner Insta exists (G9) |

### H. Disqualifying signals — "are they even our target?"

| # | Signal | Why it matters | Methods |
|---|---|---|---|
| H1 | Brand name matches a chain (Clove, Apollo White, Sabka Dentist, FMS, Dentzz, Smileworks) | Chain ≠ solo decision-maker | Substring + fuzzy match on `name` against a maintained list; `LLM-TEXT` for ambiguous cases |
| H2 | Embedded in a hospital ("XYZ Hospital Dental Department") | Institutional, multi-stakeholder | `RAW-JSON.types` (look for "hospital"); name pattern match; `LLM-TEXT` |
| H3 | Government / public clinic (Civil Hospital, ESIC, DGHS) | Won't pay | Name pattern match; `LLM-TEXT` |
| H4 | University / dental college affiliated (Christian Dental College, etc.) | Institutional | Name pattern match; `LLM-TEXT` |
| H5 | `business_status` not OPERATIONAL | Closed / temporarily closed | `RAW-JSON.businessStatus` |
| H6 | Very small + very young (no website, <2 years, <50 reviews) | Can't bootstrap them | Computed from F3 + A1 + D2 |
| H7 | Already-perfect digital presence (Practo Plus + active Insta + booking widget + 1000+ reviews) | No value-jump room | Computed from E4 + A22-A23 + B3 + D2 |
| H8 | Currently in legal / compliance trouble (court cases, dental council action) | Risk flag | `BING-SEARCH-API`; `API-SERPAPI` news vertical; `LLM-TEXT` |

---

## Cross-cutting considerations

These don't fit any single signal but affect how methods get picked.

| Topic | Note |
|---|---|
| **Instagram / LinkedIn ToS** | Direct scraping is increasingly blocked (login walls, CAPTCHAs). Apify-style paid scrapers are usually the practical choice at scale, even if they cost. Agentic methods (Computer Use, Cowork) sidestep some blocks at a higher per-lead latency cost. |
| **Practo ToS** | Light scraping for our use case (B2B research, low volume) hasn't historically drawn enforcement, but bulk scraping does. We could also consider a paid API partner if Practo offers one. |
| **Rate limiting** | Anything that hits public APIs at scale (Places, PSI, SERP) needs polite back-off + caching. Once-per-lead-per-N-days refresh policy probably right, like discovery. |
| **Caching** | Most of these methods produce data that's stable on the days-to-weeks timescale. A simple "store the last enrichment per signal per lead with a timestamp; refresh if older than N days" cuts costs aggressively. |
| **Confidence scoring per signal** | Several methods (LLM-extracted Practo URL, agentic-find owner LinkedIn) produce uncertain output. Each enriched field should carry a confidence score so downstream scoring can weight it. |
| **Idempotency** | Like discovery + sync, every enrichment method should be safe to re-run. The repo's upsert semantics already give us that on the storage side; methods themselves need to be careful (e.g., agent runs that produce different outputs each time). |
| **LLM-batch vs realtime** | For city-scale batch enrichment (60+ leads), Anthropic's Batch API at 50% off is the right pattern. For per-lead "enrich me one thing right now" (e.g., before a sales call), realtime calls. We probably want both modes. |
| **Cowork agents** | Best for one-shot "enrich this lead end-to-end" tasks where the agent can read the lead, decide what to fetch (Practo, IG, LinkedIn), and emit a structured report. Trade-off: ~$0.50–2.00 per lead vs ~$0.05 for a deterministic enrichment pipeline. Worth using on the top-N scored leads, not all of them. |
| **MCP-based composition** | We already have Drive + Notion MCPs configured. Worth considering Notion as the human-readable lead store with enriched fields shown as properties (richer than a Sheet). |
| **Refresh cadence per signal** | Domain age changes never. Practo fee changes rarely. Insta follower count changes daily. Different signals warrant different refresh cadences. |

---

## What's intentionally NOT in this doc

- Prioritization. Which of these signals to actually capture in v1, in what order, with what method. (Next exercise.)
- Storage schema for enriched fields.
- The scoring function that turns these signals into a 0–100 lead score.
- The "value-jump hypothesis" generator that turns the score into a one-line SDR talking point.

These are downstream of picking which signals matter most.
