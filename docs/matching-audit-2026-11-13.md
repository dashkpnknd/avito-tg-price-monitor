# Matching audit, 13 November 2026

Evidence: the saved price-to-Avito run report supplied by the project owner, a
copy of the KingStore Nizhnevartovsk client `Телефоны` tab, the monitor's
existing server logs, and read-only checks of the linked client/autoload sheets.
The source files and credentials are not stored in Git.
No manual Avito API calls were made for this audit.

| Confirmed case | Saved-report evidence | Monitor handling |
| --- | --- | --- |
| Technical `Причина без цены` column blocked Android rows | Pixel 10 was matched by the price script but rejected by the monitor's older parser | Exclude the same technical columns as the source script |
| Storage units differ | KingStore Nizhnevartovsk: 12 Xiaomi 17 Ultra autoload rows show `1 ТБ`, while the four priced client rows show `1024 ГБ`; Kaluga has eight analogous Pixel 11 rows | Match equivalent capacities after the exact script match |
| Client SIM is unknown | iHub Noyabrsk: 29 retained-price autoload rows have a corresponding priced client row with `Не знаю` versus a specified SIM | Use the unique row whose other SKU fields agree; keep two different specified SIM variants distinct |
| Client row exists but has no current price | KingStore Nizhnevartovsk: 27 retained-price autoload rows concern Galaxy S25+, Z Flip7, and A56; their client rows have blank `Price`. The next routine monitor run also linked 46 SOTIKS Ivanovo active listings, including autoload row 559 to unpriced `Телефоны` 101, and 12 Kaluga Pixel 10 Pro listings to unpriced client rows. | Recognize the client row as unpriced and do not claim it is absent; still compare autoload with Avito |
| Autoload and Avito prices differ while the client row cannot be priced | Independent values are available in both places | Report only the confirmed autoload-to-Avito discrepancy |
| Avito list rate limit prevents a city's check | The routine run stopped at listing page 25/26 with HTTP 429 for Sachkoff Omsk, KingStore Nizhnevartovsk, and APPHUB Ekaterinburg. | Use `Retry-After` when supplied; otherwise wait a full minute before retrying the same page. Do not send a partial city's report. |
| Client has only `Title` and `Price` for consoles | iHub Noyabrsk client `пс` rows 3–5 append `Гарантия` and/or `Рассрочка`; autoload `Avito - Игровые приставки` rows 3–5 omit them and have a `Model` column absent from the client sheet. | Compare complete titles after removing only those trailing sale terms and a spaced ` + ` delimiter; preserve `Дисковод` and model variants. This rule applies to every title-only sheet. |
| Monitor selected fewer client tabs than the script | Control workbook `Параметры` rows 2–10 has a blank `ЛИСТ` column for all running accounts. Thus `getProductsFromPrice` scans all tabs with a `Price` header, while the monitor previously used only registered category tabs or tabs marked `(авито)`. | Read all price-bearing tabs in workbook order for ordinary client sheets; retain explicit tab handling for special legacy layouts. |

The routine run after the blank-price change identified 46 unpriced client
matches in SOTIKS Ivanovo, 12 in Kaluga, 11 in Geek Choice Belgorod, six in
iHub Noyabrsk, and 97 in U-SE Elektrostal. These were previously counted as
unmatched or skipped, despite an identifiable client row.

The script leaves the old autoload price in place when its current run finds no
priced client row. An old autoload price therefore does not establish a current
client price. The monitor must retain this distinction in its diagnostics.

The Kaluga routine run also logged five iPhone 17 listings with two nearby
client rows each, including autoload row 383 and client rows 152/157. A
read-only sheet check confirms that row 383 is `Только eSIM`, `256 ГБ`, white,
while row 152 is `SIM + eSIM`, `256 ГБ`, white, and row 157 is `Только eSIM`,
`512 ГБ`, white. Neither client row is the same SKU. The script can retain an
older autoload price when a current client row is absent; the autoload price
alone does not establish a current client match. Do not borrow a price across
different SIM or capacity values.

Geek Choice Belgorod autoload row 396 specifies `Не знаю` for SIM. Client
rows 221 and 258 are both priced `iPhone 17 Pro`, `512 ГБ`, silver, but have
different explicit SIM variants and different prices. Without a unique SKU
there is no safe client-price comparison. A matching price is not a reliable
identity key: it could hide precisely the stale-price error being monitored.

The iHub Noyabrsk routine run reduced its unmatched active listings to three
PlayStation rows. The linked sheets confirm the exact client and autoload
titles, so the title-only rule above addresses this pattern globally. No
additional sheet copies are needed for the current matching changes.

The next natural server run after the title rule completed iHub Noyabrsk with
no unmatched warning (31 confirmed price differences, zero alerts before the
grace period). Kaluga still logged five unmatched iPhone 17 rows; its saved
source-script report itself marks autoload row 383 `Нет в прайсе`. LimeStore
Gatchina's saved script report likewise marks Galaxy Watch 9 autoload row 69
`Нет в прайсе`. These are not evidence that the monitor failed to reproduce a
successful script match.
