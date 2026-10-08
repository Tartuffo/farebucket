# farebucket

Finds the cheap seats a booking engine hides when you ask for more than one.

## The problem

Search a flight for one passenger and you might see $180. Search the same
flight for two and the per-seat price jumps to $208.50. Nothing changed except
your party size.

That's because airlines sell seats in **fare buckets**, each holding a limited
number of seats. When you book several passengers on one reservation, the
airline prices everyone in the cheapest bucket deep enough to hold the whole
party. One orphan seat at $180 is invisible to a two-person search.

Real economy fares from `data/`, JFK↔SFO over Thanksgiving 2026. Every price
below is **per seat**; each column changes only the number of passengers in
the search:

| Flight  | Date   | Search for 1 | Search for 2 | Search for 3 | Cheap bucket depth |
|---------|--------|-------------:|-------------:|-------------:|--------------------|
| B6 1515 | Nov 24 |      $180.00 |      $179.50 |      $179.67 | at least 3         |
| AA 177  | Nov 24 |      $180.00 |      $208.50 |      $208.67 | exactly 1          |
| AS 1542 | Nov 22 |      $340.00 |      $339.50 |      $388.67 | exactly 2          |
| DL 669  | Nov 29 |      $829.00 |      $828.50 |    $1,043.67 | exactly 2          |

SerpApi reports a whole-dollar party total, so dividing it back into seats can
produce small 33- or 50-cent movements within what is effectively one bucket.
The large jumps are the signal.

Look at the first two rows. Search either flight for one passenger and you see
**the same $180**. They are not the same. B6 1515 has at least three seats at
roughly that price; AA 177 has exactly one, and a second passenger reprices
*both* to $208.50 each.

The last row is the expensive case. Two seats average $828.50 each, but asking
for three moves the whole party to $1,043.67 each. Under the snapshot model,
separate bookings have an upper bound of $2,701.17. `farebucket` rounds that
bound up to $2,702 before comparing it with the $3,131 group total, so it
reports a conservative whole-dollar saving of **$429**.

None of this is visible from a normal search, because a search for three
people never shows you the price for one.

`farebucket` finds these automatically by querying every party size from 1 to
N and comparing. In the bundled three-passenger sample, 18 of 240 flights have
a positive whole-dollar split saving under the snapshot model.

## How it works

For a party of N, let `P(k)` be the observed total when searching for `k`
passengers, and let `f(k) = P(k) / k` be its exact per-seat value. The economic
fare ladder should rise with bucket depth, although whole-dollar API totals can
introduce the small sub-dollar reversals visible in the table.

```
booking together:       P(N)
separate snapshot bound: f(1) + f(2) + ... + f(N)
```

The bound follows because, in one unchanged inventory snapshot, a bucket deep
enough for `k` seats still has at least one seat after `k-1` are removed. It is
conditional on every search identifying the same itinerary and cabin, and on
inventory not changing between searches or bookings. The API cannot provide an
atomic snapshot, so treat the result as a decision aid rather than a checkout
guarantee. The code preserves exact fractions, rounds the modeled split cost
up, and rounds the resulting saving down.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No third-party
Python packages — everything runs on the standard library.

### Getting an API key

Fare data comes from [SerpApi's Google Flights
API](https://serpapi.com/google-flights-api), which returns structured
itineraries including flight number, aircraft type, cabin, and price. Sign up
at **<https://serpapi.com/users/sign_up>**.

As of August 2026 the free plan includes:

| | Free plan |
|---|---|
| Searches | **250 per month** |
| Throughput | 50 per hour |
| Cost | $0, no time limit |
| Paid tiers | from $25/mo for 1,000 searches |

Unused searches don't roll over; the allowance resets monthly. Check current
terms at <https://serpapi.com/pricing> — this tier was raised from 100 to 250
in 2026, so the numbers above may have moved again.

The hourly throughput cap is the one that bites in practice. A sweep costs
`dates x cabins x party sizes` searches, so a 10-date, 3-party pull is 30
requests per direction — fine. A 20-date sweep at 3 party sizes is 60 and will
hit the hourly ceiling partway through. `fetch_flights.py` paces itself at one
request per second and retries transient failures, but it won't wait out an
hourly limit; split large pulls into separate runs.

You can check your remaining balance any time:

```bash
curl -s "https://serpapi.com/account?api_key=$(cat SERPAPI_KEY.txt)" \
  | uv run python -m json.tool | grep -E 'usage|left'
```

### Storing the key

Put it in `SERPAPI_KEY.txt` beside the scripts — that filename is gitignored.
Alternatively set `SERPAPI_KEY` in the environment, or point `--key-file`
anywhere you like. The key is never written to the output CSVs.

## Usage

### 1. Fetch fares

```bash
uv run python fetch_flights.py JFK SFO \
    --start 2026-11-20 --end 2026-11-25 \
    --cabin economy --max-stops 0 --adults 3 --sweep
```

Writes `data/jfk_sfo_2026-11-20-2026-11-25_3pax.csv`.

`--sweep` is the important flag: it queries party sizes 1 through `--adults`,
which is what makes bucket detection possible. Without it you get one price
per flight and no way to know how deep the bucket goes. Sweeps bypass SerpApi's
cache by default to reduce capture-time skew. This costs a search for every
request; pass `--allow-cache` only when that tradeoff is acceptable.

The fetcher writes a `.csv.meta.json` sidecar recording the route, currency,
query matrix, freshness settings, timestamps, and whether every search
succeeded. A failed search prevents output from being replaced unless you
explicitly pass `--allow-partial`.

Cost is `dates x cabins x party sizes` searches. The example above is 18.

| Flag | Meaning |
|------|---------|
| `--cabin` | `economy`, `premium-economy`, `business`, `first` (multiple allowed) |
| `--max-stops` | `0` for nonstop only |
| `--max-price` | per-person cap; deferred to analysis when sweeping |
| `--adults` | party size |
| `--sweep` | query every party size from 1 to `--adults` |
| `--allow-cache` | permit cached results during a sweep |
| `--deep-search` | request slower results matching the browser |
| `--include-basic` | keep Basic Economy fares (excluded by default) |
| `--allow-partial` | write and mark an incomplete search matrix |
| `--key-file` | alternate path to the API key |
| `--output` | override the auto-generated path; `-` for stdout |

Multiple cabins and connecting itineraries are retained independently using a
fingerprint of every flight segment, airport, timestamp, and cabin.

Economy searches leave out Basic Economy by default, so the price shown is the
cheapest fare with free seat selection and a carry-on rather than the
stripped-down fare airlines use to rank lower. Pass `--include-basic` to get
the bare-bones price instead; fetch once each way to compare the two. SerpApi
only applies the filter to US domestic economy searches, and it does not cover
checked bags. The sidecar records the setting as `exclude_basic`. The bundled
`data/` files predate this and include Basic Economy fares.

### 2. Inspect per-seat fares

```bash
uv run python summarize.py data/sfo_jfk_2026-11-27-2026-12-02_3pax.csv \
    --date 2026-12-01 --parties 1 2 3 --max-price 500
```

`PP@1`, `PP@2`, and `PP@3` are the observed per-seat prices when searching for
one, two, or three passengers. An asterisk means the conservative split bound
is lower than booking that party together. Omit `--date` to print one correctly
labeled table for every date in the CSV. `--earliest-depart` and
`--latest-depart` (both `HH:MM`, inclusive) narrow the table to a departure
window; `--latest-arrive` caps the arrival time.

Add `--email` for a narrow plain-text version meant for pasting into a
message. It keeps the times, flight, and per-seat prices, states the filters
and fare type above the table, and leaves out the aircraft, notes, and
excluded-flight list:

```
EWR -> SFO | Mon Jan 4, 2027
Nonstop, Economy, departing 9:00 AM to 5:00 PM
Price per person, Basic Economy fares excluded

Depart    Arrive    Flight          Price
--------  --------  --------------  -----
9:00 AM   12:30 PM  United UA 1777   $435
10:00 AM  1:36 PM   United UA 548    $503
```

The columns line up in a fixed-width font; in a proportional font each row
still reads left to right as time, flight, price.

### 3. Rank trips

```bash
uv run python analyze_trips.py \
    data/jfk_sfo_2026-11-20-2026-11-25_3pax.csv \
    data/sfo_jfk_2026-11-27-2026-12-02_3pax.csv \
    --full-days 6 --passengers 3 --max-price 500 \
    --must-include 2026-11-26 --same-day-outbound
```

Against the bundled data that returns:

```
6 FULL DAYS AT DESTINATION
--------------------------------------------------------------------------
  1. $431.67/person  |  $1,295 for 3
     Out   Tue Nov 24  JetBlue B6 1515  06:35 / 10:09  Airbus A320  Economy  $179.67
     Back  Tue Dec 1  Alaska AS 30  06:18 / 15:03  Boeing 737  Economy  $252.00

  2. $448.33/person  |  $1,345 for 3
     Out   Wed Nov 25  Alaska AS 41  20:29 / 23:59  Boeing 737  Economy  $268.67
     Back  Wed Dec 2  JetBlue B6 16  13:20 / 22:00  Airbus A320  Economy  $179.67
```

A **full day** is one spent entirely at the destination, so both travel days
are excluded: `return_date = departure_date + full_days + 1`. Pass several
values to `--full-days` to compare trip lengths side by side.

`--must-include` requires the stay to contain a date — useful for anchoring a
trip around a holiday.

| Flag | Meaning |
|------|---------|
| `--full-days` | day counts to evaluate |
| `--passengers` | party size |
| `--max-price` | maximum round-trip price per person |
| `--must-include` | date the stay must contain |
| `--depart-from` | earliest acceptable outbound date |
| `--same-day-outbound` | outbound must land the same calendar day |
| `--same-day-return` | reject red-eye returns |
| `--latest-outbound-arrival` | arrival cutoff, `HH:MM` |
| `--earliest-return-departure` | departure cutoff, `HH:MM` |
| `--options` | how many to show per scenario |
| `--allow-partial-data` | analyze a CSV explicitly marked incomplete |
| `--dump` | also print every qualifying flight by day |

## Caveats

- **Separate reservations mean separate records.** If a flight cancels, the
  airline treats your party as unrelated passengers with independent
  rebooking. Weigh that against the saving — the modeled whole-dollar floor in
  the bundled three-passenger data ranges from $1 to $429.
- **The split bound assumes stable inventory.** Fresh searches reduce cache
  skew, but searches and purchases are still sequential. Availability or price
  can move between them.
- **Each direction is priced as a one-way and summed.** Standard for domestic
  US itineraries, but worth spot-checking against a round-trip search on
  routes where that doesn't hold.
- **Fares are snapshots.** Holiday pricing moves daily.

The `data/` directory holds real JFK/SFO fares so you can try `summarize.py`
and `analyze_trips.py` without an API key.

## Development checks

The regression suite uses only `unittest`:

```bash
uv run python -m unittest discover -s tests -v
```

Strict static checking is configured in `pyrightconfig.json` and can be run
without adding a runtime dependency:

```bash
uv run --with basedpyright basedpyright
uv run --with ruff ruff check .
```

## License

MIT
