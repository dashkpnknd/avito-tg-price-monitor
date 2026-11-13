# Connected projects

Runtime project credentials and spreadsheet IDs are stored only in the server's
`/opt/avito-price-monitor/data/projects.json`, never in Git. Telegram topic
bindings are stored in `data/telegram-topics.json` on the same server.

| Connected (MSK) | Project | Client tabs | Price mismatch delay | Telegram topic |
| --- | --- | --- | --- | --- |
| 2026-11-13 | APPHUB \| Екатеринбург | Телефоны, Макбуки, Айпады, Часы, Наушники, ПС, Дайсон | 5 hours | 3287 |
| 2026-11-13 | KingStore Екатеринбург | Телефоны, Макбуки, Айпады, Часы, Наушники, ПС, Дайсон | 5 hours | 3288 |

Both projects use the regular hourly scheduler. Their first check was not
triggered manually during onboarding.
