# IntelliSight sample dataset

`xperion_adobe_analytics_sample.csv` is a 1,800-row random sample (seed 7) of the 10,776-row synthetic Adobe Analytics
Data Warehouse export that IntelliSight ships with (github.com/Anant2510/IntelliSight): the web analytics of Xperion
Airways, a fictional airline, for 2023 and 2024, one row per
hit with the time columns, the report suite, eVar1 to eVar20 (page, section, channel, device, loyalty tier, cabin,
origin, destination, route ...) and 50 of its events (page views, visits, bookings, booking revenue, cart additions and
removals, checkouts, purchases, ancillaries, disruption and loyalty events ...). The IntelliSight pack's agents open it with
`dataset_name: xperion`; pass your own export as `dataset` (CSV text) instead.
