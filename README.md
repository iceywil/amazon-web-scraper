# Mobile-backend scraper — Amazon Shopping

A reverse-engineering-style data collection tool built against the backend API of
the **Amazon Shopping** mobile app. It intercepts the app's traffic, documents how
the API works, and replays captured requests through a polite, resilient client.

## Quick test

Copy and paste. It sets up the environment, then asks what to search:

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e . && python quickstart.py
```

```
Search amazon.fr for: cat food
```

It searches **amazon.fr**, then prints in the terminal:

1. the **first result**: title, ASIN, price, rating, URL;
2. the **first review on its page that has a photo**: rating, title, author,
   date, text (if the first result has none, it tries the next few results);
3. that **reviewer's public profile**: name, bio, number of reviews, profile picture.

The product photo, the review photos and the reviewer's profile picture are saved
under `output/assets/`, and everything is exported to **`output.json`**. Terminals
that support inline images (iTerm2, WezTerm, VS Code with
`terminal.integrated.enableImages`) also show the pictures inline.

No account is needed: amazon.fr serves a product's top reviews on the product
page itself, and reviewer profiles are public. Already set up? Just run
`python quickstart.py`, or skip the prompt with `python quickstart.py "cat food"`
(`--marketplace uk`, `--out file.json` to change the defaults).

If Amazon shows this IP its robot check, the script says so and exits. Wait a
while (minutes to an hour) and run it again.

---

## 1. Methodology (how the endpoints were found)

The standard mobile-RE loop:

1. **Intercept.** Run the app in an Android emulator routed through **mitmproxy**;
   install the proxy CA certificate.
2. **Defeat pinning** if traffic doesn't appear: attach **Frida** / **objection**
   to bypass certificate pinning.
3. **Observe.** Use the app normally; watch each screen produce requests and read
   off the URLs, query params, headers, and pagination scheme.
4. **Confirm statically** with **jadx** / **apktool** on the APK to understand how
   requests are built (base URLs, any signing/HMAC, token lifecycle).
5. **Replay** one request with `curl` outside the app, then generalise into code.

### Reproducing the loop against the Amazon app (`android/`)

The `android/` folder automates the discovery half on a local, rooted emulator —
scripted end to end and verified against **Amazon Shopping 32.18.0.100**:

```bash
android/setup_emulator.sh    # SDK + a rootable Android 15 (API 35) arm64 AVD
android/start_emulator.sh    # boot it with a writable system partition, as root
android/install_xapk.sh apk/amazon-shopping-32.18.0.100.xapk  # verify signer, install
android/install_ca.sh        # trust the mitmproxy CA system-wide (Conscrypt APEX overlay)
android/start_frida.sh       # push + run the matching frida-server
android/capture.sh           # proxy -> mitmweb, launch Amazon with cert-unpinning
```

`install_ca.sh` handles the Android 14+ change where the CA store moved into the
read-only Conscrypt APEX: it overlays a tmpfs copy of the store, plus our CA, in
**init's** and **zygote's** mount namespaces. Every app forks from zygote, so the
second one is the one that counts. The copy must carry the stock store's SELinux
label, `system_security_cacerts_file`. With any other label, apps get `EACCES`
reading the store and trust nothing (see Finding 8). `unpin.js` neutralises the
extra pinning layers (OkHttp `CertificatePinner`, `TrustManagerImpl`, custom
`X509TrustManager`s).

### What a live capture revealed

The scripts above were run for real: a 300 s session (`android/_capture_session.sh`)
attached Frida's unpinning to Amazon, drove the app across the home feed, the
search box (typeahead + results) and product deep-links, and recorded every flow.
`android/analyze_flows.py` then mined the `.mitm` file. The notes below are the
honest result — reproduce them with the commands shown.

#### Finding 1 — the app is a **hybrid**, not a pure native client

Traffic splits into two cleanly separated planes:

| Plane | Host(s) | Auth | Body | What it carries |
|-------|---------|------|------|-----------------|
| **Native "AAPI" shell** | `data.amazon.com` | `x-aapi-signature` HMAC + `x-amzn-session-id` | Snappy, `multipart/vnd.com.amazon.api+json` | nav trees, tabs, chrome, store modes |
| **Web storefront (in WebView)** | `www.amazon.com` | **cookies** (`session-id`, `sst-main`, `lc-main`) | gzip HTML **or** JSON | search, suggestions, product, cart, location |

The give-away: every `www.amazon.com` request carries Chromium `sec-ch-ua*`
client-hint headers, `x-requested-with: XMLHttpRequest` (XHR) or
`x-requested-with: com.amazon.mShop.android.shopping` (top-level nav), and a
WebView `user-agent`. The native shell boots the chrome via the signed AAPI, then
renders the actual shopping screens as web pages inside WebViews. **The
scrapable product data lives on the web plane, behind a cookie — not behind the
HMAC.**

#### Finding 2 — the two authentication models

- **AAPI (native):** `x-aapi-signature: HmacSHA256 Time=<ts> SignedHeaders=<list> Signature=<hex>/<hex>`.
  The signature covers a fixed header set and is **time-bound**, so it cannot be
  forged or replayed outside its window. A captured AAPI request replayed later
  returns `401/400` — which is the honest, expected result, and the reason the
  scraper is built to *replay* rather than *synthesise*.
- **Web (WebView):** ordinary Amazon session cookies. These are long-lived, so a
  captured web request **replays successfully** (demonstrated below).

#### Finding 3 — the search endpoints

| Endpoint | Returns | Notes |
|----------|---------|-------|
| `GET www.amazon.com/suggestions?prefix=<q>&...` | **JSON** `{suggestions:[…]}` | typeahead; one call per keystroke; cleanest to collect |
| `GET www.amazon.com/s?k=<q>&page=<n>` | **HTML** (~800 KB) | the results page; parse product cards, paginate with `&page=` |
| `GET www.amazon.com/autocomplete` | HTML | the search overlay host page |

`/suggestions` is the easy win: structured JSON, cookie-auth, `records-path
suggestions`. `/s` is server-rendered HTML — scraping it means parsing the DOM
(or finding its JSON data-island), not reading a clean API.

#### Finding 3b — parsing `/s` and `/dp` (built + working)

Both the search-results and product-detail pages were reverse-engineered and are
implemented in [`scraper/amazon_web.py`](scraper/amazon_web.py):

- **`/s` search results.** Each product is a
  `div[data-component-type="s-search-result"]` carrying `data-asin`; inside it,
  the title is in `h2`, the price in `.a-price .a-offscreen`, the star rating in
  `.a-icon-alt` ("4.5 out of 5 stars"), the thumbnail in `img.s-image`, and
  sponsored cards are flagged by the `AdHolder` class. Pagination is `&page=N`
  (verified: consecutive pages return disjoint ASIN sets).
- **`/dp` product detail.** *The layout depends on the `User-Agent`.* Under the
  app's own UA, `/dp` returns an app-optimised page whose field ids are unstable;
  under a **desktop UA** it returns the classic layout with dependable ids —
  `#productTitle`, `#corePrice_feature_div .a-offscreen`, `#acrPopover[title]`
  (rating), `#acrCustomerReviewText` (review count), `#feature-bullets li`,
  `#availability`. So `product()` sends a desktop UA by default (keeping the
  captured cookies) and the parser falls back across selectors + regex.
- **Request shaping matters.** If the captured template is the `/suggestions`
  **XHR** (`accept: application/json`, `x-requested-with: XMLHttpRequest`), those
  headers make `/s` and `/dp` return partial/JSON fragments. The scraper rewrites
  them into a top-level **document navigation** so full HTML comes back.

Verified live, reusing a captured cookie with no account signed in:

```
web-search "coffee mug"  -> 14 cards (asin/title/price/rating/reviews/image/sponsored)
web-product B083TCZDMT    -> Contigo … $17.58 ★4.5 45,102 reviews, In Stock, 5 bullets
```

#### Finding 4 — environment quirks worth recording

- **Geo-routing.** The emulator egressed from an IP that Amazon geolocated to
  **Indonesia** ("We're showing you items that ship to Indonesia"), so US-marketplace
  (`ATVPDKIKX0DER`) product/search deep-links returned the app's "UH-OH" error page
  (`CS3`). The in-app search box still worked. On a real target, egress geography
  changes what you can collect — note it.
- **Snappy.** Native AAPI responses use `accept-encoding: snappy`; mitmproxy can't
  decode it, so `analyze_flows.py` falls back to `cramjam` to recover JSON.
- **Telemetry fan-out.** Alongside the useful endpoints sit `maes/v2`,
  `fls-na.amazon.com`, `unagi*.amazon.com`, `bitdrift`, a `mdcs` WebSocket
  (HTTP `101`) and Arcus remote-config — all skippable for data collection.

#### Finding 5 — a working replay against a live endpoint

The cookie-authed typeahead replays end to end and returns live data:

```bash
# one-off: export a captured request, then collect live suggestions as NDJSON
python run.py replay --curl android/example_suggestions.curl \
    --records-path suggestions --pages 1 --format ndjson
# -> {"type":"PAST_SEARCHES","value":"…"}  {"type":"KEYWORD","value":"… maker"} …
```

This closes the chain the project set out to demonstrate: **intercept → unpin →
decrypt → export cURL → replay through the scraper → structured records**, on a
real Amazon endpoint.

#### Finding 6 — you don't actually need the emulator (or a browser)

The emulator was how we *discovered* and *captured* the protocol. For ongoing
collection on the web plane, it turns out not to be needed at all — and neither is
a headless browser. Two facts, both verified:

- **The storefront cookies are not a credential.** They are the anonymous session
  Amazon hands any first visitor; the captured app cookies worked only because
  they were such a session, not because the app authenticated them.
- **`/dp` and `/s` mint a session for a plain client.** A scripted `GET /`
  (homepage) is answered by CloudFront with an **HTTP 202 AWS WAF JavaScript
  challenge** (a 2 KB page carrying `window.gokuProps`) and at most `ak_bmsc` —
  no `session-id`. But a plain `GET /dp/<ASIN>` (or `/s`) is answered directly and
  **sets the full session** (`session-id`, `sst-main`, `i18n-prefs`, …). So
  [`scraper/session.py`](scraper/session.py) mints a usable session over plain
  HTTP in one request, then (optionally) pins the delivery location through
  Amazon's two-hop glow CSRF flow (`anti-csrftoken-a2z` → `address-change`).

```bash
python run.py mint-session --marketplace us --zip 10001 -o session.json
#   seeded (us): ['i18n-prefs','lc-main','session-id','session-id-time','sp-cdn','sst-main']
#   delivery location set to 10001
```

What the headless bootstrap showed, step by step (plain `httpx`, HTTP/2,
desktop Chrome UA, from a non-US egress IP):

| Request | Result |
|---------|--------|
| `GET /` | `202`, AWS WAF JS challenge, no session cookie |
| `GET /s?k=…` with **no cookies at all** | `200`, 48 product cards — even a cookie is optional for reads |
| `GET /dp/<ASIN>` | `200`, full page + `Set-Cookie`: `session-id`, `session-id-time`, `sst-main`, `i18n-prefs`, `lc-main`, `sp-cdn` |
| …same session, prices | geolocated from the egress IP: `i18n-prefs=INR`, `sp-cdn="L5Z9:IN"`, "Deliver to **India**", price `1667.85` (INR) |
| `GET /portal-migration/hz/glow/get-rendered-address-selections` + header `anti-csrftoken-a2z` (from the `/dp` page's `nav-global-location-data-modal-action` `data-a-modal` JSON) | `200`, location modal HTML carrying a second `CSRF_TOKEN` |
| `POST /portal-migration/hz/glow/address-change?actionSource=glow` + that token, body `{"locationType":"LOCATION_INPUT","zipCode":"10001",…}` | `{"isAddressUpdated":1,"address":{"countryCode":"US","city":"NEW YORK",…}}`; adds `ubid-main`, `session-token` |
| `GET /dp/<ASIN>` again | "Deliver to **New York 10001**", **$17.58 USD**, In Stock |

Two takeaways:
- **The geo-IP silently changes the data.** Without a location, amazon.com
  serves INR prices and India availability, which is still a valid `200` page.
  So pin a ZIP code, or at least force the currency: setting the
  `i18n-prefs=<currency>` cookie alone brings back USD (or GBP, EUR, … per
  store). The ZIP code also fixes availability and delivery estimates.
- **A seed page must not be the homepage.** Any `/dp/<ASIN>` works; the ASIN
  can be overridden with `--seed-asin` if it is ever delisted.

**This is now the default.** Every `web-*` command run without `--cookies` or
`--curl` mints a session itself (ZIP `10001` on `us`; on other stores the
IP-geolocated location, with the store's own currency forced). `mint-session`
is still there to save a session to a file and reuse it across runs.

So: **the emulator is for capture/analysis; routine web collection needs only a
minted session.** The native AAPI plane still needs the app (its HMAC can't be
reproduced off-device). On *not getting bot-walled*: the durable approach is to
behave like a legitimate client — hit the pages that answer plainly, keep the
session, and rate-limit — not to forge fingerprints or solve challenges at scale.

#### Finding 7 — endpoint coverage and limits

What the HTML plane exposes to an anonymous session, now implemented in
[`scraper/amazon_web.py`](scraper/amazon_web.py):

| Data | Endpoint | Anonymous? | Parser |
|------|----------|-----------|--------|
| Search results | `/s?k=&page=` | ✅ | `parse_search` (asin, title, price+currency, rating, reviews, image, sponsored) |
| Category listing | `/s?rh=n:<node>&fs=true` | ✅ | `parse_search` (`fs=true` forces the grid; a bare node is a landing page) |
| Product detail | `/dp/<ASIN>` | ✅ | `parse_product` (+ brand, availability, bullets, seller, seller_id) |
| **Review aggregate** | `/dp` `#reviewsMedley` | ✅ | `parse_review_summary` (average, total, per-star histogram) |
| Individual review *text* | `/product-reviews/<ASIN>` | ❌ **login** | anonymous: `AuthRequired`; signed in: 8 featured reviews per product (Finding 9) |
| Seller ("company") | `/sp?seller=<ID>` | ✅ | `parse_seller` (name, rating, business info) |
| Seller feedback | `/sp?seller=<ID>`, then `/sp/ajax/feedback` | ✅ | `parse_seller_feedback` (rating, comment, rater name, date); later pages as JSON with rater profile/avatar and a `suppressed` flag |
| Reviewer profile | `/gp/profile/<amzn1.account.…>` | ✅ | `parse_profile` (name, avatar, bio, influencer flag, the reviewer's reviews with photos) |

Two honest limits:
- **Full review text is login-gated.** The aggregate/histogram is anonymous; the
  per-review list redirects to sign-in, so anonymously `web-reviews` reports
  `LOGIN REQUIRED` and `web-review-summary` is the substitute. Full text is
  available by supplying a session **you logged into yourself** — see
  *Authenticated access* in §2 (`AmazonWebScraper.authenticated` /
  `--auth-cookies`); the library never automates login. Even signed in, only
  the 8 featured reviews come back (Finding 9).
- **Prices follow egress geography.** Our egress geolocates to India, so
  `amazon.co.uk` returns prices in **INR**, not GBP — the number is correct, the
  currency is the visitor's. We therefore record `currency` on every price; true
  local pricing needs in-country egress.
- **Reviewer profiles are a separate request.** A review row carries the
  author's name, avatar and profile link as shown on the card; `profile()`
  fetches the profile itself (Finding 10). Seller-feedback entries past page 1
  also carry the rater's profile link and avatar (`rater_profile`, `rater_avatar`).

#### Finding 8 — the sign-in / sign-up flow (captured with your own account)

`login_in_browser(app=True)` (see *Authenticated access* in §2) recorded a real
sign-up followed by a sign-in inside the app, with the account holder typing on
the device. Each step, from `android/flows/login.ndjson`:

| t | Request | What it does |
|---|---------|--------------|
| 120 s | `GET /ap/register` | sign-up form |
| 168 s | `POST /ax/claim` → `302 /ax/claim/intent` | **email first**: a "unified auth" step checks the identifier before any password is asked |
| 173 s | `POST /ap/register` (`email`, `arb`, `anti-csrftoken-a2z`) | email accepted; fresh `session-id`, `ubid-main`, `session-token` |
| 198 s | `POST /ap/register` (`customerName`, `encryptedPwd`, `metadata1`, `workflowState`, …) | account created |
| 399 s | `GET /ap/signin` → `POST /ax/claim` | sign-in, again email first |
| 422 s | `POST /ap/signin` → `302 /ap/maplanding` | **auth cookies set**: `x-main`, `at-main`, `sess-at-main` |
| 424 s | `POST api.amazon.com/auth/register` | the app registers itself as a **device** on the account (MAP) |
| 424–426 s | 5 × `POST api.amazon.com/auth/token` | exchanges that registration for per-service app tokens |
| 426 s → | `www.amazon.com/…`, `data.amazon.com/api/…/customer/…` | signed-in web pages and the native API now run as the account |

What this shows:

- **Two credentials come out of one sign-in.** The web plane gets cookies
  (`at-main`/`sess-at-main`/`x-main`, plus the usual session cookies). The native
  plane gets device-bound MAP tokens from `/auth/register` + `/auth/token`. The
  cookies alone are enough for the scraper: the 12-cookie jar from this run
  passed the signed-in check in `AmazonWebScraper.authenticated()`.
- **Sensitive pages need a fresh sign-in.** Your Orders asks for re-authentication
  (`/ap/signin?openid.pape.max_auth_age=0` → `/ax/claim`) once a sign-in is a few
  minutes old, for a valid session and an anonymous one alike. So it can't tell
  them apart, and `is_authenticated()` instead checks a product page for the
  signed-in greeting and sign-out link (`is_signed_in`). The session itself stayed
  valid: hours later it still read reviews and greeted the account by name.
- **The password never travels in clear form fields.** The page encrypts it
  client-side into `encryptedPwd` (`encryptedPasswordExpected=true`). `metadata1`
  is a device/browser fingerprint blob the page computes, and `appActionToken`,
  `workflowState`, `anti-csrftoken-a2z` and `prevRID` chain the steps together.
  So a sign-in has to run the real page; it can't be replayed from a template.
- **No OTP or captcha** was asked on this run (new account, emulator, Indonesian
  egress IP). The addon labels `/ap/cvf/*` and `/ap/mfa` steps when they appear.
- **The sign-in screens are WebViews running inside the app's own process,
  through Chromium's network stack.** Frida's `unpin.js` hooks don't reach that
  path, so these pages need the CA to be *genuinely* trusted by the system store.
  That is how the label bug surfaced: anonymous browsing had worked for days
  through the hooks, but the first tap on *Sign in* failed with "Please check
  your Internet connection" (logcat: `net_error -202`, `ERR_CERT_AUTHORITY_INVALID`).
- **The CA label bug, in detail.** The original `install_ca.sh` relabelled the
  overlay as `system_certificates_file`, a type that doesn't exist on this image,
  so `chcon` failed silently and the files kept `appdomain_tmpfs`. Root could
  read them (so the old check passed), but apps got `EACCES`. The overlay also
  lived only in init's namespace, which zygote doesn't inherit. Two approaches
  that do **not** work, for the record: bind-mounting a labelled directory from
  `/data` (the kernel treats the label as `unlabeled` there, so every new app
  crashed in `TrustedCertificateStore`), and remounting the store in running
  processes (they have already cached the broken store). Fix: overlay in
  zygote's namespace, label `system_security_cacerts_file`, then (re)start the app.
- **Screenshots of the sign-in screens are black.** `AuthPortalUIActivity` sets
  `FLAG_SECURE`; the screen shows normally in the emulator window.
- **Startup re-fork.** The app sometimes replaces its first process right after
  launch, so the Frida attach can miss (`could not attach: no pid`). Relaunch the
  app and attach again.
- **Don't kill a Frida session hard while it's attached.** Killing the client
  (`pkill`) left zygote aborting in a loop (`session socket read failed`) until
  the emulator was rebooted.

Redaction: the addon removes what was typed (`email`, `password`,
`encryptedPwd`, `customerName`, OTP fields) from `login.mitm`. `encryptedPwd`
was added after this run showed Amazon's field name, and the run's file was
re-scrubbed. Tokens and cookies are kept, which is the point of the capture.

#### Finding 9 — reviews with a signed-in session: 8 featured, no paging

Using the session from Finding 8 (`--auth-cookies android/flows/login_cookies.json`):

- **Login does unlock review text.** Anonymously, `/product-reviews/<ASIN>` has
  no review cards (and `/dp` shows none either). Signed in, the page greets the
  account by name and carries **8 full reviews**: id, rating, title, body, date,
  verified badge, helpful count, variant and display name all parse. Checked on
  two products (`B00G2XGC88`, `B083TCZDMT`).
- **The markup changed.** Each review is now `<li data-hook="review">` (it was a
  `<div>`), which made `parse_reviews` return nothing until the selector was
  widened to `[data-hook="review"]`.
- **Nothing beyond those 8.** `pageNumber`, `sortBy` (`recent`/`helpful`),
  `filterByStar` (even `one_star`) and `reviewerType` are all ignored: every
  variant returned the same eight 4–5★ reviews. The page has no pagination bar.
  Its config names an AJAX endpoint,
  `POST /portal/customer-reviews/ajax/reviews/get/` (form fields `asin`,
  `pageNumber`, `pageSize`, `sortBy`, `filterByStar`, `scope`, …, header
  `anti-csrftoken-a2z` = the page's `reviewsCsrfToken`). It answers `200` in
  Amazon's streaming format (JSON chunks `["append"|"update", "#selector",
  "<html>"]` separated by `&&&`). But for page 2+ it reports **"No customer
  reviews"**, empties `#cm_cr-pagination_bar`, and re-appends the same 8.
- So `reviews()` now stops at the first page that adds no new review id, instead
  of yielding the same 8 for every page requested.
- **The app gets the same 8.** Reviews exist only on the web plane: the app
  shows them in a WebView, and the native AAPI (`data.amazon.com`) serves none.
  Requested with the app WebView's own User-Agent (`…; wv) … Mobile Safari`),
  `/product-reviews/<ASIN>` and `/gp/aw/reviews/<ASIN>` return a mobile layout
  (`data-hook="mobley-review-content"`) with the **same 8 review ids** as the
  desktop page, the same `reviewsAjaxUrl`, and nothing new on page 2. The
  scraper stays on the desktop layout, which parses the same data more simply.
- **Not yet known:** whether this cap depends on the account (brand new, no
  orders), the egress region (geolocated to Indonesia), or is the same for
  everyone.
- **What each review card carries**, and what `parse_reviews` now records from
  it:
  - `images`: photos attached to the review (`img[data-hook="review-image-tile"]`).
    The thumbnails end in a resize directive (`…/I/714zIXoRKlL._SY88.jpg`);
    dropping it gives the original upload (`…/I/714zIXoRKlL.jpg`, here 134 KB
    JPEG).
  - `author_avatar`: the avatar shown on the card (`amazon-avatars-global/…`),
    or `None` when it is the default silhouette (7 of the 8 here).
  - `author_profile`: the profile link shown on the card
    (`/gp/profile/amzn1.account.…`), with the `/ref=` tracking suffix removed.
  - The profile itself is fetched separately with `profile()` (Finding 10).

#### Finding 10 — reviewer profiles are public, and carry the reviewer's reviews

`/gp/profile/<amzn1.account.…>` (the `author_profile` on each review) answers
**anonymously**: no sign-in needed, unlike review lists. The page embeds its data
as JSON in `<script data-a-state='{"key":"page-state-profile"}'>`:

- `profileModel`: `name`, `avatarImageURL`, `bio`, plus `isInfluencer`.
- `reviewsTimeline.shopItemModels[]`: the reviewer's reviews, each with `title`,
  `description` (full text), `rating`, `asinTitle`, `productImageUrl`,
  `visualElements` (attached photos and videos, already full size),
  `helpfulVoteText` and `viewOnAmazon` (→ the review id `R…`).
- `reviewsTimeline.nextPageToken`: empty on both profiles tried (19 and 20
  reviews), so how the timeline continues for prolific reviewers is still unknown.
  `parse_profile` returns the token as `next_page_token` so a non-empty one shows up.
- The timeline gives the product *title and image* but no ASIN.

`parse_profile` / `AmazonWebScraper.profile()` / `web-profile` turn this into one
record per profile, with `reviews` as a list.

#### Mining a capture yourself

```bash
python android/analyze_flows.py android/flows/session.mitm                 # endpoint table
python android/analyze_flows.py android/flows/session.mitm --host www.amazon.com
python android/analyze_flows.py android/flows/session.mitm --grep /suggestions --show 1
```

> Everything here runs on your own emulator, against an anonymous session or,
> for Finding 8, an account you created and signed into yourself, to *document*
> the protocol. It defeats no protection on anyone else's device and collects
> no one else's account data. Captured flows and any exported cURL files
> hold session cookies/tokens, so they are git-ignored — keep them that way.

---

## 2. The library

The collector is a small importable library. One class, `AmazonWebScraper`, mints
an anonymous session and exposes a method per surface:

```python
from scraper import AmazonWebScraper

with AmazonWebScraper.open("us", zip_code="10001") as amz:   # mints a session, no emulator
    for card in amz.search("coffee grinder", pages=1):       # -> dict rows
        print(card["asin"], card["price"], card["currency"], card["title"])
    detail  = amz.product(card["asin"])                      # -> dict
    ratings = amz.review_summary(card["asin"])               # average + star histogram
    seller  = amz.seller(detail["seller_id"])                # a seller ("company") page
```

Public API (`from scraper import …`):

| Method / function | Returns | Surface |
|---|---|---|
| `AmazonWebScraper.open(marketplace, zip_code=…)` | ready scraper | mints a session |
| `.search(keyword, pages=…)` | iter of dict | `/s` product cards |
| `.browse(node, pages=…)` | iter of dict | `/s?rh=n:` category |
| `.profile(profile_id)` | dict | `/gp/profile` reviewer profile + their reviews |
| `.product(asin)` | dict | `/dp` detail |
| `.review_summary(asin)` | dict | `/dp` rating + histogram |
| `.reviews(asin, pages=…)` | iter of dict | `/product-reviews` (login-gated → `AuthRequired`) |
| `.seller(seller_id)` | dict | `/sp` seller page |
| `.seller_feedback(seller_id, pages=…)` | iter of dict | `/sp` + `/sp/ajax/feedback` entries |
| `mint_session(marketplace, zip_code=…)` | session dict | raw session cookies |
| `parse_search / parse_product / parse_review_summary / parse_seller / …` | records | parse HTML you already have |

**Deep pagination.** Every paginated method takes `pages=N`, or `pages=None` to
follow the listing to its end (CLI: `--pages all`; `-v` logs each page). How each
one ends:

| Method | How it pages | Ends when |
|---|---|---|
| `search`, `browse` | `/s?…&page=N` | the page's *Next* button is disabled (Amazon's last page), or a page adds no new ASIN |
| `seller_feedback` | page 1 from `/sp`, then a `lastEvaluatedKey` cursor through `/sp/ajax/feedback` (JSON) | `hasNextPage` is false |
| `reviews` | `/product-reviews/<ASIN>?pageNumber=N` | a page adds no new review id: today after page 1, since Amazon ignores the page number (Finding 9) |
| `profile` | one page | no continuation seen yet: `next_page_token` was empty on every profile tried |

Every walk stops at the first page that adds nothing new (Amazon often repeats
its last page instead of returning an empty one), with a hard cap of
`MAX_PAGES` (500) as a backstop. Measured: `search("cast iron skillet")` →
7 pages, 313 unique products; a 400-page category (`browse("289814")`) runs all
400 pages, about 39 cards each, at the default 1 req/s; one seller's feedback →
27 pages, 131 entries (115 counted + 15 that Amazon suppressed from the rating +
1, flagged `suppressed`). Cursor-based feedback can't jump ahead, so
`start_page` > 1 walks from page 1 and skips.

```
scraper/
  __init__.py    the public API above
  client.py      HTTP/2 session · UA · rate limiter · retry/backoff · cookie jar
  session.py     mints an anonymous web session over HTTP (no emulator/browser)
  amazon_web.py  AmazonWebScraper + parsers · 12 marketplaces · currency-aware
  capture.py     parses a "Copy as cURL" request into a replayable form
  amazon.py      replay path for JSON endpoints (native AAPI, /suggestions)
  cli.py         optional thin CLI over the library (python -m scraper.cli)
example.py       runnable demo that prints a résumé of each method's output
run.py           convenience launcher for example.py
```

Other ways to build a scraper (instead of `open()`): `AmazonWebScraper(cookies=…)`
from a minted/saved session, or `AmazonWebScraper(template=load_curl_file(…))` to
reuse a captured app or logged-in session.

### Authenticated access (login-gated data, e.g. full review text)

Anonymous sessions can't read `/product-reviews` — Amazon redirects them to
sign-in. To read it you supply a session **you logged into yourself**; the library
never handles your password, OTP, or the sign-in challenge, and does not automate
login. You sign in with your own browser, export that session's cookies, and pass
them in:

```python
from scraper import AmazonWebScraper, cookies_from_file

cookies = cookies_from_file("amazon_cookies.txt")          # Netscape cookies.txt or JSON export
with AmazonWebScraper.authenticated(cookies, "us") as amz:  # verifies you're signed in
    for r in amz.reviews("B00G2XGC88", pages=2):
        print(r["rating"], r["title"], r["author"], r["verified"], r["images"])
```

`cookies_from_browser("chrome", "amazon.com")` can read them straight from your
local browser instead (needs the `browser` extra: `pip install -e ".[browser]"`).
`AmazonWebScraper.authenticated(...)` checks a product page for the signed-in greeting and raises
`AuthRequired` if the cookies aren't a signed-in session. The CLI mirrors this:

```bash
python -m scraper.cli web-reviews --auth-cookies amazon_cookies.txt --asin B00G2XGC88 --pages 2
```

**Or sign in on the emulator and record the flow.** `login_in_browser()` points
the emulator at mitmdump, opens Amazon's sign-in, and waits while *you* sign in
(or create an account) on the device. It prints each step as it happens and
returns the session once the auth cookies appear. Use `app=True` (`--app`) to
sign in through the Amazon app; that also records the app's device registration.
Without it, the emulator's Chrome is used:

```python
from scraper import AmazonWebScraper, login_in_browser

cookies = login_in_browser("us")             # app=True: use the Amazon app (Frida-unpinned)
with AmazonWebScraper.authenticated(cookies, "us") as amz:
    ...
```

```
[ 120.2s] signup                       GET  200 https://www.amazon.com/ap/register
[ 198.4s] signup                       POST 200 https://www.amazon.com/ap/register
                                       fields: …, customerName, encryptedPwd, metadata1, …
[ 422.6s] signin                       POST 302 https://www.amazon.com/ap/signin
                                       -> https://www.amazon.com/ap/maplanding
                                       set-cookie: …, x-main, at-main, sess-at-main   <- auth cookies
[ 424.0s] app registration (MAP)       POST 200 https://api.amazon.com/auth/register
```

```bash
python -m scraper.cli login --marketplace us            # or --app
python -m scraper.cli web-reviews --auth-cookies android/flows/login_cookies.json --asin B00G2XGC88
```

It needs the emulator up with the CA installed (`start_emulator.sh`, `install_ca.sh`;
plus `start_frida.sh` for `--app`). Run `install_ca.sh` before launching the app,
because a running app keeps the trust store it started with. What the capture
revealed is in §1, Finding 8. The capture goes to `android/flows/`:
`login.ndjson` (one line per request: phase, redirect, submitted field *names*,
cookies set), `login_cookies.json` (the jar) and `login.mitm` (full flows). The
addon ([`android/_login_capture.py`](android/_login_capture.py)) redacts what you
typed (password, email/phone, OTP codes, names) before anything is written. Session
tokens are kept, so treat these files like a logged-in browser profile. They are
git-ignored. Stopping with Ctrl-C before you finish signing in still keeps the
capture, which is how you record a sign-up on its own.

Each review row has `id, rating, title, body, date, verified, helpful, variant,
author, author_profile, author_avatar, images` (photo URLs at full size).

Each review's `author_profile` can be passed to `profile()` for the reviewer's
public profile; that works anonymously, no sign-in needed:

```python
with AmazonWebScraper.open("us") as amz:
    p = amz.profile("amzn1.account.XXXXXXXXXXXXXXXXXXXXXXXXXX")   # or the profile URL
    print(p["name"], p["avatar"], len(p["reviews"]))
```

```bash
python -m scraper.cli web-profile --profile amzn1.account.XXXXXXXXXXXXXXXXXXXXXXXXXX
```

Scope note: only *your own* session is used for sign-in.

Design points worth noting:
- **Resilience.** Transient `429/503/5xx` and connection errors are retried with
  exponential backoff; non-retryable `4xx` fails fast.
- **Politeness.** A thread-safe rate limiter caps the request rate.
- **Honesty.** A captcha/login wall raises `BotWall` / `AuthRequired` instead of
  silently returning garbage.
- **Currency-aware.** Every price carries a `currency`, so geolocated pricing
  (§1, Finding 7) is explicit rather than silently wrong.

---

## 3. Install & run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # installs the library + its deps (or: pip install -r requirements.txt)
```

**Run the demo** — mints a session and prints a résumé of each method's output:

```bash
python example.py          # US marketplace
python example.py uk       # any of: us uk de fr it es ca jp in mx br au
```

**Use it as a library:**

```python
from scraper import AmazonWebScraper

with AmazonWebScraper.open("us", zip_code="10001") as amz:
    cards = list(amz.search("coffee grinder", pages=2))
    print(len(cards), "results;", cards[0]["asin"], cards[0]["price"], cards[0]["currency"])

    product = amz.product(cards[0]["asin"])
    print(product["title"], product["rating"], product["reviews"])

    summary = amz.review_summary(cards[0]["asin"])      # anonymous aggregate
    print(summary["average"], summary["histogram"])

    if product["seller_id"]:
        print(amz.seller(product["seller_id"])["name"])
        for fb in amz.seller_feedback(product["seller_id"], pages=1):
            print(fb["rating"], fb["rated_by"], fb["comment"])
```

Parse HTML you already have, without any network:

```python
from scraper import parse_search, parse_product
rows = parse_search(open("some_search.html").read())
```

**Optional CLI** (a thin wrapper over the same library):

```bash
python -m scraper.cli web-search --keyword "coffee mug" --pages 2 --format ndjson
python -m scraper.cli web-search --keyword "coffee mug" --pages all -v      # follow to the last page
python -m scraper.cli web-product --marketplace uk --asin B0D1VN9691
```

### Politeness, metrics & benchmarking

Every `ScraperClient` is rate-limited and self-instrumenting:

- **Rate limiting** — a thread-safe limiter spaces requests at `requests_per_second`,
  with optional `jitter` so the cadence isn't machine-perfect.
- **Adaptive backoff** — a `429`/`503` widens the interval (`penalise`), and clean
  responses decay it back (`relax`); transient `429/500/502/503/504` and transport
  errors retry with exponential backoff.
- **Metrics** — `client.metrics` tracks requests, status mix, retries, throttles,
  rate-limit wait time and latency:

  ```python
  with AmazonWebScraper.open("us") as amz:
      list(amz.search("coffee", pages=1))
      print(amz.client.metrics.pretty())
      # requests=3 status={200: 3} retries=0 throttled=0 errors=0
      # elapsed=6.2s effective=0.48/s latency avg/min/max=1980/1511/2752ms
  ```

[`benchmark.py`](benchmark.py) verifies this behaviour **without load-testing
Amazon**: it checks the rate limiter's spacing and the adaptive backoff *offline*
(no network), and `--live N` measures latency/metrics over a **small, capped,
rate-limited** burst of real requests.

```bash
python benchmark.py                 # offline checks only
python benchmark.py --ramp          # ramp the rate against a LOCAL mock to find the edge
python benchmark.py --ramp --edge 25   # mock throttles above 25 req/s
python benchmark.py --live 8        # + a gentle, capped real-request sample
```

`--ramp` answers "how does the client behave as we push the rate up to the edge?"
*safely*: it starts a localhost server that returns `429` above a configurable
limit, then escalates the request rate against **that** and shows throughput climb,
plateau at the edge, and the retry/backoff response — e.g.:

```
target rps | sent | 200 | 429 | ok rps | retries
         2 |   40 |  40 |   0 |    2.0 |       0
         5 |   40 |  40 |   0 |    5.0 |       0
        10 |   41 |  40 |   1 |    7.9 |       1     <- edge
        20 |   44 |  40 |   4 |    6.5 |       4
```

> We deliberately do **not** push escalating load at Amazon to find *their* limit —
> probing a third party's anti-abuse thresholds at volume is abusive and against
> their ToS. `--ramp` finds the edge of a mock we own; the rest keeps *our* client
> polite and observable.

---

## 4. Ethics & scope

- **Anonymous by default, read-only.** Collection uses an anonymous guest session
  (minted or captured) and reads public catalogue pages. Login-gated data (full
  review *text*) is read only with a session *you* signed into yourself, by hand,
  in your own browser or on the emulator (`login_in_browser`). Without one it is
  left alone (`AuthRequired`). No one else's account data is touched.
- **Passing the bot wall.** The scraper requests the pages that answer a plain
  client, keeps the session, and rate-limits politely.
- **People.** Reviewer profiles hold personal data (name, photo, bio, review
  history). Collect only what your purpose needs, rate-limit, and check the data
  protection rules that apply to you (e.g. GDPR) before storing or combining it.
- **Your responsibility.** Scraping may conflict with Amazon's Terms of Service and
  local law; prefer the official PA-API / SP-API where they cover your need, and
  respect each target's ToS and robots directives before collecting.

---

## 5. Adapting to another target

1. Capture the target's requests (Methodology, §1).
2. Set the base URL + captured headers/token in `ScraperClient`.
3. Write a new adapter beside `amazon.py` mapping its endpoints to methods.
4. Reuse `client.py`, `capture.py`, and `cli.py` unchanged.
