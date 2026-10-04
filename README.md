# Seoul Index

The source code behind **Seoul Index (숫자로 보는 서울)**, [**@seoul-index.bsky.social**](https://bsky.app/profile/seoul-index.bsky.social), a Bluesky bot. Each post is a short set of real statistics, mostly from Seoul Open Data and otherwise from Statistics Korea, the OECD and other public publishers, rendered as a card image. A post goes out as a four-post thread: an English card, then a Korean one, each followed by a short reply carrying the clickable source and tags.

The account is written by A.I. and says so in its profile. This repository is published for transparency: The code here is exactly what composes and sends the posts.

English dates on this account run month first ("September 7", "August 31 to September 6", "3 p.m., August 21"), matching the Korean card beside each English one (9월 7일) and Seoul's own English site. Every English date goes through `en_date()` or `_span_en()` in `seoul_index_post.py`; `seoul_weather_post.py`, `seoul_index_books_harvest.py` and `seoul-transit-art/seoul_transit_post.py` (which also posts here) use the same order.

## Design principle: accuracy over wit

**Python owns every number.** It harvests the data, formats each value and detects the sharp juxtapositions. A `claude -p` step only *curates* (which lines, in what order, and a neutral opener), lightly rewords English labels and *translates* the labels to Korean. Claude never emits a numeric value: the poster reuses Python's exact value string in both languages, and a digit-guard rejects any Claude-written label that contains a figure's digits. So a hallucinated number cannot reach a post.

### The labels are checked too

The digit-guard covers the numbers, not the words beside them. `check_labels` runs at the end of `compose()`, after the trim and the opener rewrites, so it reads the wording the card will actually draw. It flags three things: an English label that no longer says what the source label says, a Korean label that says something different from the English, and a label that means nothing read with its opener. It never judges wit, brevity or a dropped unit conversion. Cross-vein collision cards are the structural weak point: they must take a neutral opener, so the metric has nowhere else to live if the label itself goes bare.

A flagged label falls back to the pool's own label, the same fallback `clean_label` uses for a label containing a figure's digits. A flagged **Korean** label falls back to the English source label: a line of English on a Korean card reads oddly, but is better than a Korean line making a claim the data does not carry.

Every verdict is logged to `label_checks.jsonl`, and a rejection is reported to `~/Scripts/observe.py` where that exists. A check that cannot run is not a failure: the card goes out unchecked and the log says so.

### Is the Korean card in Korean?

`check_korean()` runs on every card before it is drawn and flags any Korean label or opener carrying **no Hangul**. The model checker cannot catch this (an English label does say what its figure is), so a regex owns it: deterministic, no network, and it runs whether or not `CHECK_LABELS` is on. It reports and repairs nothing: for veins the selector translates, its answer is the only Korean that exists. Veins that own their Korean (crowd, spotlight, rush set `label_ko`) never reach the selector for it. It never blocks a post, and there is no exemption list. The range is Hangul syllables, not CJK ideographs, so a label of pure Hanja is flagged too.

### Bolding the variable

A card whose rows are **one metric read at several places** bolds the place and leaves the shared wording plain: *Estimated crowd,* **Gangnam Station**. The decision is made from the labels, per card: cut each line's place out of its label, and bold only if every remainder is identical and non-empty. Four *Visitors to X* qualify; the same vein mixing in *Busiest subway station, Gangnam* does not.

- **The remainder must be non-empty.** River and water label rows with bare names, so cutting the place would leave nothing and the whole card would come out bold.
- **All rows or none, judged per language.** A label that does not contain its own place (the selector reworded it) takes the whole card back to plain.
- **`_strip_live_frame`** removes *Estimated* and a trailing *right now* from live lines on a grouped card, since the live group's subhead reads *Right now* and an estimated line always puts the KT caveat in the footnote. It applies to live lines only: on a dated line those words are the card's only copy. Pinning a live label does not exempt it.
- **The crowd label is pinned in both languages** (`pin` for English, `label_ko` alongside it), since a place cannot be bolded while the wording around it moves.

## How a post is built

1. **Harvest** a pool of candidate facts from the live and cached data sources (see below). Each fact carries an exact, pre-formatted value.
2. **Select** with `claude -p`: it picks 3 to 4 lines that form a coherent set, preferring to build around one pre-detected pair, and writes a neutral opener plus Korean labels. This call and the label check run `claude -p --restricted --tools ""` (`CONFINED` in `seoul_index_post.py`): no tools at all, since unconfined `claude -p` is an agent with a shell and a selector that only returns JSON has no use for one.
3. **Compose**: Python stitches the chosen labels back onto its own exact values, adds the source line and tags, and enforces the character limit. Wording that every line repeats is trimmed, so the metric is named on the first line and each later line carries only what differs ("Estimated crowd in Jamsil", then "In Hongdae"), and anything the opener already says is dropped. English trims the leading run and Korean the trailing one, since Korean puts the head last.
4. **Render and post**: each index is drawn as a card image, and the thread goes out as the English card, a reply with its clickable source and tags, the Korean card, then its source reply. Each card's full text is its image alt text. The `boxhist` card is the one exception (`credit_on_card` in `compose()`): it has no dateline of its own, so the credit rides the card as the muted footnote under the rows, and the thread is just the two card posts with no source replies.

### Cross-vein collisions

The pairs pre-detected within a single source are the two ends of one distribution (the dearest and cheapest flat, the fullest and quietest crowd). The sharper Harper's-Index move is a *collision*: two figures from **unrelated** datasets that land on nearly the same number. A whole quarter of one Seoul industry set beside a single apartment's deposit; a month of visitors to a palace beside the crowd on a café street right now.

A pass over the whole pool (`cross_vein_pairs`) finds these and offers the selector one to build around, as a sanctioned exception to the rule that each source keeps its own post. It only pairs like with like (two ₩ figures, or two head-counts), so money is never set against people. Only the detector reads a fact's raw magnitude; Python still owns every posted number. When a post spans two sources, both are credited on the source line and both provisos ride the card footnote. `python3 seoul_index_post.py --show-cross` prints the collisions the live pool currently holds.

A crossed card carries **two frames at once**, so each side gets its own red subhead with its lines beneath it: the scoped lines under their month, or under the descriptor that says what they count, and the live ones under "Right now". The live labels shed the framing the subhead now carries, so "Estimated crowd in Gwanghwamun right now" becomes "Gwanghwamun". A scope printed as a masthead under the title is a claim about **every line below it**; on a crossed card most of those lines belong to the other vein, and the opener is generic, so the subhead is the only place that covers the right lines and no others.

`DESCRIPTOR_SCOPES` is the single source of those words: `compose()` reads it for the footnote and for the subhead alike, so a vein cannot be listed in one place and not the other.

### Ranked veins on a shared card

`nightbus`, `stations`, `stationgap`, `seoulstation`, `railstations` and `railcommuter` (`RANKED_CROSS_CATS`) are each a normal own-post ranked card, but their lines all count **people**, the same unit the live crowd vein counts, so they can legitimately land beside a crowd line or beside each other.

Such a card groups exactly like a collision card. Each ranked vein heads its own group under a subhead built from its own dateline: a bare weekday date for `nightbus` and `stationgap` (their lines already say what they count), a worded one for the rest ("Subway boardings, Friday, September 18"; `stations`' is assembled in `compose()` from `STATION_DAY`). Crowd lines, if any, sit under "Right now". The footnote merges every ranked vein's note in the card's line order, dropping a "latest date" sentence an earlier part already carries; the opener stands down to generic. The one shape still unhandled is crowd beside **two** ranked veins at once: neither subhead lifts, nothing groups, and both dates stay in the footnote.

## Two kinds of card

Most posts set things against each other across the city. About one post in five (a random draw, never two in a row; `SPOTLIGHT_EVERY`) instead drills into **one place read along a clock**: the crowd right now, what that place is usually like at this hour on this weekday, and the busiest and quietest hours ahead, cycling through curated spots. A spotlight card never reaches the selector, so its share of the schedule is a share no other source can occupy.

A place falls back to a normal post when it does not answer with enough lines, or when its readings are the same number wearing different labels: the crowd figures arrive in coarse pre-rounded buckets, so at quieter spots the present hour, weekday average and peak ahead often round to the same value. A card must carry at least three distinct values, spread at least a quarter of the largest (`SPOTLIGHT_MIN_DISTINCT`, `SPOTLIGHT_MIN_SPREAD`).

The spotlight card needs no `claude -p` call: its lines are fixed and in order, and their labels carry clock times, which Python does not hand over to be reworded. Its opener names the place in both languages from the curated list. It is headed "hour by hour" rather than "today" because `citydata_ppltn` knows only the present and the next 12 hours, so the peak and trough are the busiest and quietest hours **ahead**, and the footnote says the later hours are forecasts. The "usual for a Monday at this hour" line comes from the bot's own logged observations (`crowd_history.jsonl`) and does not appear until three separate weeks have been recorded.

A topical emoji leads the opener, and per-line emoji are added only where an obvious one fits; a guard rejects any number or keycap emoji so figures stay Python's alone.

### Making sure every source gets used

These rules keep the feed off the few sources the selector finds most attractive. All are enforced in Python rather than asked of the selector.

**Rotation** keeps two consecutive posts off the same metric.

**Cooldowns** hold back a source for a set number of days after it posts. They live in one table, `COOLDOWNS` in `seoul_index_post.py` (`cat -> (days, label)`), stamped after each post as `last_<cat>_at`; adding a vein with a cooldown is one line there. Typical reasons:

- `world` (3 days): the OECD vein holds the widest gaps in the pool and would otherwise win nearly every time it is offered.
- `spending` (3 days): quarterly sales are frozen, so their pre-detected pair is the same two categories for months.
- `bike`, `traffic`, `transport`, `national`, `tourism` (3 days): each `facts()` hands the selector one fixed structural pairing, so the card is nearly always the same one. (`national`, KOSIS, is a different vein from `nation`, the World Bank one, which is cooled inside `nation_facts()`.)
- `infra` (30 days): bus stops, car parks, libraries and parks are registry sizes that barely move.
- Ranked and monthly veins carry their own: 3 days for `busroutes`, `busstops`, `stations`, `railstations` and `wxday`; 7 for `nightbus`, `busweekend`, `stationgap`, `seoulstation`, `rescue` and `kopis`; 28 for the four KEPCO cards and `railcommuter`.

`busroutes`, `busstops` and `stations` carry a **change override** (`apply_cooldown_unless_changed`, `LEADER_CATS`): each records its leader (a route, a stop, a station) when it posts, and if the leader has changed when the vein is next offered, the card goes through early with a footnote sentence saying so. An unchanged leader waits out the cooldown.

**A vein floor** does the reverse. Left unmanaged, a large share of harvested facts would never reach a reader, because the selector's preference for pre-detected pairs and collisions clusters on the live and quarterly sources. So when a source has not led a card for `STARVE_DAYS` (12) days, the pool is narrowed **to** that source for one card. Never-published sources go first, then the longest-neglected. Two promotions do not normally run back to back, with two exceptions: a source that has never led a card, and the single most-overdue vein once it has waited `SEVERE_STARVE_DAYS` (24). A source with fewer than `STARVE_MIN_FACTS` (3) facts can never be promoted, and says so in the log. `STARVE_DAYS` should be re-derived as veins divided by slots a day if the roster changes much: set too low, every run becomes a promotion and the feed turns into a strict round robin.

**A repeat guard** keeps two memories. First, a new card may share at most `CARD_OVERLAP_MAX` (2) line ids with any of the last `RECENT_CARDS_KEEP` (12) cards, about three days of posts; when one does, the offending lines are dropped and the selector is asked again, up to three times, after which it posts anyway. It counts shared lines rather than demanding an exact match, because a source can produce near-identical cards that share their top lines without being duplicates. Second, since a vein's turn comes round roughly every twelve days, well outside that window, `last_card_by_cat` remembers each vein's own last card however old, keyed by **content** (a line's label and figure, not its id, since one attraction can appear under two ids with the same label and number). A card saying exactly what one of its veins last said is rejected; the same lines with a moved figure are new. A cross-vein pair is recorded under its primary vein only.

## Card images

Each index is rendered to a PNG by `seoul_index_card.py`: the card is laid out in HTML, screenshotted with headless Google Chrome, then cropped to content with Pillow. Color emoji and Korean text come from the system fonts, and the look is monospace on cream to match the avatar. A caveat that qualifies the numbers rather than credits them ("Crowds are KT-estimated", or "Metro areas, 2023" on a world card) sits in a muted footnote on the card; the source credit stays in the reply below, where it can be a real clickable link (except on `boxhist`, above). If rendering fails, the poster falls back to a plaintext thread, so a post always goes out. The pinned methodology thread is built the same way, as prose cards, by `seoul_index_methodology.py`.

## Data sources

- **[Seoul Open Data](https://data.seoul.go.kr)** (CC-BY): live crowd estimates (KT mobile-signal based, disclosed as estimates), air quality, subway and bus boardings, infrastructure counts, quarterly commercial-district sales, the public-bike system counted live citywide (bikes at a dock, docking points, stations, stations standing empty), and live road speeds on named arteries. Average-bill lines are sales divided by transactions, so they are what one payment came to, not what one person spent.
- **Seoul Open Data, bus routes** (`CardBusStatisticsServiceNew` via `bus_route_history.json`, own "busroutes" category, own post): the day's busiest, second-busiest and quietest bus routes **by boardings per stop served**, plus the citywide average on the same measure. Raw boardings would rank by route length rather than crowding. Trunk and branch routes only (100-799, 1000-7999) with at least `BUS_MIN_STOPS` (10) stops served. **Stops served are distinct stop ids, not feed rows**: some routes carry more rows than stops, and the history stamps its stop counts with `STOPS_RULE` so an older fill can be rebuilt. Lines carry bare route numbers. The footnote states the eligibility rule, the stop counts behind the top two and any active streak, read from the history file. A route map rides as a fifth reply.
- **Seoul Open Data, subway stations** (`CardSubwayStatsNew`, own "stations" category, own post): the day's busiest, second-busiest and quietest subway stations plus the day's total boardings, with a map of the three as a fifth post (`render_station_map()`). **Rows are summed per station across lines** (the feed is one row per station per line); the transport vein's busiest/quietest lines use the same summed ranking, so the two cards cannot disagree. **Stations inside Seoul only**, by distance to the nearest Seoul-registered bus stop (`STATION_IN_SEOUL_KM` 0.3, against `subwayStationMaster`), since the feed reaches Gyeonggi. Lines carry bare station names. All three need an official English name from `seoul_index_names_en.json` or the card withholds.
- **Seoul Open Data, air** (`ListAirQualityByDistrictService`): monitors reporting, worst PM2.5 district, cleanest PM2.5 district, and a count of districts the city's own index rates 좋음 ("19 of 22"). A monitor under maintenance (점검중) is left out of both extremes and the count.
- **Seoul Open Data, one station both ways** (`CardSubwayStatsNew`'s alighting column, own "stationgap" category, own post): the Seoul station with the day's widest gap between people getting off and getting on, as two figures, larger first. Fixed opener, the day on the dateline, the footnote saying how the station was chosen. **The gap is absolute, not a ratio** (a ratio would favor a near-empty halt), and inherits the stations card's summed, inside-Seoul rules. A row missing an alighting figure is left out rather than treated as zero. The difference itself is never printed.
- **Seoul Open Data, bus stops** (`CardBusStatisticsServiceNew`, own "busstops" category, own post): the day's three busiest bus stops and how many stops took at least one boarding, with a pin map as a fifth post. **The unit is one stop (one ARS number), and a name appears once**: some named locations are several distinct stops, so where two share a name the busier is shown. Seoul stops only (ids beginning '1'). No quietest line, since too many stops tie near zero. **English names come from the station table, never a romanization**: no Seoul feed carries English stop names, so a stop named after a station borrows that station's official English, and a top-three stop that isn't withholds the card.
- **Seoul Open Data, bus routes against their own past** (`CardBusStatisticsServiceNew` via `bus_route_history.json`, three own-post categories): a per-route daily history (`transport_facts()` appends on every fresh day; `bus_route_history_backfill.py` fills the backlog from the feed, which goes back to 2024). **Public holidays are excluded from every baseline and never themselves compared** (nager.at's Korean list, cached in the same file); a missing holiday table withholds. Trunk and branch routes only, over a floor of 1,000 baseline boardings. Each posts a route map as a fifth reply.
  - `busmovers`: the day's largest rise and fall against each route's median of its previous same weekdays (up to 8, at least 3), as a signed percentage. Fixed opener. No cooldown.
  - `busweekend`: the latest complete holiday-free week, each route's weekend-per-day boardings against its weekday-per-day: the route that holds up best and the one that falls most. Weekend ridership is usually down citywide, so the top is typically small or flat and the bottom a sharp fall. Lines carry "Route".
  - `nightbus`: busiest, second-busiest and quietest N (night) route plus the night total, ranked within that class. Footnote "Night routes only".

  Covered by `BusHistoryCards` in `test_seoul_index_veins.py`; the tests redirect `BUS_HISTORY` to a temp file, since `transport_facts()` writes it.
- **Seoul Open Data, rush hour** (`CardSubwayTime`, monthly per-station, per-hour boardings): one subway station's own morning/evening swing, at whichever two hours it peaks. The station bolds and the opener is fixed ("Boarding the subway"). The side shown **alternates am/pm by run**, since the most extreme stations by ratio are all evening-heavy office stops. `RUSH_FLOOR` (300,000 monthly boardings) keeps small stations out. A rush card can be two lines rather than three.
- **Seoul Open Data, market prices**: what one everyday item costs at shops across the city on one day, from the price observations the city has collected since January 2025 and refreshes weekly. Both ends are published prices at named shops, so the spread is quoted rather than calculated. Shops are identified by district and kind (market or supermarket), never by name. An item whose dearest is less than one and a half times its cheapest is skipped.
- **Seoul Open Data, day and night**: how many people are in each district by day or by night, from the city's district aggregate of the 생활인구 series. A card is one half or the other, never both. These count everyone present, not residents, and are KT-modeled estimates, which the card says.
- **Seoul Open Data, waterworks**: the raw water drawn at each of the five purification centers, yesterday. Intake only; transmission and supply figures are not mixed in.
- **Seoul Open Data, children**: how many children Seoul has at one age, across a decade. This feed's own field labels are unreliable (a row named "count" holds a percentage and "ratio" holds the count), so the harvester keys on the numeric row code and reads only rows verified to be whole numbers.
- **Seoul Open Data, library membership**: who holds a card at Seoul Library, the city's flagship, by decade of life. Each count carries a **"1 in N"**: members of that band against Seoul's registered population of the same age, from KOSIS (`DT_1B04005N`, two five-year bands summed to a decade). The population month rides in the footnote, not the period slot, since the membership figures carry no date. **The numerator is not a subset of the denominator**: membership is open to residents of all Korea and to people who work or study in Seoul, and the API has no member-class field. So the card states a ratio, never a share, and the footnote says "Members need not live in Seoul". A KOSIS outage costs the ratio only.
- **Seoul Open Data, complaints**: how many faults residents reported to the city in a whole year. Complete years only, since the running year's current-month slot holds a year-to-date total.
- **Seoul Open Data, library loans** (`SeoulLibraryBookRentNumInfo`, harvested weekly into `books_agg.json`): what Seoul Library lent over the last 60 days, counted **by subject** across the ten KDC classes. Titles are never named; the opener names the library. Each count carries a "1 in N": its share of every checkout counted, never of all checkouts, and the footnote states the total. Two detectors ride along: a dead heat (two subjects within 2%) and the least-to-most gap. Caveats:
  - **No dateline**: the period is the rolling 60-day window stated in the footnote, so `books_facts()` refuses to speak if the window or record count is missing.
  - **Classification is KDC, not DDC** (KDC's 4 is natural science, DDC's is language), verified against 서울도서관's own category filter. Labels are the library's own words; 기술과학 is glossed "Applied sciences".
  - **The feed is a cut**: `list_total_count` is exactly 3,000, so the card reads "Seoul Library's 3,000 most-borrowed items, last 60 days". Figures are checkouts per record, not titles, and nothing is de-duplicated.
  - **Every record must land in a class**: an unreadable `CLASS_NO` is bucketed `unclassified`, and the harvest aborts above 5% unclassified.
  - Diffing two harvests does not produce a calendar month: the window rolls, so a delta can go negative.
  - [도서관 정보나루](https://data4library.kr) (citywide, all public libraries, by calendar month) was the earlier source; its key returns `vitalizationErr` (an account-activation issue) on every call. Worth reinstating if activated.
- **[KOSIS / Statistics Korea](https://kosis.kr)**: national-contrast lines (Seoul's share of the country's population, and the total-fertility-rate gap) and the denominator behind the library "1 in N". Credited on its own source line.
- **[OECD](https://data-explorer.oecd.org)** (SDMX, no key): Seoul against eight peer cities (Tokyo, Osaka, Paris, London, New York, Berlin, Madrid and Amsterdam) on green space per person, share of people within a 5-minute walk of a transit stop, summer-night urban heat island and population density. A measure is used only when Seoul and at least two peers report in the **same year**. An OECD functional urban area is not the city: Seoul's is the whole capital region, roughly 24m people against the city's 9.6m, so a world card carries "Metro areas" and the year in its footnote.
- **[World Bank](https://data.worldbank.org)** (no key), with Seoul's own figure from KOSIS: Seoul against whole **countries** on one measure at a time (density, birth rate). Seoul leads the card; the other lines are bare country names, so the opener carries the measure, and the year and scope ride the footnote.
- **[MOLIT 실거래가](https://rt.molit.go.kr)** (via [data.go.kr](https://www.data.go.kr)): one month's apartment-market filings: the dearest and cheapest single sales, a record jeonse deposit, and counts of filings citywide, by district, and jeonse against monthly rent. Every line is a filed transaction or a count of them, never a median or average, and canceled filings are excluded. Filings are due within 30 days of a contract, so the bot uses the newest month that can no longer grow (two months back).
- **[Frankfurter](https://api.frankfurter.dev)** (ECB daily reference rate, no key): the KRW→USD conversion behind the "$1 ≈ ₩N" footnote on won-denominated cards. Cached once per KST day; a failed fetch reuses the cached rate, and the footnote is omitted if no rate has ever been cached.
- **[KMA](https://data.kma.go.kr)** (기상청, via data.go.kr): daily readings from station 108, Seoul's reference station, observing since 1907: the last full month against the same month fifty years earlier (hottest day, wettest day, counts of days meeting a stated criterion) and, in summer, a season-to-date tally of swelter days (33°C+) from 1 June against the same span fifty years back. Extremes are published rows and the rest is counting, never computed means. A then-and-now card **groups by metric**: the criterion is a red subhead with the two periods bolded beneath it, refused unless every line carries the split and every metric has at least two rows.
- **KMA, yesterday** (`AsosDalyInfoService`, station 108, own "wxday" category, own post): yesterday's high, low and rain, plus sunshine and snow when it fell. Fixed opener, the date on the dateline. **The rain field is blank on a dry day, not 0.0**, and prints "None"/"없음"; a missing high or low withholds the card.
- **Seoul Open Data and KMA together**: hourly water temperature in the Han (at Seonyu) and the Tancheon, Jungnangcheon and Anyangcheon, against the air temperature over central Seoul at the same hour. The vein stays silent when the warmest and coolest readings are under 3°C apart. Seonyu publishes about five hours behind the tributaries, so the harvester finds the newest hour the stations agree on and asks KMA for the air at that hour.
- **[HRFCO](https://www.hrfco.go.kr)** (한강홍수통제소): the Han's level at Jamsu Bridge against that gauge's own flood-warning tiers (관심, 주의, 경계, 심각). Appears only once the river reaches the first tier. Seoul's sixteen gauges each read from their own datum, so one gauge against its own tiers is the only honest arrangement. The tiers are flood-warning levels, not the level at which the bridge walkway floods.
- **[Korea Airports Corporation](https://www.airport.co.kr)** (via data.go.kr): Gimpo's monthly transport row: passengers and flights, the same month twenty years earlier, and the domestic/international split (each a published row via the route filter, never a subtraction). A month publishes from the 5th business day of the next.
- **[Incheon International Airport Corporation](https://www.airport.kr)** (via data.go.kr, `operationPerformanceByRoute`): Incheon's newest published month: total passengers, total flights and the busiest destination country, each a sum of the API's per-airline, per-route rows. A snapshot only, with no month parameter, so no then-and-now pair. The sibling `AviationStatsByAirport` stopped updating after October 2024.
- **[Korea Railroad Corporation](https://www.korail.com)** (한국철도공사, via data.go.kr): the busiest intercity route (KTX/새마을/무궁화, one month), the busiest commuter line and the citywide commuter total. **Both operations return about a year of history with no date parameter**, so the harvester keeps only the newest `run_ym` before summing or ranking.
- **Korail, stations** (`mainLineStationPer`, own "railstations" category, own post): Seoul's four busiest Korail stations by boardings on the newest published day. This feed is daily, with no date parameter, so the harvester fetches several days and keeps the newest. Seoul membership uses the stations card's inside-Seoul test; the footnote states the roster is 8 stations and that 수서 (SRT, another operator) is absent.
- **Korail, Seoul Station** (`mainLineStationPer`, own "seoulstation" category, own post): one station's newest day: boarded, got off, passengers together, and "A typical Tuesday" (the median of the previous same weekdays, at least 3). **Same weekday, never the last N days**, since weekday and weekend traffic differ sharply. The dateline names a public holiday when the day falls on one. `korail_station_history.json` keeps daily figures beyond the feed's roughly one-year window (`korail_station_history_backfill.py` filled the first year); the card always uses the newest day fetched this run.
- **Korail, commuter rail in Seoul** (`wideRailloadStationPer`, own "railcommuter" category, own post): the newest month's boardings at Korail commuter-rail stations inside Seoul: the busiest three and the total, with a pin map. Membership uses the stations card's inside-Seoul test, cached monthly in `railcommuter_cache.json`. **A bracket naming another city excludes the row**, so another city's station of the same name is never credited to Seoul; a line bracket (서울(경의선)) folds onto its station.
- **[Animal and Plant Quarantine Agency](https://www.animal.go.kr), rescued animals** (`abandonmentPublicService_v2`, data.go.kr 15098931, own "rescue" category, own post): one week of rescue notices filed by Seoul's 25 districts: all, cats, dogs, other. **A notice lags its rescue**, so the rolling window ends `RESCUE_LAG_DAYS` (3) before the build. `bgnde`/`endde` filter on the rescue date; `upr_cd=6110000` is the register's own Seoul code. A zero-row week withholds, since it means an outage.
- **[KOPIS](https://www.kopis.or.kr), performances** (`prfstsArea`, the national box-office register for the performing arts, own "kopis" category, own post): one week on Seoul's stages: productions, productions that opened, performances given, tickets sold net of cancellations and box office. Its key is KOPIS's own. The gateway needs a browser User-Agent and a redirect follow. **Day rows add up on showings and tickets but not on productions**, so the week is always one call.
- **[KEPCO](https://bigdata.kepco.co.kr), electricity** (`powerUsage/contractType`): **kepco** is the newest month in Seoul: customers, electricity used, the households' and shops-and-offices shares, and the bill, summed over 25 districts × 7 contract types. **kepcohist** sets the same month against the same month twenty years earlier as three period pairs. No per-household figure is computed here (see `kepcohouse`). About two months behind; an unpublished month answers 404, so the newest month is found by walking back, and a month under `KEPCO_MIN_ROWS` (150) rows is refused as partial.
- **KEPCO, household averages** (`powerUsage/houseAve`, own "kepcohouse" category): the newest month's per-household electricity by district: highest/lowest per-household kWh and average bill, every figure KEPCO's own. The month must be zero-padded in the URL; a bare digit answers 404 like an unpublished month.
- **KEPCO against Hong Kong** (own "kepcohk" category): four lines, per capita and per household, Seoul against Hong Kong. Seoul's year runs to KEPCO's newest month, with population from KOSIS's registered count; Hong Kong's is its newest published year. **Hong Kong's figures are hand-checked constants**, read from CLP Power's ESG Databook (CLP serves over 80% of Hong Kong's households), because its host refuses scripted fetches. All four lines post or none.
- **[HIRA](https://opendata.hira.or.kr)** (건강보험심사평가원, via data.go.kr): patients per condition at Seoul care institutions, from adjudicated health-insurance claims, for the newest complete published year. The footnote notes the region is where the institution is, not the patient, and counts are insurance claims only. Conditions are curated for recognizability and honesty.
- **[MCST](https://www.mcst.go.kr)** (문화체육관광부's culture-facility survey, via data.go.kr): Seoul's museums and galleries: the counts and each year's most-visited houses. The survey lags a year, which the footnote says.
- **[KCTI](https://know.tour.go.kr)** (한국문화관광연구원, via data.go.kr): monthly visitor counts at paid-admission Seoul attractions (the palaces, Lotte World, Seoul Sky), with foreigner counts as their own frame. The harvester walks back to the newest month with rows, and rows are curated to a whitelist, since the raw feed carries closure artifacts.
- **[KOFIC](https://www.kobis.or.kr/kobisopenapi)** (영화진흥위원회, its own key): one day's cinema **admissions on Seoul screens**, film by film (`wideAreaCd=0105001`). **Dropping the region parameter silently returns national rows in the same shape**, so a test asserts it is present. Each film costs an extra call for its English title; a film with none on file is dropped rather than romanized. **The card is the day's top four, complete and in order, or no card**: `complete_boxoffice` fills in anything the selector leaves out, and the harvester refuses a day it cannot fill. A second frame, **`boxhist`**, is its own card: how many Seoul screens the day's number-one film was on, against the same date five and ten years back (`SCREENS_YEARS`, kept inside the era of near-complete ticketing coverage, from about 2008).
- **[OpenStreetMap](https://www.openstreetmap.org/copyright)** (ODbL), via Overpass: English names for subway stations and districts. The Seoul feeds return Korean names only, and mechanical romanization gets official names wrong (홍대입구 is "Hongik Univ.", 시청 "City Hall"). Harvested once and committed, so no post depends on Overpass being up.

**Labels lead with what the number means.** A place name is never the whole label: "Most paid for an apartment (Yongsan-gu)", "Dearest, a traditional market (Gangdong-gu)", "Fullest by night (Songpa-gu)". The English card reaches the reader least able to place a district from its name, so the line must be legible without the geography.

**A Korean proper noun on the English card carries its category, once.** Four veins (river, traffic, water and world) deliberately use bare-name labels so parallel lines stay scannable, so each card names the KIND of thing once somewhere on its face, never on every line:

- **water**: on the dateline, "Purification centers, August 3".
- **river**: in the footnote, "The Anyangcheon, Tancheon and Jungnangcheon are tributaries of the Han", built from the lines actually on the card (the dateline is taken by the reading hour).
- **traffic**: carried by the metric (km/h under an opener about how fast Seoul is driving); nothing added.
- **world**, **nation**, **books**: city names, country names and subject classes are already English.
- **crowd**, **spotlight**, **price**, **transport**: the category rides the label ("Estimated crowd in Hongdae", "Busiest station, Gangnam").

When adding a new bare-name vein, settle which slot carries the category before it posts: the dateline if free, otherwise the footnote.

**Temperatures and speeds carry an imperial conversion on the English card only**: "26.5°C (80°F)", "26 km/h (16 mph)".

Every post hyperlinks its source.

## Files

| File | Purpose |
| --- | --- |
| `seoul_index_post.py` | Harvest, select, compose, render and post one index (English + Korean card thread). |
| `seoul_weather_post.py` | Daily companion: posts today's Seoul forecast (KMA 단기예보) on its own schedule, outside the rotation. No selector step: a forecast card is fixed numbers built by Python in both languages. Shares the account's config and Bluesky credential. |
| `seoul_index_card.py` | Render an index or prose card to a PNG (headless Chrome, cropped with Pillow). |
| `seoul_index_methodology.py` | Post the pinned methodology thread as prose cards. `--replace` posts the new thread, pins it, then deletes the previously pinned one, only after the replacement is up and only if it has the shape this script posts. |
| `seoul_index_sales.py` | Monthly full scan of the commercial-district sales dataset into `sales_agg.json`. |
| `seoul_index_books_harvest.py` | Weekly harvest of Seoul Library's 60-day loans by subject into `books_agg.json`; aborts if it cannot read the library's published window. |
| `seoul_index_crowd_log.py` | Crowd sampler, hourly from 05:00 to 23:00; appends readings to `crowd_history.jsonl` so the bot can say what a place is usually like. |
| `bus_route_history_backfill.py` | Fill `bus_route_history.json` from the bus feed's backlog. |
| `korail_station_history_backfill.py` | Fill `korail_station_history.json` from the Korail station feed. |
| `net_guard.py` | Waits for a route out before harvesting, so a post is delayed rather than lost when the machine wakes without a network. |
| `limit_guard.py` | Waits out a spent `claude -p` quota on the selector call. Shared byte-identical copy, like `net_guard.py`. |
| `api_call_log.py` | Logs every outbound call (curl, `requests`, `httpx`) with its target host to the shared `~/Scripts/api_calls.jsonl`. Installed only from `seoul_index_post.py`'s `__main__` block. Shared byte-identical copy. |
| `test_*.py` | Tests (selection, veins, labels, cards, methodology, books, rush, weather and more). No network, no model call, nothing posted. |
| `seoul_index_names_harvest.py` | Regenerate `seoul_index_names_en.json` from OpenStreetMap. Run occasionally: stations open a few times a year. |
| `seoul_index_names_en.json` | Korean → English names for stations and districts, so the English card carries no Hangul. |
| `seoul_index_config.example.json` | Template for the gitignored `seoul_index_config.json`. |
| `seoul_index_avatar.svg` | The account avatar. |

## Setup

Requirements: Python 3, the [`atproto`](https://pypi.org/project/atproto/) and [`Pillow`](https://pypi.org/project/pillow/) packages (`pip install atproto pillow`), `curl`, Google Chrome (for headless card rendering), and the [Claude Code CLI](https://claude.com/claude-code) for the `claude -p` selector.

### API keys

All set in `seoul_index_config.json`. Only the first is required; without any of the others the bot still runs and the veins that need it simply never appear.

- **Seoul Open Data** (`api_key`, required): the source for most veins. Register at [data.seoul.go.kr](https://data.seoul.go.kr/) and request a general authentication key (일반인증키). One key covers every Seoul service the bot calls.
- **KOSIS** (`kosis_key`): national-contrast lines and the library "1 in N". Request an OpenAPI key at [kosis.kr/openapi](https://kosis.kr/openapi). The key is base64 and ends in `=`; keep the trailing character.
- **공공데이터포털** (`data_go_kr_key`): the apartment-market, weather, airport, health, culture, tourism, Korail and rescued-animals lines. Register at [data.go.kr](https://www.data.go.kr/); the account gets ONE key, but each API needs its own 활용신청 (usually instant, 자동승인) before the key works against it, including 아파트 매매 실거래가 (15126469), 아파트 전월세 실거래가 (15126474), 지상(종관, ASOS) 일자료 (15059093), 전국공항 수송실적통계 (15158834), 질병정보서비스 (15119055), 전국문화기반시설총람 (15125097), 관광자원통계서비스 (15000366; its openapi.tour.go.kr gateway can take overnight to register a new key), 단기예보 조회서비스 (15084084, live air temperature on the river cards) and 국가동물보호정보시스템 구조동물 조회 서비스 (15098931).
- **도서관 정보나루** (`data4library_key`): currently unused (see library loans above); nothing breaks if it is removed.
- **KEPCO 전력데이터 개방 포털** (`kepco_key`): the four electricity cards. Sign up at [en-ter.co.kr](https://en-ter.co.kr/ft/login/join.do?siteCode=B) as 개인회원 (phone 본인인증), then on [bigdata.kepco.co.kr](https://bigdata.kepco.co.kr) go 데이터공개 → OPEN API → API 인증키 신청 바로가기; the key is issued on the spot.
- **KOPIS** (`kopis_key`): the performances card. Apply at [kopis.or.kr](https://www.kopis.or.kr/por/cs/openapi/openApiUseSend.do?menuId=MNU_00074) through the 인증키 발급신청 form (no account needed); the key arrives by email.
- **한강홍수통제소** (`hrfco_api_key`): the river-level lines. Register at [hrfco.go.kr](https://www.hrfco.go.kr/) and request a key; click the activation link in the emailed key before use, or every call returns `{"code":"941"}`.
- **KOFIC** (`kobis_key`): the box-office cards. KOFIC issues its own key at [kobis.or.kr/kobisopenapi](https://www.kobis.or.kr/kobisopenapi).

The Bluesky app password and the Claude token are not API keys; they live in the Keychain (below).

### Configuration

1. Copy the config template and fill in your own free API keys:
   ```
   cp seoul_index_config.example.json seoul_index_config.json
   ```
2. Store the Bluesky app password in the macOS Keychain:
   ```
   security add-generic-password -a "your-handle.bsky.social" -s "seoulindex-bluesky" -w
   ```
3. Create a long-lived Claude Code token for the selector:
   ```
   claude setup-token
   ```
   then store it under Keychain account `seoulbot`, service `claude-oauth-token`.

### Running

```
python3 seoul_index_post.py --dry-run      # harvest, select, compose and print, no post
python3 seoul_index_post.py                # post one index (English + Korean card thread)
python3 seoul_index_post.py --only=books   # build the card from ONE vein and post it
python3 seoul_index_post.py --daily=wxday  # a vein's own launchd slot
python3 seoul_index_post.py --show-cross   # print the cross-vein collisions in the live pool
```

The bot rejects any flag it does not know (a bare run posts live).

`--only=<cat>` builds the card from one vein, taking the same path the vein floor takes when it promotes a starved one, and skips that vein's cooldown. It refuses a vein with fewer than three facts (naming the veins that exist), and a vein that posted within the last `ONLY_MIN_HOURS` (6) hours unless `--force` is passed, so a hand run cannot duplicate a scheduled post. Check the feed before any manual post.

`--daily=<cat>` is `--only` made safe for an unattended launchd slot. If the vein's own feed has not advanced since its last post, it exits 0 and says so (`daily_already_posted`, comparing the card's data day with `daily_last_day` in the state file, stamped by every live post of a vein carrying a `map_day`). A withheld or thin vein, or one posted under 6 hours ago, exits 0 with the reason where `--only` exits 1. Each slot post refreshes the vein's cooldown, so the rotation runs do not also pick it.

### Schedule

The live account runs under `launchd`:

- **The rotation**: four posts a day, 8:30 a.m., 12:30 p.m., 4:30 p.m. and 8:30 p.m. KST.
- **Dedicated slots**: `busweekend` Fridays at 11:00 (its card needs a whole complete week, ready by Thursday at the feed's four-day lag) and `wxday` daily at 09:10 (yesterday's reading, so it never repeats a day). Other veins (`busroutes`, `busstops`, `nightbus`, `stations`) have had slots and now post through the rotation, the first three with the change override above.
- **Weather forecast**: `seoul_weather_post.py` at 05:25, with a 06:30 safety net; `already_posted()` checks the log before any network call, so the second run exits if the first posted.
- **Crowd sampler**: hourly from 05:00 to 23:00.
- **Sales scan**: monthly, on the 3rd (the sales data is quarterly).
- **Books harvest**: weekly, Sundays at 05:20. The counts move daily and the card carries no date of its own, so a month-old harvest would read as current.

## License

This code is released under the [MIT License](LICENSE). The Seoul Open Data and KOSIS figures it draws on are used under their respective open-data terms (Seoul is CC-BY, credited on every post).
