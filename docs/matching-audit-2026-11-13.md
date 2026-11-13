# Matching audit, 13 November 2026

Evidence: the saved price-to-Avito run report supplied by the project owner, a
copy of the KingStore Nizhnevartovsk client `Телефоны` tab, and the monitor's
existing server logs. The source files and credentials are not stored in Git.
No manual Avito API calls were made for this audit.

| Confirmed case | Saved-report evidence | Monitor handling |
| --- | --- | --- |
| Technical `Причина без цены` column blocked Android rows | Pixel 10 was matched by the price script but rejected by the monitor's older parser | Exclude the same technical columns as the source script |
| Storage units differ | KingStore Nizhnevartovsk: 12 Xiaomi 17 Ultra autoload rows show `1 ТБ`, while the four priced client rows show `1024 ГБ`; Kaluga has eight analogous Pixel 11 rows | Match equivalent capacities after the exact script match |
| Client SIM is unknown | iHub Noyabrsk: 29 retained-price autoload rows have a corresponding priced client row with `Не знаю` versus a specified SIM | Use the unique row whose other SKU fields agree; keep two different specified SIM variants distinct |
| Client row exists but has no current price | KingStore Nizhnevartovsk: 27 retained-price autoload rows concern Galaxy S25+, Z Flip7, and A56; their client rows have blank `Price`. The next routine monitor run also linked 46 SOTIKS Ivanovo active listings, including autoload row 559 to unpriced `Телефоны` 101, and 12 Kaluga Pixel 10 Pro listings to unpriced client rows. | Recognize the client row as unpriced and do not claim it is absent; still compare autoload with Avito |
| Autoload and Avito prices differ while the client row cannot be priced | Independent values are available in both places | Report only the confirmed autoload-to-Avito discrepancy |
| Avito list rate limit prevents a city's check | The routine run stopped at listing page 25/26 with HTTP 429 for Sachkoff Omsk, KingStore Nizhnevartovsk, and APPHUB Ekaterinburg. | Use `Retry-After` when supplied; otherwise wait a full minute before retrying the same page. Do not send a partial city's report. |

The routine run after the blank-price change identified 46 unpriced client
matches in SOTIKS Ivanovo, 12 in Kaluga, 11 in Geek Choice Belgorod, six in
iHub Noyabrsk, and 97 in U-SE Elektrostal. These were previously counted as
unmatched or skipped, despite an identifiable client row.

The script leaves the old autoload price in place when its current run finds no
priced client row. An old autoload price therefore does not establish a current
client price. The monitor must retain this distinction in its diagnostics.

The Kaluga routine run also logged five iPhone 17 listings with two nearby
client rows each, including autoload row 383 and client rows 152/157. The
saved report shows a priced `SIM + eSIM` client row and a `Только eSIM`
autoload row. These are left separate pending the owner's rule; no price is
borrowed across explicitly different SIM variants.

The iHub Noyabrsk routine run reduced its unmatched active listings to three
PlayStation rows. The saved report shows client titles with `Гарантия` and
`Рассрочка`, while autoload titles omit those words. The source row columns
are needed before defining a console title rule.

Open evidence requests: the owner's rule for a blank client price; whether
Kaluga's explicitly different SIM variants should remain distinct; and iHub
Noyabrsk PlayStation client/autoload rows 3–5 with their column headers.
