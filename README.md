# USPTO Trademark Office Action Monitor

Finds US trademark office actions issued against applications whose owner is
domiciled **outside the US** and which have **no attorney of record** (no US
representative). Covers both direct US applications and Madrid Protocol
s.66(a) designations (serial numbers beginning 79).

## How it works

A scheduled GitHub Action (`.github/workflows/daily.yml`) runs twice daily.
It downloads the USPTO **Trademark Full Text XML Data – Daily Applications**
bulk file (product `TRTDXFAP`) from the USPTO Open Data Portal, scans each
case file's prosecution history for office-action events issued in the last
few days, filters to foreign-domiciled owners with no attorney of record, and
commits the results:

- `data/latest.json` – new hits found by the most recent run (this is what the
  daily email reads)
- `data/recent.json` / `data/recent.csv` – rolling 7-day window of hits
- `data/archive/YYYY-MM-DD.json` – daily archive (only on days with hits)
- `data/state.json` – processed-file and dedupe state (do not edit)

`latest.json` also includes `debug_event_descriptions_in_window` – a frequency
table of all prosecution-history event descriptions seen in the date window,
used to tune the office-action matching patterns in `filter_oas.py`.

## Setup

1. Repository secret `USPTO_API_KEY`: a USPTO Open Data Portal API key
   (free; requires a USPTO.gov account verified via ID.me –
   https://data.uspto.gov/apis/getting-started).
2. Enable Actions. Run the workflow once manually (Actions → Daily USPTO OA
   monitor → Run workflow) to verify.

## Notes

- USPTO publishes each day's file the following morning (US Eastern time), so
  hits normally appear 1 day after the office action issues.
- Since August 2019 the USPTO requires foreign-domiciled applicants to be
  represented by a US-licensed attorney; Madrid designations typically arrive
  without one and receive an office action requiring appointment of US
  counsel. Those actions are included by design.
- All data processed is public USPTO record.
