# Seoul Index

The source code behind **Seoul Index (숫자로 보는 서울)**, [**@seoul-index.bsky.social**](https://bsky.app/profile/seoul-index.bsky.social), a Bluesky bot. Each post is a short set of real statistics, mostly from Seoul Open Data and otherwise from Statistics Korea or the OECD, rendered as a card image. A post goes out as a four-post thread: an English card, then a Korean one, each followed by a short reply carrying the clickable source and tags.

The account is written by A.I. and says so in its profile. This repository is published for transparency: The code here is exactly what composes and sends the posts.

## Design principle: accuracy over wit

**Python owns every number.** It harvests the data, formats each value and detects the sharp juxtapositions. A `claude -p` step only *curates* (which lines, in what order, and a neutral opener), lightly rewords English labels and *translates* the labels to Korean. Claude never emits a numeric value: the poster reuses Python's exact value string in both languages, and a digit-guard rejects any Claude-written label that contains a figure's digits. So a hallucinated number cannot reach a post.

### The labels are checked too

The digit-guard covers the numbers. It says nothing about the words beside
them, and until 23 August 2026 nothing did: a reworded English label and every
Korean label went to a live card unread.

An audit of 71 published cards found 7 real problems. **Five were the same
shape and all five were on a cross-vein card.** The reason is structural rather
than careless. A collision card is required to take a neutral opener, because a
vein-specific one would falsely frame the other vein's line; and every rule
that makes a bare label safe assumes the opener names the metric. Both cannot
hold, so on those cards the metric has nowhere to live, and one went out
reading "Seodaemun Prison History Hall: 25,343" with nothing saying those were
a month's visitors. Not one single-vein card was flagged. The other two were a
Korean line saying 체결된 (concluded) where the English said "filed", which is a
different stage of the same transaction.

`check_labels` now runs at the end of `compose()`, after the trim and after the
river and transport openers are rewritten, so it reads the wording the card
will actually draw. It reports only three things: an English label that no
longer says what the source label says, a Korean label that says something
different from the English, and a label that means nothing read with its
opener. Wit, brevity and a dropped unit conversion are explicitly not its
business.

A flagged label falls back to the pool's own label, the one wording on the card
guaranteed to say what its number is. That fallback is the one `clean_label`
already used for a label containing a figure's digits; this widens what can
send a label back to it. ⚠️ A flagged **Korean** label falls back to the
English source label, which is what the card already does when the selector
returns no Korean at all. A line of English on a Korean card reads oddly, and
is better than a Korean line making a claim the data does not carry.

Every verdict is logged to `label_checks.jsonl`, rejections included, and a
rejection is reported to `~/Scripts/observe.py` where that exists, so a check
firing three weeks running reads as one recurring condition. A check that
cannot run is not a failure: the card goes out unchecked, as every card did
before, and the log says the check was the thing that failed.

## How a post is built

1. **Harvest** a pool of candidate facts from the live and cached data sources (see below). Each fact carries an exact, pre-formatted value.
2. **Select** with `claude -p`: it picks 3 to 4 lines that form a coherent set, preferring to build around one pre-detected pair, and writes a neutral opener plus Korean labels.
3. **Compose**: Python stitches the chosen labels back onto its own exact values, adds the source line and tags, and enforces the character limit. Wording that every line repeats is trimmed here, so the index reads like one: the metric is named on the first line and each later line carries only what differs ("Estimated crowd in Jamsil", then "In Hongdae"), and anything the opener already says is dropped from the lines entirely. English trims the leading run and Korean the trailing one, since Korean puts the head last.
4. **Render and post**: each index is drawn as a card image (the numbers on the card), and the thread goes out as the English card, a reply with its clickable source and tags, the Korean card, then its source reply. Each card's full text is its image alt text. ⚠️ The `boxhist` card is the one exception (`credit_on_card` in `compose()`, added 28 August 2026): it has no dateline of its own, so the credit flies as red text under the title instead, and the thread is just the two card posts, EN then KO, with no source reply — a card that carried the credit twice was judged worse than losing the clickable link.

### Cross-vein collisions

The pairs pre-detected within a single source are the two ends of one distribution (the dearest and cheapest flat, the fullest and quietest crowd), a range rather than a coincidence. The sharpest Harper's-Index move is instead a *collision*: two figures from **unrelated** datasets that happen to land on nearly the same number. A whole quarter of one Seoul industry set beside a single apartment's deposit; a month of visitors to a palace beside the crowd on a café street right now.

A pass over the whole pool (`cross_vein_pairs`) finds these and offers the selector one to build around, as a sanctioned exception to the rule that each source keeps its own post. It only ever pairs like with like (two ₩ figures, or two head-counts) and never against a count of a different thing, so a figure a fact declares as money is never set against one declared as people. The values themselves are untouched: only the detector reads a fact's raw magnitude to decide the pairing, so Python still owns every posted number. When a post spans two sources, both are credited on the source line and both provisos ride the card footnote. Run `python3 seoul_index_post.py --show-cross` to print the collisions the live pool currently holds.

A crossed card carries **two frames at once**, so each side gets its own red subhead and its own lines beneath it: the scoped lines under their month, or under the descriptor that says what they count, and the live ones under "Right now". The live labels shed the framing the subhead now carries, so "Estimated crowd in Gwanghwamun right now" becomes "Gwanghwamun".

That arrangement exists because the alternative is not merely cramped, it is false. A scope printed as a masthead under the title is a claim about **every line below it**, and on a crossed card most of those lines belong to the other vein. The card posted on 22 August 2026 set Seoul Library membership against the live crowd and put "Members of Seoul Library" in the footnote next to "Crowds are KT-estimated", where it read as attribution rather than as the only thing on the card saying what "60s" and "Teens" counted. Lifting it to the masthead instead would have been worse: it would have claimed the 11,000 people in Gwanghwamun as library members. The veins whose labels are bare carry "own post, never mixed" for exactly this reason, but a collision **overrides** that rule and takes the opener generic, so the opener cannot carry the meaning either. The subhead is the only place left that covers the right lines and no others.

⚠️ `DESCRIPTOR_SCOPES` is the single source of those words: `compose()` reads it for the footnote and for the subhead alike, so a vein listed in one place and not the other is not possible. Four period-carrying veins (`infant`, `daynight`, `water`, `price`) were flying their scope as a masthead over live lines until 22 August 2026 for want of being marked groupable: the age band is the sharp one, since it is the only thing distinguishing four otherwise identical series of bare years, and "Under-ones" over a crowd line said something plainly untrue.

## Two kinds of card

Most posts set things against each other across the city. One post in five, on average (a coin flip, never two in a row, rather than a fixed cadence), instead drills into **one place read along a clock**: the crowd right now, what that place is usually like at this hour on this weekday, and the busiest and quietest hours ahead, cycling through the curated spots. These are interspersed with the ordinary index cards, not a replacement for them, and a place that does not answer with enough lines falls back to a normal post.

It used to be one in three, which turned out to be too many. A spotlight card never reaches the selector, so its share of the schedule is a share no other source can occupy, and at a third of the feed it was crowding out the city.

A place also falls back to a normal post when its readings are all the same number wearing different labels. The crowd figures arrive in coarse pre-rounded buckets, and at the quieter spots the present hour, the weekday average and the peak ahead often round to the same value, so a card can say one thing four times: 11,000 / 11,000 / 11,000 / 6,250. A card must carry at least three distinct values, spread at least a quarter of the largest. The older check compared the peak and trough *hours* and never looked at the values, which is how those cards got out.

The spotlight card needs no `claude -p` call: its lines are fixed and in order, and their labels carry clock times, which are numbers Python does not hand over to be reworded or translated. Its opener names the place in both languages from the curated list.

It is headed "hour by hour" rather than "today" on purpose. `citydata_ppltn` knows the present and the next 12 hours and nothing else, so the peak and trough are the busiest and quietest hours **ahead**: the morning that already happened is not in the data. The footnote says the later hours are forecasts. The "usual for a Monday at this hour" line is the one figure that escapes that caveat, because it comes from the bot's own logged observations rather than from the forecast, and it simply does not appear until three separate weeks have been recorded.

A topical emoji leads the opener, and per-line emoji are added only where an obvious one fits; a guard rejects any number or keycap emoji so figures stay Python's alone.

### Making sure every source gets used

Three rules keep the feed off the few sources the selector finds most attractive. All three are enforced in Python rather than asked of the selector, because asking is what failed: the prompt already requested bare labels and was ignored, so `dedupe_labels` trims them instead, and the same pattern repeats here.

**Rotation** keeps two consecutive posts off the same metric.

**Cooldowns** hold back a source the selector over-reaches for. After a world post, world facts leave the pool for three days: the vein holds the widest gaps in the pool, so it wins nearly every time it is offered. Quarterly sales are cooled for the same span for the opposite reason, being *frozen* rather than merely attractive, since the pre-detected pair in a quarterly aggregate is the same two categories for months at a time.

**A vein floor** does the reverse, and matters more. An audit of the first fifty logged cards found that nine of the sixteen harvested sources had never once been a card's primary source: 39% of every fact harvested had never reached a reader, while six sources took the whole feed. Nothing had errored, and nothing was logged; the numbers were simply never chosen. The cause was the prompt, which tells the selector to strongly prefer a pre-detected pair and calls a cross-vein collision the sharpest move available, and pairs cluster in the live and quarterly sources. The annual-vintage ones lost every time they were offered.

So when a source has not led a card for five days, the pool is narrowed **to** that source for one card and the selector has nothing else to choose. Never-published sources go first, then the longest-neglected. Two such cards do not normally run back to back, so a backlog airs over alternating posts rather than putting the feed on rails. A source that has never led a card at all is the exception, and that exception was measured rather than guessed: on 25 August 2026 the queue was twelve to fifteen sources deep and draining at one promotion every two posts, so five sources live since 22 July (weather, health, property, airport and culture: 47 of the pool's 258 facts) had still never once led a card and sat at the head of every run's log line waiting their turn. The alternation rule was holding back the debut it exists to deliver. It is safe to relax there and nowhere else because the never-published set only ever shrinks, so it turns itself off when it empties, with no flag to reset: a source added later re-arms it, which is exactly when a debut is wanted again. Relaxing it on "the queue is deep" instead would be permanently true, and the rule would be dead code. A source with too few facts to fill a card on its own can never be promoted, and says so in the log rather than being quietly skipped.

**A repeat guard** stops a card recurring. A new card may share at most two line ids with any of the last twelve cards; when one does, the offending lines are dropped from the pool and the selector is asked again, up to three times, after which it posts anyway on the grounds that a repetitive card beats a missing one. The rule counts shared lines rather than demanding an exact match, because the failure it was written for was subtler than a duplicate: one card went out line-for-line identical four times in three weeks, but its top three lines were shared by all six cards that source produced in the same period.

## Card images

Each index is rendered to a PNG by `seoul_index_card.py`: the card is laid out in HTML, screenshotted with headless Google Chrome, then cropped to content with Pillow. Colour emoji and Korean text come from the system fonts, and the look is monospace on cream to match the avatar. A caveat that qualifies the numbers rather than credits them ("Crowds are KT-estimated", or "Metro areas, 2023" on a world card) sits in a muted footnote on the card, next to the figures it applies to; the source credit usually stays in the reply below, where it can be a real clickable link — the one exception is the KOFIC `boxhist` card (see above), which has no dateline to give up and flies the credit itself in the masthead instead, unlinked. If rendering ever fails, the poster falls back to a plaintext thread, so a post always goes out. The pinned methodology thread is built the same way, as prose cards, by `seoul_index_methodology.py`.

## Data sources

- **[Seoul Open Data](https://data.seoul.go.kr)** (CC-BY): live crowd estimates (KT mobile-signal based, disclosed as estimates), air quality, subway and bus boardings, infrastructure counts, quarterly commercial-district sales, the public-bike system counted live citywide (bikes at a dock, docking points, stations, stations standing empty), and live road speeds on named arteries. Average-bill lines are sales divided by the number of transactions, so they are what one payment came to, not what one person spent: a restaurant bill covers a shared table.
- **[Seoul Open Data](https://data.seoul.go.kr), market prices**: what one everyday item costs at shops across the city on one day, from the ~700,000 price observations the city has collected since January 2025 and refreshes weekly. Both ends of the card are published prices at named shops, so the spread is quoted rather than calculated: on 14 August a napa cabbage ran ₩2,992 at a supermarket in Nowon-gu and ₩6,900 at a traditional market in Dongjak-gu. Shops are identified by district and kind, never by name, because the English card has no English for 뚝도시장, and the kind is the better half anyway, since which of market or supermarket is dearer changes from item to item. An item whose dearest is less than one and a half times its cheapest is skipped: a flat spread is not an index, and the items rotate, so a skip simply gives the card to the next one.
- **[Seoul Open Data](https://data.seoul.go.kr), day and night**: how many people are in each district by day and by night, from the city's own published district aggregate of the 생활인구 series. A card is one half or the other, never both, since a daytime figure beside a night-time one for different districts reads as a ranking of places and is nothing of the sort. These count everyone PRESENT (residents, people at work, people visiting), not who lives there, and they are KT-modelled estimates, which the card says.
- **[Seoul Open Data](https://data.seoul.go.kr), waterworks**: the raw water drawn at each of the five purification centres, yesterday. Intake only: the same feed carries transmission and supply figures, and setting one against another would compare different things while looking like a ranking of places.
- **[Seoul Open Data](https://data.seoul.go.kr), children**: how many children Seoul has at one age, across a decade. Under-ones fell from 75,536 in 2016 to 41,600 in 2025. ⚠️ This feed's own field labels are unreliable (a row named "count" holds a percentage and the row named "ratio" holds the count), so the harvester keys on the numeric row code and reads only rows verified to be whole numbers.
- **[Seoul Open Data](https://data.seoul.go.kr), library membership**: who holds a card at Seoul Library, by decade of life. One library, the city's flagship, not the 215 public libraries counted elsewhere on these cards.

  Each count carries a **"1 in N"** beside it: the members of that band set against Seoul's registered population of the same age, from KOSIS (`DT_1B04005N`, 주민등록인구 by five-year band, two bands summed to a decade). A bare 10,921 teens says nothing about whether that is many; 1 in 65 does. It rides in a trailing parenthetical so the card still orders its lines by member count, and the population month rides in the footnote descriptor rather than the period slot, because a period there would lift to the masthead and date the membership figures — which carry no date at all.

  ⚠️ **The numerator is not a subset of the denominator, and the card says so.** Read off 서울도서관's own 회원증 발급 page on 23 August 2026: 준회원 is open to any 대한민국 국민 or registered foreign resident of Korea, with no Seoul connection required whatever, and even 정회원 covers people who merely work or study in Seoul while living outside it. The API returns `AGE_RANGE`, `BRDT` and `MBR_CNT` only, with no member class, so the two cannot be separated. The card may therefore state a **ratio** and must never state a **share**: "1 in 65" is honest, "1.5% of Seoul's teens hold a card" is a claim the data cannot carry. The footnote reads "Members need not live in Seoul" for that reason, and a ratio shipping without it would be misreporting. A KOSIS outage costs the ratio and nothing else: the counts go out bare, and the credit and both footnote notes go with it.
- **[Seoul Open Data](https://data.seoul.go.kr), complaints**: how many faults residents reported to the city in a whole year. Complete years only: in the running year the current month's slot holds the year-to-date total rather than that month, which would post a figure several times too large.
- **[HRFCO](https://www.hrfco.go.kr)** (한강홍수통제소), river level: covered above.
- **[KOSIS / Statistics Korea](https://kosis.kr)**: national-contrast lines (Seoul's share of the country's population, and the total-fertility-rate gap), and the denominator behind the library vein's "1 in N". Annual figures for the contrast lines, monthly registered population for the library ratio; credited on their own source line either way.
- **[OECD](https://data-explorer.oecd.org)** (SDMX, no key): Seoul set against eight peer cities (Tokyo, Osaka, Paris, London, New York, Berlin, Madrid and Amsterdam) on green space per person, share of people within a 5-minute walk of a transit stop, summer-night urban heat island, and population density. One publisher measuring every city the same way is the only sort of source a comparison like this can honestly rest on; nine separate city portals would compare definitions rather than cities.

  Two things constrain it. A measure is used only when Seoul and at least two peers report in the **same year**, because mixed vintages are a comparison of survey dates dressed as a comparison of cities. And an OECD functional urban area is not the city: Seoul's is the whole capital region, roughly 24m people, against the 9.6m city the Seoul Open Data and KOSIS lines describe. So a world card carries "Metro areas" and the year in its footnote, under the figures it qualifies. The vein is also rationed (after a world post, world facts leave the pool for three days), because it holds the widest gaps in the pool and would otherwise crowd out the city itself.

- **[World Bank](https://data.worldbank.org)** (no key), with Seoul's own figure from KOSIS: Seoul set against whole **countries** on one measure at a time, which is the point of it: the city is about thirty times denser than the country it sits in, and its birth rate is lower. Seoul always leads the card and the other lines are bare country names, so the opener carries the measure; the year and the "Seoul against whole countries" scope ride the card footnote, since they qualify the figures rather than credit them. This is a different thing from the OECD lines above, which compare Seoul's metro area with other **cities**. An earlier attempt at a country-only vein (Korea against peer nations, no Seoul figure at all) was built and then cut: a card with no Seoul on it is not this account.
- **[MOLIT 실거래가](https://rt.molit.go.kr)** (via [data.go.kr](https://www.data.go.kr)): one month's apartment-market filings: the dearest and cheapest single sales, a record jeonse deposit, and counts of filings citywide, by district, and jeonse against monthly rent. Every line is a filed transaction or a count of them, never a median or an average, and cancelled filings are excluded. Filings are due within 30 days of a contract, so the bot uses the newest month that can no longer grow (two months back) and caches the harvest for the whole month.


### Is the Korean card in Korean?
`check_korean()` runs on every card, before it is drawn, and flags any Korean label or opener
carrying **no Hangul**. It exists because on 24 August 2026 a crowd card went out with three of
its four Korean labels still reading *Estimated crowd in Seoul Station right now*, on a live
account, unnoticed. The model checker below it was looking: it asks whether a label still says
what its figure is, and an English label does say that. **Language is a different question and
a regex owns it**, so this is deterministic, needs no network, and runs whether or not
`CHECK_LABELS` is on — which is exactly when the model checker is unreachable or out of quota.

⚠️ **It reports and repairs nothing, deliberately.** For the veins the selector translates, its
answer is the only Korean that exists and the pool's own label is the English being complained
about. The veins that own their Korean (crowd, spotlight, rush set `label_ko`) never reach the
selector for it, so the 24 August card could not recur today.

⚠️ **And it never blocks a post.** The measured rate is one card in ninety-nine; a card with
English labels is a bad card, a card that never posts is a dead bot. That risk posture is also
why there is **no exemption list**: a Latin-only Korean label is conceivable — a film title on a
boxoffice card — and has never once occurred. Across 99 Korean cards in the feed on 26 August
2026 the only Latin-only labels were those three. An exemption for a case that has never
happened is a guess that can hide a real one, and a false positive here costs one log line.

The range is Hangul syllables and **not** the CJK ideographs, so a label of pure Hanja is
flagged too: this account writes its Korean in Hangul.

### Bolding the variable
A card whose rows are **one metric read at several places** bolds the place and leaves the
shared wording plain: *Estimated crowd,* **Gangnam Station**. Same rule as the then-and-now
subheads a section up — bold what CHANGES between the rows — and it is decided from the
labels rather than from a list of veins: cut each line's place out of its label, and bold
only if every remainder is identical. That is literally the test “is the place the one thing
that differs”, and it is right per CARD rather than per vein, so four *Visitors to X* qualify
while the same vein mixing in *Busiest subway station, Gangnam* does not.

⚠️ **The remainder must be non-empty**, and that guard is what keeps bold meaningful. River
and water label their rows with bare names, so cutting the place leaves nothing, every
remainder is trivially equal, and the card would come out entirely in bold: the same as no
bold at all, only heavier.

⚠️ **All rows or none, judged per language.** A label that does not contain its own place —
the selector reworded it — takes the whole card back to plain, because three bolded rows and
one not reads as a claim about the fourth. English and Korean are judged on their own labels.

⚠️ **Pinning a live label does NOT exempt it from `_strip_live_frame`**, unlike the
`dedupe_labels` trim. That trim removes whatever the labels happen to share, which on a pinned
label can be load-bearing; this removes exactly *Estimated* and a trailing *right now*, both of
which a grouped card states elsewhere by construction — the live group's subhead reads *Right
now*, and an estimated line always puts the KT caveat in the footnote. Pinning the crowd label
made it exempt for an hour on 26 August 2026 and put the duplication straight back: *Estimated
crowd, Hongdae* under a *Right now* subhead over *Crowds are KT-estimated*. It is confined to
the LIVE lines for the mirror-image reason: on a dated line those same words are the card's only
copy.

⚠️ **The crowd label is pinned in BOTH languages, and that is what makes the rest possible.**
The selector used to reword it per row: the 24 August 2026 card carried *Estimated crowd,
Gangnam Station*, *Estimated crowd in Seoul Station right now* and *Estimated crowd at Nodeul
Island the same minute* — three sentences for one metric, the last of them wrapping onto a
second line. A place cannot be bolded while the wording around it moves. Note `pin` covers
English only, so `label_ko` is set as well; without it the Korean twin keeps both faults while
the English card is fixed.

- **[KMA](https://data.kma.go.kr)** (기상청, via data.go.kr): daily readings from station 108, Seoul’s reference station, observing since **1907** — 경성측후소 opened in 낙원동 that October and moved to the present site in 1933. (This README said 1904 until 26 August 2026, which is when Korea’s modern observation network began, not this station. Settled against the bot’s own source: station 108’s first daily row in the ASOS API is 1907-10-01, and every span before it returns `NO_DATA`. It now appears in the posted credit line, so the year is published rather than merely commented.) (The live air temperature on the river cards below comes from a different KMA service and a different instrument, so the two are credited separately.) Yesterday's high, low and rain; the last full month set against the same month fifty years earlier: hottest day, wettest day, and counts of days meeting a stated criterion (33°C or more, nights never below 25°C, days never above freezing); and, in summer, a season-to-date running tally of swelter days (33°C or more) from 1 June through yesterday, against the same span fifty years back. Extremes are published rows and the rest is counting: no monthly means or totals, which would be computations rather than published figures.

  A then-and-now card **groups by metric**: the criterion is drawn once as a red subhead and the two periods sit bolded beneath it (*Nights never below 25°C (77°F)* → *Summer 2026* 23, *Summer 1976* 0). Before 26 August 2026 every row carried the whole criterion **and** its span, which wrapped at card width and stranded the year — the one token a reader scans for — alone on the second line, while the value sort interleaved the two pairs so neither sat beside itself. Grouping is refused unless every line carries the split and every metric has at least two rows: a subhead over one row is a heading over nothing, and the flat layout is still correct, only longer. The season window and the station sit in the muted **footnote** under the rows: *1 June–25 August · Seoul’s reference station, observing since 1907*. ⚠️ Both are passed with an empty period slot, and that is load-bearing rather than incidental — a scope entry carrying a period is promoted to the masthead, which would draw the span in red under the title in the same weight as the metric subheads. They are also **not** repeated on the source reply, following the rule that already keeps the KT-estimate caveat off it: the reply sits one post below the image and must not say what a reader has just read.
- **[Seoul Open Data](https://data.seoul.go.kr) and [KMA](https://data.kma.go.kr) together**: hourly water temperature in the Han, at the Seonyu station, and in the Tancheon, Jungnangcheon and Anyangcheon, set against the air temperature over central Seoul at the same hour. The pairing is the point. Four river temperatures on their own sit within about a degree of each other and say nothing; what makes a card is the water and the sky disagreeing, which they do by several degrees for most of the year. So the vein stays silent whenever the warmest and coolest readings are less than 3°C apart, which is much of high summer, when river and air converge.

  Every line on a card comes from one reading hour, and that is deliberate. Seonyu is the only station on the Han's main stem and publishes about five hours behind the three tributaries, so taking each station's newest reading would set a one o'clock river beside a six o'clock sky and call the result current. The harvester instead picks the newest hour the stations agree on and asks KMA for the air at that same hour; the hour then heads the card. The air figure is 초단기실황, a station observation rather than a forecast, which is a different instrument from the station-108 daily readings above and is credited as such.

- **[HRFCO](https://www.hrfco.go.kr)** (한강홍수통제소): the Han's level at Jamsu Bridge, set against that gauge's own published flood-warning tiers, 관심, 주의, 경계 and 심각. Sorted by value, the current reading lands in its true place among the tiers, and that arrangement is the whole card: no line points out where the river sits, because the column already has.

  It appears only when the river reaches the first tier. Four of the five lines never move, so a routine version of this card would repeat itself forever and the repeat guard above would block it, correctly. A quiet river is simply silence. Seoul has sixteen gauges and they are **not** comparable with one another: each reads from its own datum, and those span nearly fifty metres of elevation, so two gauges showing the same number describe quite different things. One gauge against its own tiers is the only honest arrangement available. The tiers are flood-warning levels set by the agency and not the level at which the bridge's walkway goes under; the card never suggests otherwise, and never adds urgency of its own.

- **[Korea Airports Corporation](https://www.airport.co.kr)** (via data.go.kr): Gimpo's monthly transport row: passengers and flights, the same month twenty years earlier, and the domestic/international split, each side of which is a published row via the route filter, never a subtraction. A month publishes from the 5th business day of the next.
- **[HIRA](https://opendata.hira.or.kr)** (건강보험심사평가원, via data.go.kr): patients per condition at Seoul care institutions, from adjudicated health-insurance claims, for the newest complete published care year. Two provisos ride on the card footnote: the region is where the institution is, not where the patient lives, and the counts are insurance claims only. The conditions are curated for recognisability, and for honesty: hair loss was cut because insurance covers so little of it that the true figure would read as wrong.
- **[MCST](https://www.mcst.go.kr)** (문화체육관광부's culture-facility survey, served by 한국문화정보원 via data.go.kr): Seoul's museums and galleries: the counts, and each year's most-visited houses, which are published per-facility visitor totals. The survey lags a year (the 2024 edition carries 2023 figures), which the card footnote says.
- **[KCTI](https://know.tour.go.kr)** (한국문화관광연구원, via data.go.kr): monthly visitor counts at paid-admission Seoul attractions (the palaces, Lotte World, Seoul Sky), with the foreigner counts as their own frame. Publication runs months behind, so the harvester walks back to the newest month with rows and the card footnote dates it. The rows are curated to a whitelist of recognisable attractions: the raw feed carries closure artefacts (a memorial hall with eight visitors in a month), and a juxtaposition built on one would mislead.
- **[KOFIC](https://www.kobis.or.kr/kobisopenapi)** (영화진흥위원회, its own key, not data.go.kr): one day's cinema **admissions on Seoul screens**, film by film, from the ticketing network every Korean cinema reports into. `wideAreaCd=0105001` is 서울시 in KOBIS's own region table, and the cut is a real one rather than a relabelled national figure: on 22 August 2026 오디세이 took 132,555 admissions in Seoul against 557,345 nationwide, and the orders differ too, 인시디어스 ranking 3rd in Seoul and 4th nationally. ⚠️ **Drop the region parameter and the call still succeeds, returning national rows in the identical shape**: nothing errors, nothing looks wrong, and the card would say Seoul while printing the country. `test_seoul_index_veins.py` asserts the parameter is on the URL for exactly that reason. The day rides as the dateline and "Seoul screens" as the footnote, since a card of bare film titles otherwise gives a reader no way to tell which figures they are looking at. ⚠️ The box office rows carry **Korean titles only**, so each film costs one extra call to `searchMovieInfo` for the English title KOFIC itself publishes (오디세이 is "The Odyssey"); a film with none on file is dropped rather than romanised, the same rule as `seoul_index_names_en.json`. Both titles are pinned, since a film's Korean title is not a translation of its English one. ⚠️ **The card is the day's top four, complete and in order, or there is no card.** It briefly offered five and let the selector take four, which produced cards reading 1, 2, 3, 5: four titles in descending order read as the ranking whether or not one is missing, so the hole is invisible. `complete_boxoffice` adds back anything the selector leaves out on an own-vein card, the harvester refuses a day it cannot fill, and the one likely cause of that (a film with no English title at KOFIC) is printed to the run log rather than left to look like a vein nobody picks. Four films a day, five calls a run. **A second frame, `boxhist`**, is its own card and never shares a post with the first: how many Seoul screens the day's number-one film was on, against the same date five and ten years back. On 22 August that runs 382 screens against 224 and 161. The labels carry the title and the year, the lines are a sequence rather than a ranking (so `boxhist` is in `ORDERED_CATS`), and three years or none: two is a comparison, not a card. ⚠️ **Not twenty years.** The ticketing network was still being rolled out in the 2000s, covering about half of screens in 2005, 86% in 2006, 95% in 2007 and 98% by 2008, and KOBIS's own numbers show it: the top film sat on 27 Seoul screens in August 2004 and 89 in 2006. Setting that against today would compare how many cinemas reported while looking like it compares cinemas. `SCREENS_YEARS` keeps both offsets inside the ≥98% era; twenty years becomes honest in 2028 on its own. Data begins 1 January 2004 and 2003 returns nothing. ⚠️ **`boxhist` carries the credit itself, on the card.** Every other vein's credit rides the clickable source reply below the image; this card has no dateline to spend that slot on (each row is its own year), so `compose()`'s `credit_on_card` flag flies "Source: kobis.or.kr · KOFIC" as the masthead instead and the thread skips the source reply entirely, rather than say it twice. Added 28 August 2026 after a reader asked for it on the card at <https://bsky.app/profile/seoul-index.bsky.social/post/3mu5baibvie24>.
- **[Seoul Open Data](https://data.seoul.go.kr), library loans** (`SeoulLibraryBookRentNumInfo`): what Seoul Library lent over the last 60 days, counted **by subject**: literature, philosophy, 어학 and the other seven KDC classes. Titles are never named, so the labels are bare subject names. **One library, the city's flagship, not the 215 public libraries counted elsewhere on these cards**: the same building the membership lines come from. The opener is required to name the library for exactly that reason. Two detectors ride along, as on the sales vein: a dead heat (two subjects within 2% of each other) and the gap between the least- and most-borrowed. On 22 August 2026 literature ran to 3,625 against 어학's 448, while natural science and history came out 1,130 to 1,122.

  Each count carries a **"1 in N"** beside it, as the membership lines do: the subject's share of every checkout counted, 11,706 of them across the ten classes on 23 August 2026, so literature is 1 in 3 and 어학 1 in 27. A card shows **four** of the ten subjects, which is the reason this earns its place — four lines alone cannot say what the other six weigh, and "1 in 15, 1 in 18, 1 in 20, 1 in 22" turns four similar-looking counts from the middle of the list into four subjects that are all minor. It is all-or-nothing across the card: one total divides every line, so a per-line guard could leave the largest subject bare while the rest carried a ratio, which is one card in two forms for no reason a reader could see.

  ⚠️ **It is a share of the checkouts COUNTED, never of all checkouts**, and the footnote states the total so the reader can check the arithmetic. Both halves of the ratio come from the same truncated feed, which is what makes it honest — and is the difference from the membership ratio, which divides one publisher's numerator by another's. Three denominators were considered and rejected before this one: **loans against the library's holdings** (the true analogue of the population denominator, dead on the data — the numerator is cut at the 3,000 most-borrowed items while holdings would be complete, so the rate would be wrong in level and possibly in order); **loans per title** within the counted set (2.82 to 5.82 across the ten classes on 23 August 2026, a spread that is an artefact of the truncation threshold rather than a fact about Seoul's reading, and would read as a finding); and **loans per Seoul resident** (a subject's checkouts over the whole city population means nothing, and the library lends to non-residents anyway).

  ⚠️ **This vein carries no dateline, alone among the scoped ones.** It briefly showed the harvest date above the lines while the footnote said "last 60 days", and the two read as contradicting each other. Everywhere else the dateline slot means *the period these figures cover* (a month, a quarter, an hour), but here the period IS the rolling 60 days in the footnote and the harvest date is only when it was read, so that slot said something it does not mean. The post's own timestamp answers "when". The consequence is that the footnote is now the only thing on the card stating a period, so `books_facts()` refuses to speak at all if the window is missing or zero.

  ⚠️ **Ten subjects and four lines is the narrowest pool any vein offers, so the selection guidance is doing real work.** The first version told the selector "the spread is the point" and handed it one example opener; it duly produced the same card twice in three runs, always literature at the top and 어학 at the bottom, under an identical headline. The guidance now says outright that the extremes are not compulsory, that four middle subjects or the four smallest are cards in their own right, that at most one of the two pairs belongs on a card, and that the opener must not settle on one wording. Measured over five consecutive selections afterwards: five distinct line-sets, literature absent from three of them, and openers varying in both languages.

  ⚠️ **This counted the top ten books until 22 August 2026, and the card was dull.** The most-borrowed book (32), the tenth (20) and the ten combined (245) are three numbers off one short list, none of which tells a reader anything they had not assumed. The 3,000 records carry a class on every one, so the figure worth posting was being fetched and thrown away.

  ⚠️ **The classification is KDC, not DDC, and they disagree exactly where it would hurt.** Under DDC a 4 is language and a 7 is the arts; under KDC a 4 is natural science and a 7 is language, so reading it wrong would file 이기적 유전자 under language and 여행영어 under the arts, and the card would read perfectly. Verified 22 August against 서울도서관's own category filter (`favorLoan?category=N00`, whose tabs name 총류 … 역사, 지리 in KDC order): for each of the ten classes, the API's top five titles in that class appear on the library's page for that same class. The labels are the library's own words, and 기술과학 is glossed **Applied sciences** rather than Technology because its most-borrowed titles are medicine and health.

  ⚠️ **The feed is a CUT, and the footnote has to say so.** `list_total_count` is exactly 3,000, and the records per loan-count run 590 at two loans against 1,248 at three: a natural tail carries the MOST records at the lowest count, so that inversion is the list being truncated part-way through the books borrowed twice. Everything borrowed once is missing, and so is most of what was borrowed twice. The card therefore reads "Seoul Library's 3,000 most-borrowed items, last 60 days"; it said "Loans at Seoul Library" for one evening, which claimed every loan the library made. The truncation need not fall evenly across subjects, so it bends the comparison between the lines and not only their totals. `books_facts()` refuses to speak if the record count is missing, exactly as it does for the window.

  **The figures are CHECKOUTS, not titles.** `CNT` is 대출횟수: the same book borrowed twice counts twice. And a title held in several copies is counted per record, so 모순's two editions are two rows (30 loans and 9), which is how the library's own list reckons it too.

  ⚠️ **Every record must land in a class.** A row with no readable `CLASS_NO` is counted as `unclassified` and its loans appear in no subject, which understates every line by an invisible amount without changing their order: a card that reads as a quieter library rather than as a bug. The harvest aborts above 5%, and the count rides in the output either way. It was 0 of 3,000 on 22 August 2026.

  ⚠️ **The 60-day window is the whole reason this source is publishable, and it is not in the API.** The payload's eight fields carry no date of any kind, which is why the dataset was assessed and rejected on 21 August 2026. 서울도서관 publishes the period itself, on the page serving the identical table: [lib.seoul.go.kr/statistics/favorLoan](https://lib.seoul.go.kr/statistics/favorLoan) heads it "TOP 100 목록 (최근 60일 자료집계)". Verified 22 August by matching the API's top 12 titles against that page (12 of 12 present, counts identical or within 2), and the harvester now re-reads that heading on every run, so a change of window follows automatically and a page that cannot be read, or that no longer shows our list, aborts the harvest rather than dating the counts by guesswork.

  ⛔ **This replaced [도서관 정보나루](https://data4library.kr) (data4library) on 22 August 2026, and what the two cover is not the same thing:** data4library is all 215 public libraries by calendar month, this is one library over a rolling 60 days. The data4library key was issued 19 July 2026 and returned `vitalizationErr` ("the API is not in an activated state") on every call for the 34 days that followed, so that vein never produced a single card. A bogus key of the same shape gets `authErr` and that one does not, so the key IS recognised: what was never switched on is the API, which is an account matter (libdata@korea.kr, 02-595-6131), not a code one. If it is ever activated it is worth having back, since it is citywide and its months are real calendar months, but nothing waits on it now.

  ⛔ **Do not diff two harvests to manufacture a calendar month.** The window rolls, so old loans fall out of it and a delta is net change rather than checkouts, and can go negative. Counts are also per RECORD, not per title: 모순 appears twice with different ISBNs at 30 and 9, and the library's own TOP 100 lists them separately rather than summing. That matters less now the vein sums whole classes, but it is why nothing is de-duplicated. The top titles are still computed every run, not to post, but because they are the proof that the API is serving the library's own list, which is what the 60-day window rests on.
- **[OpenStreetMap](https://www.openstreetmap.org/copyright)** (ODbL), via Overpass: English names for subway stations and districts. The Seoul feeds return Korean names only, and the English card should be English throughout. Romanising them mechanically would not do it: the official name of 홍대입구 is "Hongik Univ." and of 시청 is "City Hall". OSM carries `name:en` for the whole capital-area network, including the Korail and AREX lines the Seoul Metro datasets leave out. The table is harvested once and committed, so no post depends on Overpass being up.

**Labels lead with what the number means.** A place name is never the whole label: "Most paid for an apartment (Yongsan-gu)", "Dearest, a traditional market (Gangdong-gu)", "Fullest by night (Songpa-gu)". The English card is published first, and the reader it reaches is the one least able to place Dongjak-gu from its name, so the line has to be legible without the geography, and the district stays as texture for readers who have it.

**A Korean proper noun on the English card carries its category, once.** Four veins are a deliberate exception to the rule above (river, traffic, water and world), because their openers own the metric and their labels are bare names, which is what keeps five parallel lines scannable. But a bare name is exactly the thing the rule above exists to protect against: "Amsa" and "The Anyangcheon" are unparseable to the reader least able to place them, and that reader gets the English card first. So each of those cards names the KIND of thing somewhere on its face, once, and never on every line: a category word repeated down three of five rows is the sort of thing `dedupe_labels` exists to strip.

- **water**: on the dateline, "Purification centres, 3 August". Without it, Amsa and Ttukdo read as districts, or as rivers.
- **river**: in the footnote, "The Anyangcheon, Tancheon and Jungnangcheon are tributaries of the Han". The dateline is not available here, because the reading hour is already flying there. Two rules attach to it. It is built from the lines actually on the card, so a station under maintenance is never named in a footnote above a card it has dropped out of. And it says **tributaries**, not streams or rivers: Seoul's own English calls them Streams while Wikipedia has the Anyangcheon as a river, so "tributary" is at once the safe word and the useful one, since what makes the card worth reading is that these feed the Han.
- **traffic**: carried by the metric, and nothing is added. km/h under an opener the prompt already requires to say "how fast Seoul is driving" leaves nothing to explain, and appending "road" to Gangbyeonbuk-ro would only say -ro twice.
- **world**, **nation**, **books**: nothing needed. City names, country names and Dewey classes, already English.
- **crowd**, **spotlight**, **price**, **transport**: the category rides the label, which the main rule already requires. "Estimated crowd in Hongdae", "A traditional market in Dongjak-gu", "Busiest station, Gangnam".

⚠️ When a new bare-name vein is added, settle which slot carries the category BEFORE it posts: the dateline if it is free, the footnote if the dateline is spoken for. The river vein went out on 23 August 2026 reading "The Anyangcheon: 28.5°C" with nothing anywhere on the card to say what an Anyangcheon is.

**Temperatures and speeds carry an imperial conversion on the English card only**: "26.5°C (80°F)", "26 km/h (16 mph)", since Korea is metric and the Korean card would only be cluttered by it.

Every post hyperlinks its source.

## Files

| File | Purpose |
| --- | --- |
| `seoul_index_post.py` | Harvest, select, compose, render and post one index (English + Korean card thread). |
| `seoul_weather_post.py` | Standalone daily companion to `seoul_index_post.py`: posts today's Seoul weather forecast (KMA's 단기예보), once a day, on its own schedule outside the main index rotation. No `claude -p` selector step — a forecast card is five fixed numbers, built directly by Python in both languages. Shares the account's config and Bluesky credential with `seoul_index_post.py`. |
| `seoul_index_card.py` | Render an index or prose card to a PNG (headless Chrome, cropped with Pillow); the poster falls back to plaintext if it fails. |
| `seoul_index_methodology.py` | Post the pinned methodology / "about" thread as prose cards. A card is an image, so changing a word means re-posting all seven records: `--replace` posts the new thread, pins it, then deletes the one that was pinned when the run started. It deletes only after the replacement is up, and only if what was pinned has the shape this script posts (six captionless image posts, then the credits reply), since whatever is pinned is not guaranteed to be its own thread. |
| `seoul_index_sales.py` | Monthly full scan of the commercial-district sales dataset into `sales_agg.json` (the poster reads this cheaply). |
| `seoul_index_books_harvest.py` | Weekly harvest of Seoul Library's 60-day loans by subject into `books_agg.json`, like the sales scan. Re-reads the library's own published window every run and aborts if it cannot (see Data sources). |
| `seoul_index_crowd_log.py` | Crowd sampler, hourly from 05:00 to 23:00; appends observed readings to `crowd_history.jsonl` so the bot can say what a place is *usually* like. |
| `net_guard.py` | Waits for a route out before harvesting, so a post is delayed rather than lost when the machine wakes without a network. |
| `test_seoul_index_selection.py` | Tests for the vein floor, the repeat guard and the spotlight flatness rule. No network, no model call, nothing posted. |
| `test_seoul_index_methodology.py` | Cross-file tests that the pinned thread still credits every publisher the daily posts credit, clickably, and that `--replace` refuses to delete a thread that is not its own. No network, nothing posted. |
| `seoul_index_names_harvest.py` | Regenerate `seoul_index_names_en.json` from OpenStreetMap. Run occasionally: stations open a few times a year. |
| `seoul_index_names_en.json` | Korean → English names for stations and districts, so the English card carries no Hangul. |
| `seoul_index_config.example.json` | Template for the gitignored `seoul_index_config.json`. |
| `seoul_index_avatar.svg` | The account avatar. |

## Setup

Requirements: Python 3, the [`atproto`](https://pypi.org/project/atproto/) and [`Pillow`](https://pypi.org/project/pillow/) packages (`pip install atproto pillow`), `curl`, Google Chrome (for headless card rendering), and the [Claude Code CLI](https://claude.com/claude-code) for the `claude -p` selector.

### API keys

The bot uses free keys from four South Korean open-data portals, all set in `seoul_index_config.json`:

- Seoul Open Data (`api_key`): Required. The source for most veins (crowds, air, transport, infrastructure, sales). Register a free account at [data.seoul.go.kr](https://data.seoul.go.kr/) and request a general authentication key (일반인증키). One key works across every Seoul Open Data service the bot calls.
- KOSIS / Statistics Korea (`kosis_key`): Needed for the national-contrast lines and for the "1 in N" beside each library-membership count. Register at [kosis.kr](https://kosis.kr/) and request an OpenAPI key at [kosis.kr/openapi](https://kosis.kr/openapi). The key is a base64 string that ends in `=`, so keep the trailing character. Without this key the bot still runs: the national lines simply don't appear, and the library counts go out bare, with no ratio and no KOSIS credit.
- 공공데이터포털 (`data_go_kr_key`): Needed for the apartment-market and weather lines. Register at [data.go.kr](https://www.data.go.kr/); the account gets ONE key, but each API needs its own 활용신청 (instant, 자동승인) before the key works against it: apply for 아파트 매매 실거래가 (15126469), 아파트 전월세 실거래가 (15126474), 지상(종관, ASOS) 일자료 (15059093), 전국공항 수송실적통계 (15158834), 질병정보서비스 (15119055), 전국문화기반시설총람 (15125097) and 관광자원통계서비스 (15000366: its openapi.tour.go.kr gateway can take overnight to register a new key) and 단기예보 조회서비스 (15084084, for the live air temperature on the river cards). Without this key the bot still runs; those lines simply don't appear.
- 도서관 정보나루 (`data4library_key`): **Unused since 22 August 2026.** The books vein now reads Seoul's own portal on `api_key`. The entry is kept because the key is real and merely inactive; nothing reads it, and nothing breaks if it is removed.
- 한강홍수통제소 (`hrfco_api_key`): Needed only for the river-level lines. Register at [hrfco.go.kr](https://www.hrfco.go.kr/) and request a key; HRFCO emails it and you must click the activation link in that mail before it works, or every call returns `{"code":"941"}`. Without this key the bot still runs; the level vein simply never wakes.

The Bluesky app password and the Claude token are not API keys; they live in the Keychain, not the config (steps below).

### Configuration

1. Copy the config template and fill in your own free API keys:
   ```
   cp seoul_index_config.example.json seoul_index_config.json
   ```
2. Store the Bluesky app password in the macOS Keychain (it is not kept in the config):
   ```
   security add-generic-password -a "your-handle.bsky.social" -s "seoulindex-bluesky" -w
   ```
3. Create a long-lived Claude Code token for the selector:
   ```
   claude setup-token
   ```
   then store it under Keychain account `seoulbot`, service `claude-oauth-token`.

Run it:

```
python3 seoul_index_post.py --dry-run   # harvest, select, compose and print, no post
python3 seoul_index_post.py             # post one index (English + Korean card thread)
python3 seoul_index_post.py --only=books  # build the card from ONE vein and post it
```

The live account posts four times a day (8:30 a.m., 12:30 p.m., 4:30 p.m. and 8:30 p.m. KST) via `launchd`, the fourth slot added on 25 August 2026 to drain the vein-floor queue faster, with the crowd sampler running hourly from 05:00 to 23:00, the sales scan monthly (the sales data is quarterly, so a weekly scan was recomputing a figure that moves four times a year) and the books harvest weekly on Sundays at 05:20. Books is weekly rather than monthly because its counts move daily (they rose by one overnight between 21 and 22 August 2026), and the card carries no date of its own, so a month-old harvest would read as current. `seoul_weather_post.py` runs separately, once a day at 05:25.

`--only=<cat>` builds the card from one vein and takes the same path the vein floor takes when it promotes a starved one. It exists because there was otherwise no way to show a particular vein on demand: the floor picks whichever vein has waited longest, so asking to see a new one meant either waiting its turn or posting the wrong card to find out. It refuses, naming the veins that exist, when the one asked for has fewer than three facts.

## Licence

This code is released under the [MIT Licence](LICENSE). The Seoul Open Data and KOSIS figures it draws on are used under their respective open-data terms (Seoul is CC-BY, credited on every post).
