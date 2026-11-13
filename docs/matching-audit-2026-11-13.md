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
| Client row exists but has no current price | KingStore Nizhnevartovsk: 27 retained-price autoload rows concern Galaxy S25+, Z Flip7, and A56; their client rows have blank `Price` | Recognize the client row as unpriced and do not claim it is absent; still compare autoload with Avito |
| Autoload and Avito prices differ while the client row cannot be priced | Independent values are available in both places | Report only the confirmed autoload-to-Avito discrepancy |

The script leaves the old autoload price in place when its current run finds no
priced client row. An old autoload price therefore does not establish a current
client price. The monitor must retain this distinction in its diagnostics.

Open evidence requests: SOTIKS Ivanovo autoload row 559 and client rows 97/104;
the owner's rule for a blank client price; and whether Kaluga's explicitly
different SIM variants should remain distinct. No automatic equivalence is
assumed for these cases.
