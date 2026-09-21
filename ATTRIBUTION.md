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
