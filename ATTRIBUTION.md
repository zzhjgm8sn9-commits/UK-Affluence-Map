# Attribution and data licences

Everything in this project is built from open data, but not all of it carries
the same obligations. This file records what has to appear where, and what the
ODbL layers commit you to if the map is published.

The map itself shows a condensed version of this in its attribution control and
in the intro panel.

---

## Open Government Licence v3.0

The large majority of the data. OGL requires **attribution** and nothing else —
no share-alike, and commercial use is permitted.

| Source | Publisher | Used for |
|---|---|---|
| LSOA 2021 boundaries | Office for National Statistics | Geographic backbone (England & Wales) |
| Data Zone 2022 boundaries | Scottish Government | Geographic backbone (Scotland) |
| ONS Postcode Directory | Office for National Statistics | Postcode lookup, search, geography crosswalks |
| Census 2021 | ONS, via NOMIS | Occupation, qualifications, car availability, population |
| Scotland's Census 2022 | National Records of Scotland | The same, for Scotland |
| Price Paid Data | HM Land Registry | Property values (England & Wales) |
| Residential property sales | Registers of Scotland, via statistics.gov.scot | Property values (Scotland) |
| Personal Incomes Statistics (Table 3.15) | HM Revenue & Customs | Income distribution model |
| Small-area income estimates | Office for National Statistics | Out-of-sample validation |
| OS Open Roads | Ordnance Survey | Major road overlay |
| NaPTAN | Department for Transport | Travel connections rating |

Required notice, which the map displays:

> Contains OS data © Crown copyright and database right 2026.
> Contains public sector information licensed under the Open Government
> Licence v3.0.

Ordnance Survey additionally asks that OS OpenData products carry the Crown
copyright acknowledgement above, which the same line covers.

---

## Open Database Licence (ODbL) — OpenStreetMap

**Two layers only: bank branches and place labels.**

ODbL is materially different from OGL. It requires attribution *and* imposes
**share-alike**: if you publicly distribute a database derived from OSM, that
derived database must itself be offered under ODbL.

Required notice, which the map displays:

> © OpenStreetMap contributors (ODbL)

### What this means in practice

- **Using the map internally** carries no share-alike obligation. ODbL bites on
  distribution, not on use.
- **Publishing the map publicly** — which this project is set up to do —
  distributes `gb_branches.json` and `gb_places.json`, both derived from OSM.
  Those files are therefore offered under ODbL.
- **The rest of the project is unaffected.** The affluence index, the income
  model and the road overlay contain no OSM data, so they carry only the OGL
  attribution requirement. This is why the OSM-derived layers are deliberately
  isolated in their own pipeline steps (`build_branches.py`, `build_places.py`)
  and their own output files — they can be dropped without touching anything
  else.

If share-alike ever becomes unwelcome, removing those two steps produces a build
that is entirely OGL. You lose branch catchments and city labels.

---

## Operators' own branch lists — used to delete, never to publish

**One input, no output: `pipeline/verify_branches.py`.**

Barclays publishes a branch finder. The project reads it to answer one question
about records it already has — *is this branch still open?* — and deletes the
ones that are not.

| What is taken | What is done with it |
|---|---|
| The sitemap that `robots.txt` names, which disallows nothing under `/branch-finder/` | Enumerate branch pages, one request |
| One postcode per branch page, fetched once and cached | Decide whether an existing OSM record still corresponds to something |

Nothing is redistributed. No coordinate, address, name or opening hour from
Barclays reaches `web/data/gb_branches.json`; the branch layer remains
OSM-derived and ODbL, with closed entries removed. The cached pages stay in
`data/raw/`, which is gitignored.

This is deliberately a narrower use than the postcode lending data below, and
the difference is the point. There, values would have been published. Here, a
publicly advertised fact about which shops are open is used to remove stale
records from somebody else's database — which improves the accuracy of what is
published without adding anything to it.

If that ever changes — if branch coordinates start coming from an operator
rather than from OSM — the licensing question changes with it and has to be
asked again.

---

## The modelled figures are ours, not the sources'

The income band estimates are **modelled output**, not published statistics. No
source in this list publishes individual income below parliamentary
constituency. Attributing HMRC as the source of the underlying tax data is
correct; presenting the small-area estimates as HMRC figures would not be.

The same applies to the affluence index and the travel connections rating: both
are constructed here, from the sources above, using methods documented in the
README.

These outputs, and the code that produces them, are all rights reserved. See
[LICENSE](LICENSE). That is a statement about the original work only; it does
not and cannot license any of the third-party data above.
