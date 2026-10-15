# SmartGaráž — politika taxonómie vozidiel (fitment)

**Toto nie je právna rada.** Je to technická politika produktu, ktorá má
znížiť riziko sporu s TecDoc / LKQ / Szekop / Inter Cars / podobnými
komerčnými katalógmi. Finálne posúdenie nechaj na právnika.

Širší právny / prevádzkový kontext (TecAlliance licencia, sui generis,
nekalá súťaž, fingerprinting):
[`CATALOG_LICENSING.md`](CATALOG_LICENSING.md).

## Cieľ

Mať **vlastnú** hierarchiu vozidiel (`Make → Model → Generácia → Motor → Rok`), ktorú:

1. **vlastní a spravuje SmartGaráž** (vlastné ID, vlastná štruktúra),
2. **nekopíruje** cudzie vehicle databázy ani ich ID,
3. slúži len na **filter kompatibility** v e‑shope (nie na predaj licencovaných
   TecDoc dát tretím stranám).

## Čo JE povolené (vstupy)

- **Všeobecné verejné fakty** o autách: značka, modelový názov, bežné
  označenie motora (napr. „1.6 TDI“), orientačné roky výroby — ako ich
  uvádza výrobca / trh.
- **Vlastná kurácia** SmartGaráž (manuálne doplnenie SK trhu).
- **Mapovanie voľného textu** z B2B feedov (`make`/`model` ako string)
  **na našu** hierarchiu — bez uloženia ich interných vehicle ID.
- **OE / cross čísla dielov** z obalu, verejného popisu alebo dodávateľského
  atribútu dielu (`_sg_oe_numbers`) — to sú identifikátory **dielu**, nie
  kópia vehicle DB.
- Verejné open dátové zdroje s jasnou licenciou (napr. Wikidata) — len ako
  inšpirácia na fakty, nie import celej DB; pri použití uviesť v provenancii.
- **Vlastný VIN dekód** (`bot/vin_decode.py`, `inc/sg-vin.php`, pack
  `vin_wmi_sg.json`): ISO 3779/3780 + SG WMI kurácia + verejné továrenské
  kódy (napr. Mercedes W205). Výstup len `sgv-*`. **Plný VIN sa neukladá**
  (redacted + fingerprint). **Zakázané:** TecDoc / Autovini / Cebia /
  CarVertical / iná licencovaná VIN databáza.

## Čo je ZAKÁZANÉ

Nikdy do gitu, DB, meta ani UI neukladaj:

| Zakázané | Príklady |
|---|---|
| TecDoc / TecAlliance ID | `kType`, `ktypenr`, `linkage`, TecDoc vehicle keys |
| Cudzie vehicle ID dodávateľov | LKQ/Szekop/Inter Cars/Autodoc vehicle node IDs, ich strom „ako je“ |
| Dump / scrape katalógu | hromadné sťahovanie cudzej vehicle taxonómie |
| Scrape maloobchodnej konkurencie | Autodoc, filtreoleje, Mister-Auto… — katalóg / fitment bez licencie |
| Reverzné inžinierstvo DB | 1:1 kópia štruktúry a výberu záznamov z komerčnej DB |

**Aj nepriamy scrape** e‑shopu, ktorý TecDoc licenciu platí, je NO‑GO
(pozri `CATALOG_LICENSING.md`). Deny‑list hostiteľov:
`bot/catalog_licensing.py`.

Ak feed pošle také pole → **zahodiť** (viď `fitment.sanitize_inbound_fitment`).

## Šedá zóna (prakticky)

- **Fakty** (existuje VW Golf, motor 1.6 TDI) nie sú „vlastníctvom“ TecDoc.
- **Databáza ako dielo** (výber + usporiadanie + ID) môže byť chránená —
  preto máme **vlastné `sgv-…` ID** a vlastný strom, nie ich kľúče.
- Fitment na produkte = naše polia `_sg_fit_*` + OE dielu; **nie**
  „TecDoc vehicle link“.
- Dodávateľov používať ako zdroj **tovaru a cien**, nie ako zdroj
  vehicle taxonómie.

## Vlastné ID (povinné)

- Make: `sgv-make-<slug>`
- Model: `sgv-model-<make>-<model>`
- Generácia (voliteľne): `sgv-gen-<make>-<model>-<label>` — verejné
  trhové označenia (III, VII, B8…), nie cudzie vehicle ID
- (Voliteľne neskôr) Engine: `sgv-eng-<…>`

Tieto ID generuje SmartGaráž. Nikdy nepoužívaj cudzie číselníky ako primárny kľúč.

## Ako rozširovať hierarchiu

1. Uprav `bot/data/vehicle_hierarchy_sk.json` (zdroj pravdy).
2. Spusti `python bot/validate_vehicle_hierarchy.py`.
3. Skopíruj JSON do
   `web/wp-content/themes/hello-elementor-child/data/vehicle_hierarchy_sk.json`.
4. Commit — v provenancii uveď, že ide o nezávislú kuráciu / public facts.

## Kontrola v CI / pred commitom

```bash
python bot/validate_vehicle_hierarchy.py
python -m unittest bot.test_catalog_licensing -v
```

Failne pri TecDoc/kType/Intercars ID vzoroch alebo pri chýbajúcich `sg_id`.
Deny‑list zablokuje crawl zakázaných katalógových hostiteľov pred sieťou.
