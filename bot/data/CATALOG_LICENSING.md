# SmartGaráž — licencia katalógov autodielov (záväzná prevádzková politika)

**Toto nie je právna rada.** Je to interná prevádzková politika majiteľa
(2026-08), ktorá má zabrániť porušeniu TecAlliance / autorského práva /
nekalej súťaže. Finálne posúdenie nechaj na právnika.

Súvisiace: [`VEHICLE_TAXONOMY_POLICY.md`](VEHICLE_TAXONOMY_POLICY.md),
`bot/catalog_licensing.py`, `bot/media_licensing.py`,
`bot/validate_vehicle_hierarchy.py`.

## Zhrnutie (jedna veta)

**Bez platnej licencie TecDoc (TecAlliance) a bez súhlasu autorov pôvodných
e‑shopov nesmie SmartGaráž preberať ani kopírovať kompletné katalógy /
vehicle databázy z konkurenčných e‑shopov.**

## Prečo (riziká, ktoré berieme vážne)

### 1. Licenčné práva TecAlliance

TecDoc je chránená databáza (štruktúra vozidiel, strom kategórií,
cross‑referencie OE, TecDoc ID). Komerčné využitie bez licenčnej zmluvy
je zakázané — či cez API, web, alebo lokálnu DB.

**Nepriame využitie:** aj scrape e‑shopu, ktorý si TecDoc licenciu platí,
stále znamená neoprávnené využitie chráneného obsahu TecAlliance bez odplaty.

### 2. Osobitné právo k databáze (sui generis)

Zhotoviteľ databázy má výhradné právo udeliť súhlas na vyťahovanie
(extraction) alebo opätovné využitie (re‑utilization). Masové kopírovanie
cudzej databázy (vlastné párovanie, popisy, fotky, strom) bez súhlasu je
protiprávny zásah.

### 3. Nekalá súťaž (Obchodný zákonník SR)

Preberanie výsledkov práce a investícií konkurencie s cieľom ušetriť na
licenciách / vývoji napĺňa znaky nekalej súťaže (parazitovanie).

### 4. Detekcia a dôsledky v praxi

- Watermarking / fingerprinting (zámerné chyby, fiktívne väzby, skryté
  Unicode / HTML) → dôkaz o skopírovaní.
- Cease & Desist, spätné licenčné poplatky, zmluvné pokuty.
- Technické blokovanie (Cloudflare, rate limit).

## Čo JE povolené

| Zdroj | Čo smieme brať | Podmienka |
|---|---|---|
| **LKQ / Szekop B2B** | SKU, EAN, B2B cena, sklad, voľný text make/model | Prihlásený účet; žiadne TecDoc ID |
| **Würth B2B** (`eshop.wurth.sk`) | Art.-Nr./SKU, EAN, názov, B2B cena/sklad, DIN/ISO text, KBCH PDF bez úprav | `WURTH_B2B=1` + účet. Fotky len ak `WURTH_MEDIA_OK=1` (súhlas VOP) |
| **PRO‑TEC / Metabond / EF** | Tovar, EAN, B2B cena, sklad | Vlastný B2B účet |
| **Vlastná SG taxonómia** `sgv-*` | Nezávislá kurácia SK trhu | `vehicle_hierarchy_sk.json` |
| **OE čísla dielu** | Z obalu / atribútu dielu | Nie celá cross‑ref matica z cudzieho e‑shopu |

Fitment = `_sg_fit_*` + free‑text → `sgv-*`. Cudzie vehicle / TecDoc ID
**vždy zahodiť** (`fitment.sanitize_inbound_fitment`).

## Čo je ZAKÁZANÉ (NO‑GO)

1. Scrape maloobchodnej konkurencie (Autodoc, filtreoleje…) kvôli katalógu /
   fitmentu / fotkám.
2. Ukladanie TecDoc kľúčov (`kType`, `ktypenr`, `linkage`, …).
3. 1:1 kópia komerčnej vehicle taxonómie.
4. Obchádzanie B2B loginu / paywallu kvôli cenníku.
5. Rehost Würth studio fotiek bez `WURTH_MEDIA_OK` / VOP súhlasu.
6. **Akékoľvek skryté formátovanie** v textoch/HTML na shope
   (zero‑width, soft hyphen, `display:none`, fingerprint komentáre).

Deny‑list: `bot/catalog_licensing.py`. Médiá: `bot/media_licensing.py`.
Skryté formátovanie: `strip_hidden_formatting` / `sanitize_sk_copy`.

## Médiá (fotky)

| Situácia | Postup |
|---|---|
| B2B fotka bez watermarku | Rehost + meta `_sg_image_source` / `_sg_image_original_url` / `_sg_image_fetched_at` |
| Watermark / logo dodávateľa v URL | Zahodiť |
| Würth / witglobal CDN | Default **OFF**; zapnúť len `WURTH_MEDIA_OK=1` |
| C&D výzva | `python bot/purge_supplier_images.py --source wurth_b2b --dry-run` |

## Würth — špecifiká

- **Smieme:** Art.-Nr., EAN, názov, B2B cena (po logine), sklad, technické
  parametre (DIN/ISO), KBCH PDF bez zmeny obsahu.
- **Nesmieme:** dump celého `eshop.wurth.sk`; guest cenník ako „naša“ B2B cena;
  rehost fotiek bez súhlasu; vydávať Würth tovar za nebrandované generikum.
- Heijunka: max **20–50 SKU** / beh.
- Env: `WURTH_B2B=1` (cena/sklad), `WURTH_MEDIA_OK=1` (fotky — len so súhlasom).

## Skryté formátovanie (Jidoka)

Nikde v názve, popise ani HTML produktu nesmie zostať:

- zero‑width znaky (`U+200B`…), soft hyphen, BOM, bidi override
- HTML s `display:none` / `visibility:hidden` / `opacity:0` / `font-size:0`
- `aria-hidden` / `hidden` bloky, HTML komentáre, `<script>` / `<style>`

Pipeline volá `strip_hidden_formatting` pred zápisom do WooCommerce.

## B2B vs. konkurencia

- Szekop/LKQ leaf = tovar a ceny (≤50), nie ich vehicle DB.
- Konkurenčný retail = nikdy ako zdroj katalógu. Heuréka = len obchodná
  inteligencia (manuálne), nie hromadný scrape.

## Kontrola

```bash
python bot/validate_vehicle_hierarchy.py
python -m unittest bot.test_catalog_licensing bot.test_media_licensing bot.test_sanitize_sk_copy -v
```

Ak agent žiada crawl konkurencie / Würth fotky bez `WURTH_MEDIA_OK` →
**zastav linku (Jidoka)** a odkáž na tento dokument.
