# UK Affluence Map

An interactive map of relative affluence across Great Britain at small-area
resolution, built entirely from open government data.

**Status:** all three phases complete — a GB affluence index across 43,064 small
areas with live adjustable weights, modelled individual income bands including
the share of adults on £100k+, and bank branches with adjustable catchment
analysis.

---

## What this is, and what it deliberately is not

The goal is a map you can zoom into, click, and interrogate: how affluent is
this neighbourhood, and how many people in it sit in a given income band.
Phase 2 adds modelled income bands; phase 3 adds bank branch locations with
adjustable catchment radii.

Three constraints shaped the design, and they are worth stating plainly because
they are the difference between a map that is useful and one that quietly
misleads:

**1. Postcode-level income does not exist in open data.** Nothing free gets you
individual income at postcode-unit level. HMRC publishes taxpayer counts by
income band, but only down to local authority and parliamentary constituency.
ONS publishes modelled *household* income at MSOA. The commercial products
(CACI Paycheck, Experian Mosaic) do offer postcode-sector income distributions,
and if precision matters more than cost, that is the honest answer.

What this project does instead: map the finest *statistical* geography that has
real data behind it, and use the free ONS postcode lookup so that searching or
clicking any postcode returns the statistics for the area containing it. You get
the postcode interaction without pretending to postcode-level precision.

**2. Deprivation indices are the wrong tool for affluence.** IMD, WIMD and SIMD
measure *deprivation* — their income domains count benefit claimants. They
saturate at the top: a £60k neighbourhood and a £250k neighbourhood both score
"not deprived" and are indistinguishable. They are excellent for their intended
purpose and useless for this one.

**3. The three national indices are not comparable with each other.** IMD 2025
ranks 33,755 English LSOAs, WIMD 2025 ranks 1,917 Welsh LSOAs, SIMD 2020v2 ranks
6,976 Scottish data zones. Each is a *within-nation* relative ranking built from
different indicators with different weights. Rank 500 in Wales and rank 500 in
England mean different things. Concatenating them into one GB map is a common
and serious error.

So this project **builds its own GB-wide affluence index** from inputs that are
genuinely comparable across the three nations — property values in pounds,
census percentages of people. The official indices are available as an optional
overlay and used as a sanity check, never as an input.

---

## Geography

| Nation | Unit | Count | Vintage |
|---|---|---|---|
| England | LSOA | 33,755 | 2021 Census |
| Wales | LSOA | 1,917 | 2021 Census |
| Scotland | Data Zone | 7,392 | 2022 Census |
| **Total** | | **43,064** | |

England and Wales use LSOA 2021 while Scotland uses Data Zone 2022. That
mismatch is deliberate: each is the geography its national statistics are
actually published against, so joins need no crosswalk and no interpolation.

---

## Setup

Requires Python 3.13 (installed via `winget install Python.Python.3.13`).

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## Running the pipeline

Run in order. Each step caches, so re-runs are cheap.

```bash
./.venv/Scripts/python.exe pipeline/build_boundaries.py
```

```bash
./.venv/Scripts/python.exe pipeline/export_web.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_lookups.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_metrics.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_income.py
```

```bash
./.venv/Scripts/python.exe pipeline/validate_income.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_branches.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_places.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_search.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_connections.py
```

```bash
./.venv/Scripts/python.exe pipeline/build_roads.py
```

Downloads are cached in `data/raw/` and never re-fetched, so re-runs are cheap
and work offline.

## Viewing the map

The map must be served over HTTP — opening `index.html` from disk fails because
the browser blocks `fetch` on `file://` URLs.

```bash
./.venv/Scripts/python.exe serve.py
```

Then open <http://localhost:8000>.

`serve.py` rather than `python -m http.server` for two reasons. It disables
caching and stamps local script and stylesheet URLs with their file mtime —
without that, an edited `app.js` or a rebuilt data file keeps serving the stale
copy and the map appears not to have changed, which is a genuinely confusing
failure when the files on disk are plainly correct. It also gzips, which takes
the boundary file from 21 MB to 3.4 MB over the wire.

---

## Layout

```
pipeline/
  config.py             paths and the registry of upstream sources
  download.py           cached fetching, including ArcGIS pagination
  build_boundaries.py   harmonised GB small-area backbone -> parquet
  export_web.py         simplified geometry -> web/data
  build_lookups.py      ONSPD postcode -> small-area lookup
  components.py         individual index components, one function each
  build_metrics.py      composite index -> parquet + web/data
  income_model.py       the income distribution and its fitting
  build_income.py       constituency model -> small areas -> web/data
  validate_income.py    out-of-sample check against ONS estimates
  build_branches.py     OSM bank branches -> web/data
  build_places.py       OSM city labels + vendored glyphs -> web/data
  build_search.py       postcode chunks + place index -> web/data
  build_connections.py  NaPTAN travel-connections rating -> web/data
  build_roads.py        OS Open Roads major-road overlay -> web/data
data/
  raw/                  downloaded sources, cached
  interim/              harmonised intermediates (parquet)
  out/                  published outputs
web/
  index.html            map shell
  app.js                map logic
  branches.js           branch layer, catchment analysis, place labels
  roads.js              major-road overlay
  search.js             postcode / place / branch search
  style.css             styling
  fonts/                generated: vendored Noto Sans glyph ranges
  data/                 generated: topology + metrics
```

### Why geometry and attributes are separate files

`gb_areas.json` holds geometry only; `gb_metrics.json` holds a columnar
attribute table keyed by the same area codes. The topology is expensive to parse
and almost never changes, while metrics are rebuilt constantly. Keeping them
apart means re-weighting the index repaints the map without re-parsing 43,064
polygons.

### Why coverage simplification, and not TopoJSON

The two national boundary sources arrive at wildly different levels of detail:
the ONS England & Wales boundaries are already generalised to about 12 vertices
per area, while the Scottish Data Zone file is full resolution at about 500.
Scotland alone was 90% of the raw backbone's 4.1 million vertices.

Simplifying each polygon independently would open slivers along shared borders,
because Douglas-Peucker on two neighbours' rings can treat the edge they share
differently. TopoJSON is the textbook fix, but the Python `topojson` package
cannot build a topology at this scale — on 43,064 polygons it grew past 24 GB of
resident memory without finishing.

GEOS coverage simplification (`shapely.coverage_simplify`, Visvalingam-Whyatt)
solves the same problem directly: it simplifies a polygonal mosaic while keeping
it a valid coverage, so neighbours stay edge-matched and no gaps appear. It runs
in 12 seconds. It is applied per source file, not per nation — England and Wales
share one edge-matched dataset and must be simplified together.

With gap-free geometry guaranteed, plain GeoJSON at 5 decimal places (~1 m) is
enough: **21 MB, 3.4 MB gzipped**, down from 100 MB.

One rendering artefact remains worth knowing about. Fill anti-aliasing leaves a
hairline seam along every shared edge, which reads as white speckle across dense
urban areas at national zoom. A 1px line in each area's own fill colour closes
it, faded out by zoom 9.

---

## Data sources

All Open Government Licence v3 unless noted.

| Source | Geography | Used for |
|---|---|---|
| ONS LSOA 2021 boundaries | LSOA, E&W | Geographic backbone |
| Scottish Government Data Zones 2022 | Data zone, Scotland | Geographic backbone, population |
| HM Land Registry Price Paid | Full postcode, E&W | Property values |
| Registers of Scotland small area statistics | Data zone, Scotland | Property values |
| VOA Council Tax stock of properties | LSOA, E&W | Council tax band mix |
| Census 2021 (ONS, via NOMIS) | LSOA, E&W | Occupation, qualifications, tenure |
| Scotland's Census 2022 | Data zone, Scotland | Occupation, qualifications, tenure |
| ONS small-area income estimates | MSOA, E&W | Calibration for phase 2 |
| HMRC Personal Incomes tables | LA / constituency | Income band distributions, phase 2 |
| OpenStreetMap (Overpass) | Point | Bank branches, phase 3 — **ODbL, not OGL** |

OpenStreetMap is the one non-OGL source. ODbL carries share-alike obligations
on derived databases, which matters if any output leaves the organisation.

---

## The affluence index

A weighted mean of standardised components, ranked to a 0-100 percentile.

| Component | Default weight | Source |
|---|---|---|
| Median property price | 40% | Land Registry 2023-25 (E&W), RoS 2023 (Scotland) |
| Higher managerial & professional (NS-SEC L1-L3) | 30% | Census 2021 / Scotland's Census 2022 |
| Degree-level qualifications | 20% | Census 2021 / Scotland's Census 2022 |
| Households with 2+ cars | 10% | Census 2021 / Scotland's Census 2022 |

Weights are defaults, not doctrine — the UI exposes them as sliders.

**Standardisation is GB-wide, not per nation.** Z-scoring each nation separately
would force every nation to the same mean and erase exactly the differences the
map exists to show. That is only legitimate because the components are measured
in pounds and in percentages of people using classifications that mean the same
thing on both sides of the border — and the national means bear it out:

| | England & Wales | Scotland |
|---|---|---|
| NS-SEC L1-L3 | 13.0% | 12.0% |
| Degree-level | 33.5% | 31.9% |
| 2+ cars | 35.9% | 32.4% |

Price is logged before standardising: it runs from £23k to £4m, and a raw
z-score would let a handful of central London areas dominate everything.

### Validation

Checked against areas with known character. Affluent: Elmbridge 91.7, St Albans
91.6, Kensington & Chelsea 90.6, Edinburgh Morningside 93.8. Less affluent:
Hull 12.9, Blackpool 14.2, Blaenau Gwent 15.4, Glasgow Drumchapel 3.2,
Paisley Ferguslie 9.6. The highest-scoring area in GB is Merton 002D
(Wimbledon); the highest in Scotland are Murrayfield and Ravelston, and The
Grange, both in Edinburgh.

### Known limitations

- **Median sale price conflates value with housing mix.** An area of small flats
  reads poorer than one of large houses at the same price per square metre.
  Fixing this means joining EPC floor areas and switching to price per square
  metre — the single biggest available improvement.
- **Scottish prices are a year behind.** The Scottish cube stops at 2023 while
  England and Wales pool 2023-25, so Scotland is very slightly understated.
- **Scottish sources need rebasing.** Census 2022 bulk tables are published on
  2022 Output Areas and the price cube on 2011 Data Zones; both are rebased onto
  2022 Data Zones using ONSPD postcode counts as weights.
- **722 areas have no price** (1.7%), mostly very low-transaction areas. They
  are still scored from the census components.

## Modelled income bands

HMRC knows individual incomes but publishes them only down to parliamentary
constituency — 632 in GB, against our 43,064 areas. Nothing free bridges that
gap, so it is modelled.

### The distribution

HMRC Table 3.15 gives four facts per constituency: the **median** and **mean**
of total income, and the number of taxpayers at **basic**, **higher** and
**additional** rates. Those rate counts are counts above the tax thresholds —
which makes them counts above known income levels, and that is what pins down
the top of the distribution.

A plain lognormal would badly understate the top end, because real income
distributions have a Pareto tail and £100k+ is exactly where that bites. So the
model is lognormal in the body and Pareto above the higher-rate threshold:

```
F(x) = Phi((ln x - mu) / sigma)          for x <= £50,270
S(x) = S(T) * (x / T) ^ (-alpha)         for x >  £50,270
```

All four facts are fitted together by least squares on log ratios.

> An earlier version solved three facts in closed form and held the mean back as
> a check. That is elegant but numerically degenerate exactly where it matters:
> when a constituency's median sits near the higher-rate threshold,
> `sigma = (ln T - mu) / z` has both numerator and denominator going to zero.
> Richmond Park — median £50,300 against a £50,270 threshold — collapsed to the
> sigma floor and produced an absurdly narrow body feeding a very fat tail,
> which put 8 of the GB top 10 inside one borough. Fitting all four at once is
> stable and strictly better informed.

Fit quality across the 632 constituencies: **median error 0.47% on the median**
and **2.95% on the mean**. National totals reproduce HMRC closely — modelled
6.57m higher-rate taxpayers against 6.50m actual, and 0.90m additional-rate
against 0.86m.

### From constituency to small area

An area's log median income is shifted by how far its affluence sits from its
constituency's average. The size of that shift is calibrated on the
*between*-constituency relationship, then applied *within* constituencies:

```
beta = 0.192 log-income per sd of affluence   (R^2 = 0.806, n = 632)
```

That is, **+1 sd of affluence is worth about +21% on median income**. The R² is
itself notable: the affluence index explains 81% of between-constituency
variation in income, having been built without using any income data.

**This is an ecological assumption and the model's central weakness.** It
presumes affluence buys the same income premium inside a constituency as it does
between them. There is no free data that can test it directly.

Spreading areas apart adds variance, so within-area sigma is reduced to
compensate — otherwise each constituency's modelled distribution would come out
wider than the one HMRC actually observed:

```
sigma_within^2 = sigma_total^2 - beta^2 * Var(affluence)
```

### Non-taxpayers

HMRC sees taxpayers. Adults under the personal allowance are largely invisible
to it, so adults aged 16+ who are not taxpayers are added into the lowest band.
The bands therefore sum to the **adult population**, not the taxpayer
population. The taxpayer rate is taken from the constituency and applied
uniformly across its areas; affluent areas almost certainly have a higher rate
than that, so the lowest band is somewhat overstated in rich areas. It does not
affect the upper bands.

### Validation

ONS publishes its own model-based income estimates at MSOA level, built from the
Family Resources Survey by completely different methods. Nothing in this
pipeline touches it, so it is a genuine out-of-sample check. ONS estimates
*household* income while we estimate *individual* income, so only the spatial
pattern is comparable — but that is what the map is used for.

| | Spearman |
|---|---|
| Modelled median income vs ONS household income | **0.91** |
| Modelled % of adults on £100k+ vs ONS | **0.85** |

Agreement is monotonic across every ONS decile — decile 1 maps to a modelled
£24.8k median and 0.5% on £100k+; decile 10 to £39.9k and 7.6%.

National result: **1.43m GB adults on £100k+ (2.69%)**, against HMRC's roughly
1.5m UK figure.

### What this is not

These are **modelled estimates, not measurements**. They are sound enough to
rank neighbourhoods and to size a catchment; they are not sound enough to quote
as fact about any individual area. If precision at postcode level matters more
than cost, CACI Paycheck is the honest answer.

## Bank branches and catchment

4,698 GB branches across 25 filterable brands, from OpenStreetMap. Select any
combination of brands, set a catchment radius (0.5–40 km, default 10), and the
map reports the population reached and its modelled income distribution.

Branches are drawn as **pins in their brand's colours**, which are bigger and
easier to hit than dots and read at a glance in a cluster. **Click a pin** to
measure that branch on its own; **click the sea** to clear the selection and
return to the whole network.

### Branch labels

Each pin is labelled by street — "Mosley Street, Manchester" rather than just
"Barclays" — which is what actually tells two branches of the same brand apart.

OSM carries `addr:street` for only 56% of branches. Falling back to the place
name recovered from the small area ("Manchester 054C" → "Manchester") is not
enough on its own: it left 832 branches (18%) sharing a label with another of
the same brand, including five Barclays all reading "Wirral".

So for every branch without an address, the pipeline asks OSM what road it
actually sits on. The branches are themselves OSM objects, so their ids seed an
Overpass set and `around.set:` finds named highways near each one individually,
with the nearest matched by point-to-segment distance in a local metric frame.

> A list of coordinates does **not** work here. Overpass reads a
> multi-coordinate `around` as a *polyline* and searches near that line rather
> than near each point, which returns nothing useful. Seeding the set from
> object ids is the correct idiom.

Where two branches of a brand still collide — genuinely both on a "High Street"
in the same authority — a postcode is appended, taken from OSM where present and
otherwise from the nearest live postcode in the ONSPD.

Two smaller fixes fell out of checking the result:

* **"Other" branches name their real brand.** That bucket holds 257 distinct
  banks, so two of them on one street looked like a duplicate when they were
  nothing of the kind — Punjab National Bank and Bank of India both sit on
  Belgrave Road in Leicester. Labels there read "Bank of India — Belgrave Road,
  Leicester".
* **Duplicate mappings are removed.** A bank often appears in OSM both as a node
  and as the building way around it. Two entries of the same brand within 40 m
  are treated as one; three were dropped. Left in, they inflate branch counts and
  double-weight that spot in a catchment.

Result: **4,698 branches, 95% labelled with a real street, and no two branches of
a brand sharing a label.** 105 fall back to a postcode tiebreak.

This is the only slow step in the pipeline. Overpass rate-limits hard, so
results are cached after every batch and a throttled run resumes rather than
starting over.

### A deliberate palette exception

Elsewhere this project follows the rule that categorical colours come from a
small validated set in fixed order, because more than a handful of hues cannot
be told apart and several would fail colour-blind separation. Brand colours
break that rule on purpose: 25 hues, and HSBC, Santander and Virgin Money are
all genuinely red.

The mitigation is that **colour never carries identity alone**. The brand filter
list doubles as a legend with the same swatches, hovering a pin names the branch
and its brand, and a realistic comparison is two or three brands at a time —
which these colours separate cleanly. Recognisability was judged worth more than
strict palette discipline here.

### How catchment is computed

An area counts as inside a catchment if its representative point falls within
the radius — in or out, never partly in. Areas average about 1,200 adults, so
above a kilometre or so the edge effects largely cancel, and it has the virtue
of counting exactly the areas whose statistics are being summed rather than
apportioning by overlap and hoping.

Overlapping catchments are **unioned, not summed**, so a person served by three
branches is counted once. This matters: summing per-branch catchments would
roughly double the apparent reach of a dense urban network.

### Composition vs coverage

The band percentages answer two different questions, and the denominator is the
whole difference. A toggle switches between them.

**Composition** — of the people this network reaches, how are they distributed?
The denominator is the catchment population, so the bands sum to 100%. Answers
"what does this network's customer base look like?"

**Coverage** — of all GB adults in each band, how many does this network reach?
The denominator is the national total for that band. Answers "what share of the
country's high earners can this network get to?"

Coverage is usually the more strategic view. HSBC's 369 branches at a 10 km
radius reach 42.0m adults — 79.1% of GB — but **82.4% of everyone on £100k+**,
and coverage climbs steadily with income:

| Band | Share of that band reached |
|---|---|
| £50k–75k | 78.1% |
| £75k–100k | 79.7% |
| £100k–125k | 80.8% |
| £125k–200k | 82.1% |
| £200k+ | **85.3%** |

Composition bars scale to the largest band, because one band always dominates
and absolute widths would be unreadable. Coverage bars scale to a true 100%,
because there the absolute level is the point.

Areas are indexed into a 5 km grid and distances use a local equirectangular
projection rather than great-circle maths — under a tenth of a percent error at
these distances, and it keeps the whole 4,701-branch union interactive.

### What it shows

With every brand selected at a 2 km radius: **29.3m adults reached** (55% of GB
adults), of whom **832,689 are on £100k+** — 2.8%, against 2.69% nationally.

Individual networks differ measurably. Barclays' 421 branches reach 13.1m adults
of whom **3.4%** are on £100k+ — a network that skews noticeably more affluent
than the branch estate as a whole.

### Caveats

- **OSM is contributed, not authoritative.** Good in towns, patchier in rural
  areas, and slow to reflect closures — and UK branches have been closing fast.
  Treat counts as indicative. An internal branch list would be strictly better
  and the importer is a small change.
- **ODbL, not OGL.** This is the one non-open-government source in the project.
  Share-alike obligations attach to derived databases, which matters if output
  containing this layer leaves the organisation. It is deliberately isolated in
  its own pipeline step and its own file so it can be dropped cleanly.
- **Radius, not drive time.** A circle is a poor model of a real catchment where
  rivers, motorways and rail lines distort access. Drive-time isochrones would
  need a routing engine (self-hosted OSRM or Valhalla).

## Roads

At national zoom the place labels are enough. Zoom in and the map goes abstract
fast — 43,064 anonymous polygons with nothing to anchor them unless you already
know the area.

Rivers help, by accident: the ONS boundaries are clipped around water, so the
Thames and the Clyde show through as gaps in the polygon coverage. There is no
water layer. Roads had to be added properly.

This is deliberately **not** a street map. Only classified roads are kept —
motorways, A roads and B roads — which gives every bypass, the radial routes out
of each city, and the named high streets that carry a classification, without
burying the data under a road atlas. They appear by class as you zoom: the
strategic network from zoom 8.5, B roads only from zoom 11, labels from 11.5.
A toolbar button turns them off for a clean choropleth.

**Source: OS Open Roads**, Ordnance Survey's open road network for GB, under the
Open Government Licence. It carries both `roadNumber` (A720) and `name1`
(Princes Street), so labels prefer whichever is more recognisable.

464,023 classified links dissolve to 35,907 roads — OS splits them at **every
junction**, and merging by name and number stops MapLibre trying to label the
same street forty times along its length. Simplification is per class, because
they appear at different zooms: 25 m for motorways, 35 m for A roads, 60 m for
B roads which are never drawn below zoom 11 where that is under two pixels.
The result is 34.9 MB raw, **4.6 MB gzipped**, and about a second to load.

Styling is furniture, not data — a thin ink line over a surface-coloured casing,
which stays legible over dark blue and pale red alike without competing with the
fill underneath.

### Labels switch from number to name

At regional zoom the label is the road number; from zoom 13.5 it becomes the
street name where there is one. That is road atlas behaviour, and it matches
what the reader is actually asking: "which road is this" when looking at a
region, "which street am I on" once inside a city. Edinburgh at zoom 12 shows
A720, A90, A8, A7; at zoom 14.6 the same roads read North Bridge, Leith Walk,
Lothian Road.

### What is missing, and why

**Princes Street is not on the map.** OS classifies it as *Unclassified /
Minor Road* — it was declassified when it became bus and tram only — so it falls
outside the motorway/A/B filter. It is not an isolated case: pedestrianised and
declassified high streets generally drop out.

Including them would mean taking OS's unclassified roads, and Edinburgh's grid
square alone holds 37,559 of those against 6,286 A roads. That is the road atlas
this layer exists to avoid. The filter is one line (`KEEP_CLASSES` in
`build_roads.py`) if the trade ever looks worth making.

The layer also carries no water. Rivers appear only because the ONS boundaries
are clipped around them, which works well for the Thames and the Clyde and not
at all for anything smaller.

## Place labels

151 city and town labels, so a choropleth of 43,064 anonymous polygons can be
navigated without a basemap underneath the colours.

Labels are **ranked, not assigned fixed zoom thresholds**. The rank goes to
MapLibre as a symbol sort key and collision detection drops the least important
ones wherever it is crowded, so the map thins itself out as you zoom instead of
switching layers on at arbitrary levels. Major cities sit in their own layer;
everything else has a zoom floor, because scaling minor labels towards zero
still leaves them holding collision space and reading as clutter.

Source is OpenStreetMap, so the same ODbL caveat as branches applies. ONS
publishes Major Towns and Cities under OGL, which would be a cleaner licence,
but it covers England and Wales only — no Glasgow, Edinburgh, Aberdeen or
Dundee — so it cannot label a GB map on its own.

### Fonts

MapLibre cannot draw a single character without a glyph source. `build_places.py`
vendors two Latin ranges of Noto Sans (Regular and Bold, ~420 KB) into
`web/fonts/` rather than pointing the style at a public font server: MapLibre's
own is explicitly a demo, the openmaptiles one no longer serves these, and a
runtime font dependency would be the only thing in the app that needs the
network at view time.

## Search

One box, three kinds of result: postcodes, towns and cities, and individual
branches. Arrow keys move, Enter selects, Escape clears.

**Postcode** flies to the exact coordinates, drops a pin and selects the small
area containing it — so the statistics shown are the ones that actually exist at
that resolution, which is the honest interaction given nothing here is modelled
at postcode-unit level.

**Town or city** pans to it. All 1,614 OSM cities and towns are searchable, not
just the 151 prominent enough to carry a label.

**Branch** focuses that branch: it ticks the brand, turns the catchment on and
reports that single branch's reach.

### Postcodes are fetched on demand

There are 1.75 million live GB postcodes — far too much to ship with a page for
a search box most sessions never use. They are split into one file per postcode
area (AB, AL, B, … 120 of them) and the file for "SW" is requested only when
somebody types a postcode starting with those letters. Median chunk is 394 KB,
the largest (B) is 1.2 MB, and chunks are cached for the session.

### Two things that had to be got right

**Keys are stored space-free.** The box normalises what the user types, so a
stored "1A 1AA" would never match a normalised "SW1A1AA" — every *full* postcode
would silently find nothing while partials worked, which is the worst kind of
bug to notice late.

**But a typed space is still information.** It marks the outward/inward
boundary. Strip it and "EH3 6" normalises to "EH36", which prefix-matches
district EH36 and buries the EH3 6xx the user asked for. When a space is
present the outward code must match exactly and only the inward part is treated
as a prefix; without one it falls back to plain prefix matching.

## Travel connections rating

Every branch carries a rating out of 10 for how reachable it is without a car.

Real drive-time isochrones need a routing engine and a road network, and they
answer a different question anyway — how far a car can get, which for a high
street bank is often the least interesting mode. This is a deliberately simpler
proxy built from four components:

| Component | Weight | What it measures |
|---|---|---|
| Rail, metro or tram proximity | 3 | distance to the nearest station |
| People within a walk | 3 | adults within 800 m |
| Mode diversity | 2 | distinct modes within 800 m |
| Bus stop density | 2 | bus stops within 500 m |

Mode diversity is scored to reward the **second** mode most — somewhere with a
bus and a train is far better connected than somewhere with two bus routes, and
the weighting has to capture that.

**Source: NaPTAN**, the Department for Transport's register of every public
transport access point in GB — 355,701 active nodes (344k bus, 6.2k rail, 3.9k
tram, 1.1k ferry). It is **Open Government Licence**, unlike the OpenStreetMap
layers, and its StopType field distinguishes modes directly rather than
requiring them to be inferred from tags.

### It reads sensibly

Camden High Street scores 10.0 — station 12 m away, four modes, 31 bus stops,
19,680 adults within a walk. Pierowall on Westray scores 0.1, with no transport
node within 6 km and ten adults within 800 m. By brand, **Metro Bank averages
8.5** (deliberately urban, high-footfall siting) against **Cumberland Building
Society at 3.3** (rural Cumbria). Median across all branches is 7.9, which is
what you would expect of an estate built around high streets.

### One thing that had to be fixed

The first implementation summed the adults of every area whose **centroid** fell
within 800 m. That is badly wrong at this scale: small areas can be two
kilometres across, so a branch in the middle of a town centre often has no
centroid within 800 m at all. It scored 104 branches at zero walkable
population, including a High Street site with eleven bus stops at the door.

Each branch now gets a buffer and every area it touches contributes its adults
in proportion to how much of it falls inside, which assumes population is spread
evenly within an area — the standard assumption, and far closer to the truth
than sampling a single point.

### What it is not

Not a drive time, and not trying to be one. It says nothing about parking, road
access or service frequency — NaPTAN records where you can board, not how often
anything runs. Adding timetable data (via BODS, also OGL) would let frequency
in, and is the obvious next step if this proves useful.

## Rankings

The sidebar lists the top and bottom ten areas on whichever metric is displayed,
and clicking any row zooms the map to it. Bounding boxes are indexed once at
load, so it can zoom to an area that is not currently rendered.

## Sharing it

The site is static — HTML, CSS, three JavaScript files and a folder of generated
JSON. No server-side anything, so it deploys to any static host.

```bash
python build_dist.py
```

That assembles `dist/`: 138 files, 117 MB on disk, of which a first visit
downloads about **10.4 MB gzipped**. The postcode chunks are the bulk of the
rest and load only when somebody searches a postcode.

It also stamps the local script and stylesheet URLs with a content hash, which
is the one thing a static host cannot do for itself. Without it a returning
visitor can sit on a cached `app.js` indefinitely and see none of your changes.

### Deploying

**Netlify Drop** is the shortest path — drag `dist/` onto
<https://app.netlify.com/drop> and it returns a public URL. No account needed to
start, no CLI, and it serves gzip and brotli automatically.

**GitHub Pages** is scripted:

```bash
python deploy_gh_pages.py https://github.com/YOU/uk-affluence-map.git
```

That turns `dist/` into its own throwaway repository with a single commit and
force-pushes it to `gh-pages`. The single-commit-force-push is deliberate:
committing 117 MB of generated data to `main` would add that much to the history
on every rebuild, for files reproducible from the pipeline in a few minutes. The
main repository is never touched — no worktrees, no branch switching, nothing to
clean up if it goes wrong.

Then enable Pages in the repository settings: *Deploy from a branch*, branch
`gh-pages`, folder `/ (root)`.

Largest file is `gb_roads.json` at 34.9 MB, comfortably inside GitHub's 100 MB
hard limit and below the 50 MB warning threshold. The deploy script checks this
before pushing.

> **Cloudflare Pages will not work** without changes: it caps individual files
> at 25 MB and `gb_roads.json` is 34.9 MB.

The geometry files are `.json` rather than `.geojson` on purpose. GitHub Pages
gzips on the fly by content type, and `application/geo+json` is not reliably on
that list — as `.geojson` the 34.9 MB road file risked being served
uncompressed.

Relative paths are used throughout, so serving from a subdirectory
(`username.github.io/uk-affluence-map/`) works without configuration.

### Before you share it

- **Licences.** See [ATTRIBUTION.md](ATTRIBUTION.md). Most of the data is Open
  Government Licence and needs only attribution, which the map displays. Bank
  branches and place labels are OpenStreetMap under **ODbL**, which adds
  share-alike: publishing the map distributes those two derived files, so they
  are offered under ODbL. Nothing else in the project is affected.
- **The intro panel.** Shown once per visitor, dismissable, reopenable from the
  About button. It exists because the income figures are modelled and a map that
  gets forwarded onward will outrun any caveat kept in a README.
- **Code licence.** There isn't one. A public repository with no LICENSE file
  means all rights reserved, which may or may not be what you want — worth a
  decision before publishing.

## Roadmap

- **Phase 1** — GB affluence index from comparable inputs, with adjustable
  weights *(done)*
- **Phase 2** — modelled individual income bands from HMRC constituency data
  and the affluence index *(done)*
- **Phase 3** — bank branches, brand filtering, adjustable catchment radii, and
  estimated population by income band within catchment *(done)*
- **v2** — savings and liquid assets
