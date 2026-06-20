# Singapore GST Tax Treatment & Tax-Code Assignment Reference

**Purpose.** Authoritative reference for an automated bookkeeping AI agent that must assign the
**correct GST tax code to each line** of supplier invoices, sales invoices, and receipts for
Singapore companies, then emit accounting-import files for **Xero** and the proprietary **QBS
Ledger** (AI-Account / Autocount / SQL Acc style short codes).

**Primary authority.** Inland Revenue Authority of Singapore (IRAS), `iras.gov.sg`. The two
load-bearing primary sources used here are:

- **IRAS e-Tax Guide — "GST: General Guide for Businesses" (17th Edition, published 30 Jan 2026).**
  Confirms current rate (9% since 1 Jan 2024) and gives the verbatim definitions of the four
  supply types in Section 4. [PDF](https://www.iras.gov.sg/media/docs/default-source/e-tax/etaxguide_gst_gst-general-guide-for-businesses(1).pdf?sfvrsn=8a66716d_97)
- **IRAS "List of International Services — An Excerpt of the GST Act" (with effect from 1 Jan 2020, last updated 17 Feb 2020).**
  The full statutory text of **Section 21(3)(a)–(y)** of the GST Act — the definitive enumeration of
  zero-rated international services. [PDF](https://www.iras.gov.sg/media/docs/default-source/uploadedfiles/pdf/list-of-international-services-extract.pdf?sfvrsn=d4781b9c_24)

> **Disclaimer.** This is engineering reference material to drive automated *first-pass* classification.
> It is not tax advice. Final GST treatment is the responsibility of the company / its accountant.
> Low-confidence lines must be flagged for human review (see §9).

---

## 1. Current GST rate and history

| Period | Standard GST rate |
|---|---|
| 1 Apr 1994 – 30 Jun 2007 | 3% → 4% → 5% (introduced 1994 at 3%) |
| 1 Jul 2007 – 31 Dec 2022 | **7%** |
| 1 Jan 2023 – 31 Dec 2023 | **8%** |
| **1 Jan 2024 onwards** | **9%** |

- GST was introduced in **1994**; the rate has been **9% with effect from 1 Jan 2024**
  (second step of the two-stage 7%→8%→9% increase announced in Budget 2022).
  Source: IRAS General Guide for Businesses §2.1; [IRAS Current GST rates](https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/basics-of-gst/current-gst-rates); [IRAS GST rate change overview](https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/gst-rate-change/gst-rate-change-for-business/overview-of-gst-rate-change).
- **Only GST-registered businesses can charge GST** (General Guide §2.1). A supplier with no GST
  registration number on its document should generally **not** be producing a 9% input-tax line.
- The agent must apply the rate **by time-of-supply / invoice date**: 8% for 2023 documents, 9% for
  1 Jan 2024 onward. The tax-code *string* is the same; the *rate variant* differs (Xero exposes
  separate 7% / 8% / 9% rate objects).

---

## 2. The four GST treatment categories

IRAS classifies every supply as **taxable** (standard-rated or zero-rated) or **non-taxable**
(exempt or out-of-scope). Source: IRAS General Guide §4 (verbatim).

| Category | Code | GST charged | Definition (IRAS) | Input tax on related purchases |
|---|---|---|---|---|
| **Standard-Rated** | **SR** | **9%** | "GST is charged at the prevailing rate of 9% by GST-registered businesses on all sales of goods and services made in Singapore." (§4.1) | **Claimable** (subject to normal input-tax rules) |
| **Zero-Rated** | **ZR** | **0%** | "GST is charged at 0%. There are two categories: 1) exports of goods; 2) provision of international services." (§4.2) | **Claimable** — this is the key advantage over exempt |
| **Exempt** | **ES** | None | "Supplies specifically exempted under the Fourth Schedule … financial services, sale and lease of residential properties and local supply of investment precious metals (IPM). No GST needs to be charged." (§4.3) | **NOT claimable** (input tax attributable to exempt supplies is irrecoverable, subject to De Minimis / partial-exemption rules) |
| **Out-of-Scope** | **OS** | None | "Supplies which are outside the scope of the GST Act … where the place of supply is outside Singapore. No GST needs to be charged." (§4.4) | N/A — outside the system |

**Claimability summary for the agent.** Both **SR and ZR** preserve input-tax recovery; only **SR**
carries 9% output/input tax. **ES** blocks input-tax recovery. **OS** is outside GST entirely.
For *purchases*, the practical question is "does the supplier's document show a GST amount we can
reclaim?" — only standard-rated **purchases** from a GST-registered supplier give a reclaimable input
tax line.

**Deemed supplies** (General Guide §4.5): gifts of goods costing > $200 (excl. GST) where input tax
was claimed, private/non-business use of business assets, disposal of business assets for free. These
are output-tax events but rarely appear as document lines the agent ingests; treat as out-of-scope of
this classifier and flag if encountered.

---

## 3. Zero-rated supplies in detail (the main concern)

Zero-rating means **0% GST charged but input tax remains claimable**. Two pillars (General Guide §4.2):

### 3.1 Export of goods
- Goods that **will be or have been exported**, with **documentary proof** of export held at the
  point of supply. Source: General Guide §4.2.1.
- On a *sales* document: goods shipped to an overseas address, export/shipping docs, foreign customer.

### 3.2 International services — Section 21(3) of the GST Act
"You may zero-rate your supply of services if it falls within the description of international
services under Section 21(3) of the GST Act. **It should be noted that not all services provided to
overseas customers can be zero-rated.**" (General Guide §4.2.2).

The full statutory list runs from **21(3)(a) to 21(3)(y)** (often summarised as "the list of
international services"). Verbatim categories from the IRAS *List of International Services* extract:

| § | Category (paraphrased from statute) | Typical document examples |
|---|---|---|
| **(a)** | **International transport of passengers or goods** (not ancillary handling): by air/land where transport is outside↔outside, SG→outside, or outside→SG; by sea outside↔outside or to/from SG and substantially outside SG. | **International freight / air & sea cargo, overseas airfare, the international leg of a shipment** |
| **(b)** | Ancillary transport activities (loading, unloading, handling) supplied by the same supplier as part of a qualifying (a) transport supply. | Handling charges bundled with international freight |
| **(c)** | Insuring / arranging insurance / arranging the transport for passengers or goods under (a) or (b). | Freight insurance, freight forwarding arranging |
| **(d)** | Letting on hire of a means of transport used wholly outside Singapore. | Overseas vehicle/vessel hire |
| **(e)** | Services directly in connection with **land or improvements situated outside Singapore** (incl. construction, repair, estate agents, architects, surveyors, engineers re overseas land). | Construction supervision / surveying of overseas property |
| **(f)** | Services directly in connection with **goods situated outside Singapore** when performed. | Repair/inspection of goods located overseas |
| **(g)** | Services directly in connection with goods for export, supplied to a person belonging outside Singapore. | Pre-export processing for overseas buyer |
| **(h)** | Prescribed **financial services** in connection with goods for export / goods moving outside↔outside. | Trade-finance on exports |
| **(i)** | Cultural, artistic, sporting, educational, entertainment, exhibition/convention services **performed wholly outside Singapore** (and ancillary organising). | Running an event held overseas |
| **(j)** | Services under a contract with, and directly benefiting, **a person belonging in a country outside Singapore who is outside SG** at the time (or a SG GST-registered person). | Generic export-of-services limb |
| **(k)** | Prescribed services to a person **wholly in his business capacity belonging outside Singapore** (engineers, lawyers, accountants, consultancy, data processing, training, testing of goods outside SG, handling/storage of import/export goods, etc. — Second Schedule). | **Professional / consultancy services to an overseas business** |
| **(l)** | Prescribed services for the **handling of ships or aircraft**, or handling/storage of goods carried in any ship/aircraft (Third Schedule; ports, FTZ, designated areas, Portnet). | Port/terminal handling |
| **(m)** | Pilotage, salvage or towage performed in relation to ships or aircraft. | Marine pilotage |
| **(n)** | Surveying / classification of any ship or aircraft for a register. | Vessel classification survey |
| **(o)** | The supply (incl. letting on hire) of any ship or aircraft. | Aircraft / ship lease |
| **(p)** | Prescribed repair, maintenance, broking or management of any ship or aircraft (Sixth Schedule). | Ship/aircraft MRO |
| **(q)** | **Telecommunication services** — provision of telecom transmitted (i) outside→outside, (ii) **SG→outside**, or (iii) **outside→SG** (Fifth Schedule: international leased circuits, roaming, IDD, etc.). | **International calls / IDD / roaming / international leased lines on a telco bill** |
| **(r)** | Services supplied in relation to a **foreign trust** (Fourth Schedule conditions). | Trustee services for foreign trust |
| **(s)** | Services relating to **co-location in Singapore of computer server equipment** for a person belonging outside SG. | Overseas-customer data-centre colocation |
| **(t)** | Prescribed services re an **electronic system for import/export of goods** (Seventh Schedule — e.g. permit systems). | Trade-permit platform services |
| **(u)** | Supply / promulgation of an **advertisement** intended to be substantially promulgated **outside Singapore**. | Overseas-targeted advertising |
| **(v)** | Supply (incl. letting on hire) of any **air container or sea container** used for international transport of goods. | International shipping container hire |
| **(w)** | Prescribed repair / maintenance / management of such air/sea containers (Eighth Schedule). | Container MRO |
| **(x)** | Supply (incl. letting/hire) of **qualifying aircraft parts** certified airworthy. | Airworthy aircraft parts |
| **(y)** | Prescribed services directly in connection with **prescribed goods** (auction/exhibition, broking, conservation/restoration, insurance, management, storage, valuation) for an overseas person, where goods are at an approved warehouse / under customs control (Ninth Schedule — antiques, art, jewellery, precious metals/stones, wine, etc.). | Fine-art storage/valuation for overseas owner |

Source for the entire table: IRAS *List of International Services* extract (verbatim Section 21(3)(a)–(y)).

### 3.3 The user's examples — why telco & freight invoices carry a separate 0% line

**Telco (e.g. Telco A / Telco B).** A Singapore telco bill mixes:
- **Standard-rated (SR, 9%)** charges — local mobile/broadband subscription, local calls, equipment,
  domestic usage consumed in Singapore.
- **Zero-rated (ZR, 0%)** charges — **international/IDD calls, international roaming, and
  international leased-circuit / international transmission** services. These qualify under
  **§21(3)(q)** because the telecommunication is transmitted SG→outside or outside→SG.

Because the two halves attract different GST treatment, the telco itemises them separately and the
**bookkeeping import must split them into two lines** — one `Standard-Rated Purchases` line carrying
9% GST and one `Zero-Rated Purchases` line carrying $0 GST.

**This is exactly what the on-disk sample shows.** `BillTemplate (3).csv` is a Xero purchase import for
**Telco B** and **Telco A** bills, with two lines per bill:

```
Telco B,  ... Telephone charges (SR), 1, 1.07,   ..., Standard-Rated Purchases, 0.1,  ... SGD
Telco B,  ... Telephone charges (ZR), 1, 2.28,   ..., Zero-Rated Purchases,     0,    ... SGD
Telco A,  ... Telephone charges (SR), 1, 1000.00,..., Standard-Rated Purchases, 90.00,... SGD
Telco A,  ... Telephone charges (ZR), 1, 50.00,  ..., Zero-Rated Purchases,     0,    ... SGD
```

Note the Telco A SR line: 1000.00 × 9% = 90.00 (confirms the 9% rate), while the ZR line's TaxAmount
is 0. The agent must reproduce this **one-row-per-tax-treatment** behaviour.

**Freight / logistics.** A freight or forwarder invoice typically splits:
- **International freight** (air/sea cargo, the cross-border leg) and its ancillary
  loading/handling and freight insurance → **ZR** under **§21(3)(a)/(b)/(c)/(v)**.
- **Local/domestic charges** consumed in Singapore — local trucking/last-mile delivery within SG,
  local warehousing, local admin/documentation fees → **SR 9%**.
- **Import GST** and disbursements paid to Customs are handled separately (see §5.3 — these are not a
  standard supplier-supply line; the GST-on-imports is `GST on Imports` / `IM`, not `TX`).

So a single forwarder invoice can legitimately produce SR, ZR, and OS/disbursement lines.

---

## 4. Exempt supplies and out-of-scope supplies

### 4.1 Exempt supplies (ES) — Fourth Schedule to the GST Act
"No GST needs to be charged" and **input tax is not recoverable**. Main exempt items
(General Guide §4.3 + [IRAS Supplies Exempt from GST](https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/charging-gst-(output-tax)/when-is-gst-not-charged/supplies-exempt-from-gst)):

- **Financial services** (e.g. interest on loans/deposits, issue/sale of shares/bonds, life insurance,
  currency exchange, certain fees that are not for an explicit service).
- **Sale and lease of residential property** (vs. commercial property, which is standard-rated).
- **Supply of investment precious metals (IPM)** — qualifying gold/silver/platinum (≥ 99.5%/99.9%).
- **Supply of digital payment tokens** (exempt from 1 Jan 2020).

### 4.2 Out-of-scope supplies (OS)
Outside the scope of the GST Act; "No GST needs to be charged." Examples (General Guide §4.4.2):

- **Sale of goods delivered from a place outside Singapore to another place outside Singapore**
  (third-country sale / sale of goods not brought into Singapore).
- **Sales of overseas goods within a Free Trade Zone (FTZ)** or within a **Zero-GST / Licensed warehouse**.
- **Salaries paid to employees** for their services.
- **Private transactions** (e.g. a GST-registered trader selling his personal stamp collection — §3.1.12).

> Distinction the agent must respect: **ES blocks input-tax recovery; OS is simply outside GST.**
> Both show $0 GST, but they map to **different** target tax codes (ES vs OS).

---

## 5. Imported services / overseas vendors

When the supplier belongs **outside Singapore**, GST is usually **not charged by the supplier** on the
face of the invoice (it is an out-of-scope supply from the SG buyer's perspective). Two special regimes
can still bring GST into play:

### 5.1 Reverse Charge (RC) — B2B imported services
- A GST-registered business that is **not entitled to full input-tax credit** (e.g. partially exempt
  traders, or non-taxable bodies that are GST-registered) must **account for output GST as if it were
  the supplier** on imported services and low-value goods, and may claim a corresponding input tax
  subject to its recovery position. From **1 Jan 2020** for imported services; extended **1 Jan 2023**
  to low-value goods and all remote (B2B) services.
  Source: [IRAS — Local businesses importing services](https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/gst-and-digital-economy/local-businesses); [IRAS e-Tax Guide: GST Reverse Charge](https://www.iras.gov.sg/media/docs/default-source/e-tax/gst-taxing-imported-services-by-way-of-reverse-charge-(2nd-edition).pdf).
- On the document this looks like an **overseas supplier invoice with no GST line**. Most fully-
  taxable SMEs are **not** subject to reverse charge. The agent should treat an overseas-supplier
  service invoice as **OS / Imported Services** by default and **flag for review** whether RC applies
  (it depends on the buyer's GST-recovery status, which is master data, not on the document).

### 5.2 Overseas Vendor Registration (OVR) — B2C remote/digital services & low-value goods
- Overseas vendors with **global turnover > S$1m** and **B2C supplies to Singapore > S$100k/yr** must
  register for GST and **charge 9% GST** on remote services (digital from 1 Jan 2020; all remote
  services and imported low-value goods from 1 Jan 2023).
  Source: [IRAS — Overseas businesses](https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/gst-and-digital-economy/overseas-businesses).
- **How it shows up:** an overseas vendor (e.g. a SaaS / cloud / marketplace) invoice that **shows a
  GST/9% line and a Singapore GST registration number** even though the vendor is foreign. In that
  case the GST is a genuine charge. For a **GST-registered SG business buyer**, OVR GST on B2B
  purchases is generally not the right mechanism (the business should provide its GST number so the
  vendor zero-rates / does not charge OVR GST, and RC rules apply instead) — another **flag-for-review**
  situation.

**Practical detection rule:** *GST line present + supplier shows a GST reg. no.* → treat the GST as
real input tax (TX / Standard-Rated Purchases), regardless of supplier nationality. *Overseas supplier
+ no GST line* → OS / Imported Services, flag for RC assessment.

---

## 6. DECISION TABLE — detecting the tax code from a document

Signals to extract per line/document: explicit GST wording (`GST 9%`, `0%`, `zero-rated`, `exempt`,
`out of scope`), **supplier/customer GST registration number** present?, **supplier/customer country**
(SG vs overseas), **nature of supply** (international transport/freight, telecom international leg,
export of goods, financial service, residential rent, etc.), **GST amount on the line** (>0 or 0),
**document/invoice date** (→ 8% vs 9%), and whether the document has **mixed lines**.

### 6.1 PURCHASES (supplier invoices / receipts → what WE buy)

Evaluate **top-to-bottom; first match wins**, per line:

| # | Observable signal on the line/document | Tax code (SR/ZR/ES/OS) | Xero *TaxType | QBS / short code | Notes |
|---|---|---|---|---|---|
| 1 | Line explicitly marked **zero-rated / 0% / "(ZR)"**, or nature = international freight / international telecom leg / export-related | **ZR** | `Zero-Rated Purchases` | `ZR` | The telco/freight 0% line. Input tax claim = $0 but recoverable status preserved. |
| 2 | Line shows a **positive GST amount** AND supplier shows a **GST reg. no.** (typically 9%, or 8% if dated 2023) | **SR** | `Standard-Rated Purchases` | `TX` | Default for normal local taxable purchases. Verify GST ≈ net × rate. |
| 3 | Line explicitly **exempt**: residential property rent, bank interest/financial charges, IPM | **ES** | `Exempt Purchases` | `ES` (or `EP`) | $0 GST; input tax not claimable. |
| 4 | **Overseas supplier**, **no GST line**, service / digital | **OS** (flag for Reverse Charge) | `No GST` / `Out Of Scope` (or `Imported Services` if RC applies) | `OS` / `NT` (or `IM`) | RC depends on buyer's recovery status (master data) → **flag**. |
| 5 | **Import permit / Customs GST** document (GST on imported goods) | Import GST | `GST on Imports` | `IM` | Not a supplier-supply line; GST charged by Customs on CIF+duties. |
| 6 | Non-taxable / no-GST items: salaries, government fees, bank transfers, supplier **not GST-registered**, meals with no GST | **OS / No-GST** | `No GST` / `Out Of Scope` | `NT` | Sample `Expenses Bill Import` uses `NT` for "Eat dinner" with $0 GST. |
| 7 | None of the above clearly determinable | **DEFAULT + FLAG** | `Standard-Rated Purchases` (tentative) | `TX` (tentative) | Low confidence → route to human review (§9). |

### 6.2 SALES (our invoices to customers → what WE sell)

| # | Observable signal | Tax code | Xero *TaxType | QBS / short code | Notes |
|---|---|---|---|---|---|
| 1 | We are **GST-registered**, customer in **Singapore**, ordinary goods/services | **SR** | `Standard-Rated Supplies` | `SR` | Charge 9%. Default for local sales. |
| 2 | **Export of goods** (overseas delivery, shipping docs) OR **international service** under §21(3) (international transport/freight, services to overseas person & consumed abroad, etc.) | **ZR** | `Zero-Rated Supplies` | `ZR` | 0% GST, output preserved as zero-rated. **"Not all services to overseas customers can be zero-rated"** — verify §21(3) fit. |
| 3 | **Exempt** supply: sale/lease of **residential** property, financial services, IPM, digital payment tokens | **ES** | `Exempt Supplies` | `ES` | $0 GST; restricts our input-tax recovery. |
| 4 | **Place of supply outside Singapore** (e.g. goods sold & delivered overseas-to-overseas, never enter SG) | **OS** | `Out Of Scope Supplies` | `OS` | Not a SG supply. |
| 5 | We are **NOT GST-registered** | No GST | `No GST` | `NT` | Cannot charge GST at all. |
| 6 | Local sale of prescribed goods (mobile phones, memory cards, off-the-shelf software) **> $10,000** to a GST-registered customer | Customer Accounting | `Customer Accounting` (`SROVR`-style / DS code) | `SR` + customer-accounting flag | Output tax accounted by the buyer; **flag** (General Guide §2.5–2.6). |
| 7 | Indeterminate | **DEFAULT + FLAG** | `Standard-Rated Supplies` (tentative) | `SR` (tentative) | Low confidence → human review. |

**Default / fallback philosophy.** **Standard-rated is the legal default** — "if a supply does not
fall within one of the other categories, it is standard-rated." So when uncertain *and* the party is
GST-registered, the safest fallback is SR — **but tagged low-confidence and flagged**, because wrongly
charging 9% on a genuinely zero-rated/exempt line is a real error. Never silently emit ZR/ES/OS without
a positive signal.

---

## 7. Exact tax-code strings per target system

### 7.1 Xero (Singapore) — `*TaxType` column values

Xero's CSV import (Purchases sheet and Sales sheet both carry a `*TaxType` column — confirmed in the
on-disk `Xero Template.xlsx`, sheets **Purchase** and **Sales**) uses **human-readable rate names**.
The on-disk `BillTemplate (3).csv` proves the exact purchase strings.

| Treatment | **Purchases / Spend** `*TaxType` | **Sales / Income** `*TaxType` |
|---|---|---|
| Standard-Rated (9%) | `Standard-Rated Purchases` ✅ confirmed in sample | `Standard-Rated Supplies` |
| Zero-Rated (0%) | `Zero-Rated Purchases` ✅ confirmed in sample | `Zero-Rated Supplies` |
| Exempt | `Exempt Purchases` | `Exempt Supplies` |
| Out-of-Scope | `Out Of Scope Purchases` (a.k.a. `No GST`) | `Out Of Scope Supplies` |
| Imported services (reverse charge) | `Imported Services` | — |
| GST on imports | `GST on Imports` | — |
| No GST | `No GST` | `No GST` |

> **Verification status.** The **purchase** strings `Standard-Rated Purchases` and
> `Zero-Rated Purchases` are *directly verified* from the customer's working Xero import file
> (`BillTemplate (3).csv`). The sales-side and exempt/OS strings follow Xero's documented Singapore
> naming convention; the live Xero Central pages
> ([Default tax rates SG](https://central.xero.com/s/article/Default-tax-rates-GL-SG),
> [Choose the right tax treatment SG](https://central.xero.com/s/article/Choose-the-right-tax-treatment-on-transactions-SG))
> are JavaScript-rendered and did not return raw text to automated fetch — **confirm the exact
> sales/exempt/OS strings inside the target Xero org (Settings → Tax rates) before going live**, since
> Xero allows orgs to rename or add rates.

**Underlying Xero API `TaxType` enum codes (Singapore)** — useful if integrating via the Xero
Accounting API rather than CSV. From the Xero PHP SDK (`calcinai/xero-php`, `TaxType.php`):

| Enum value | Meaning |
|---|---|
| `SROUTPUT` | Standard-Rated Supplies (output) |
| `ZERORATEDOUTPUT` | Zero-Rated Supplies (output) |
| `ES33OUTPUT` / `ESN33OUTPUT` | Exempt Supplies (regulation 33 / non-reg-33) |
| `OSOUTPUT` | Out-of-Scope Supplies (output) |
| `DSOUTPUT` | Deemed Supplies (output) |
| `TXINPUT` | Standard-Rated Purchases (input) |
| `ZERORATEDINPUT` | Zero-Rated Purchases (input) |
| `IMINPUT` | Imported Services / reverse charge (input) |
| `GSTONIMPORTS` | GST on Imports |
| `BLINPUT` | Blocked input tax (disallowed) |
| `NONE` | No GST |

Source: [Xero Accounting API — Tax Rates](https://developer.xero.com/documentation/api/accounting/taxrates); SDK enum [calcinai/xero-php TaxType.php](https://github.com/calcinai/xero-php/blob/master/src/XeroPHP/Models/Accounting/TaxType.php).

### 7.2 AI-Account / Autocount / SQL Acc short codes (QBS Ledger)

From the on-disk header templates (`~/Desktop/LocalTest/header template/`),
`Tax Code` is a short alpha code. Observed values:

| Short code | Meaning | Seen in template |
|---|---|---|
| **SR** | Standard-Rated **supply** (sales, 9% output) | AI-Account **Sales Invoice Import** (`SR`, e.g. incorporation fee, USD bookkeeping) |
| **TX** | Taxable **purchase** (standard-rated input, 9%) | AI-Account **Expenses Bill Import** (`TX`, e.g. "Accounting fee"; sample shows GST 210 on net 3000 = **7%**, i.e. that template predates the rate change — the agent must compute GST from the invoice date, not copy the template's rate) |
| **ZR** | Zero-Rated (0%) | Mapped from Xero `Zero-Rated Purchases`/`Supplies` |
| **ES** | Exempt supply | financial / residential / IPM |
| **OS** | Out-of-Scope | overseas place of supply |
| **NT** | No tax / not applicable | AI-Account **Expenses Bill Import** (`NT`, e.g. "Eat dinner", GST 0) |
| **IM** | Import GST / imported services | Customs import GST |

> **Important:** these short codes match the **AutoCount / SQL Accounting / Singapore** convention,
> where typically `TX` = taxable purchase (input), `SR` = standard-rated supply (output), `ZR` = zero-
> rated, `ES`/`EP` = exempt, `OS` = out-of-scope, `IM` = import, `NT`/`NR` = no-tax/non-reclaimable.
> The exact code set is **defined per company file** in the target software — the agent should read the
> company's GST code master list and map to it, not hardcode. AutoCount and SQL Acc maintain their own
> editable GST tax-code tables; the AI-Account templates above are the concrete strings the importer
> expects.

**Cross-system mapping table (canonical):**

| Canonical | Xero Purchase | Xero Sales | QBS purchase | QBS sales |
|---|---|---|---|---|
| Standard-rated 9% | `Standard-Rated Purchases` | `Standard-Rated Supplies` | `TX` | `SR` |
| Zero-rated 0% | `Zero-Rated Purchases` | `Zero-Rated Supplies` | `ZR` | `ZR` |
| Exempt | `Exempt Purchases` | `Exempt Supplies` | `ES` | `ES` |
| Out-of-scope | `No GST` / `Out Of Scope` | `Out Of Scope Supplies` | `OS` / `NT` | `OS` |
| No GST / not registered | `No GST` | `No GST` | `NT` | `NT` |
| Imported services (RC) | `Imported Services` | — | `IM` | — |
| GST on imports | `GST on Imports` | — | `IM` | — |

---

## 8. Worked example — mapping a telco bill (Telco A / Telco B pattern)

Input (telco bill, GST-registered supplier, dated 2025 → 9%):

| Charge | Net | Nature | §21(3)? | Tax code | Xero purchase TaxType | QBS code | GST |
|---|---|---|---|---|---|---|---|
| Local mobile/broadband subscription | 1000.00 | Consumed in SG | No | SR | `Standard-Rated Purchases` | `TX` | 90.00 (9%) |
| IDD / international roaming | 50.00 | Telecom SG↔outside | §21(3)(q) | ZR | `Zero-Rated Purchases` | `ZR` | 0.00 |

This reproduces the BillTemplate CSV pattern exactly. **Two output lines from one bill.**

---

## 9. Implementation recommendation for the ADK invoice agent

**Goal:** a deterministic-where-possible, LLM-assisted-where-needed, per-line tax classifier that emits
the correct tax-code string per target system, with confidence and review routing.

### 9.1 Master-data taxonomy (YAML)
Encode the taxonomy once, in master-data YAML, so target-specific strings live outside the model logic:

```yaml
gst:
  rate_by_date:           # apply by time-of-supply / invoice date
    - { from: "2007-07-01", to: "2022-12-31", rate: 0.07 }
    - { from: "2023-01-01", to: "2023-12-31", rate: 0.08 }
    - { from: "2024-01-01", to: null,         rate: 0.09 }
  treatments:
    SR: { name: Standard-Rated, claimable: true,  rate_ref: current }
    ZR: { name: Zero-Rated,     claimable: true,  rate: 0.0 }
    ES: { name: Exempt,         claimable: false, rate: 0.0 }
    OS: { name: Out-of-Scope,   claimable: null,  rate: 0.0 }
    IM: { name: Imported/RC,    claimable: cond,  rate: 0.0 }
  code_map:
    xero:
      purchase: { SR: "Standard-Rated Purchases", ZR: "Zero-Rated Purchases",
                  ES: "Exempt Purchases", OS: "No GST", IM: "Imported Services" }
      sales:    { SR: "Standard-Rated Supplies",  ZR: "Zero-Rated Supplies",
                  ES: "Exempt Supplies", OS: "Out Of Scope Supplies" }
    qbs:        # AI-Account / AutoCount / SQL Acc short codes
      purchase: { SR: "TX", ZR: "ZR", ES: "ES", OS: "NT", IM: "IM" }
      sales:    { SR: "SR", ZR: "ZR", ES: "ES", OS: "OS" }
  zero_rated_section_21_3:   # signal lexicon → ZR
    - { sec: "a/b/c", signals: [international freight, air cargo, sea freight, IDD shipping, ocean/air freight] }
    - { sec: "q",     signals: [IDD, international call, roaming, international leased circuit] }
    - { sec: "j/k",   signals: [service to overseas customer consumed abroad] }
    - { sec: "export", signals: [export of goods, overseas delivery, shipping/airway bill] }
```

### 9.2 Per-line classification pipeline (extraction → transformation)
1. **Document-level signals:** supplier/customer name + country, supplier **GST reg. no.** present?,
   our own registration status (from company master data), document date → applicable rate, doc type
   (purchase vs sales).
2. **Line-level extraction:** description, net amount, any per-line GST amount, any explicit tax
   wording (`SR`/`ZR`/`GST 9%`/`0%`/`exempt`).
3. **Rule engine first** (deterministic, ordered as the §6 decision tables). Most telco/freight/normal
   lines resolve here. Validate arithmetic: `gst ≈ net × rate` confirms SR; `gst == 0` supports ZR/ES/OS.
4. **LLM fallback** only for lines the rules cannot resolve (ambiguous nature-of-supply, mixed
   descriptions), constrained to output one of the taxonomy codes + a confidence score + the signal it
   relied on.
5. **Map** the resolved code → target-system string via `code_map[system][purchase|sales]`.
6. **Mixed-line handling:** never collapse a multi-treatment document; emit one import row per
   (description, tax-treatment) pair, mirroring `BillTemplate (3).csv`.

### 9.3 Signals to extract (minimum set)
supplier_name, supplier_country, supplier_gst_regno_present, customer_country, our_gst_registered,
invoice_date, line_description, line_net, line_gst_amount, explicit_tax_keyword, nature_of_supply.

### 9.4 When to flag for the accountant (human-in-the-loop)
Flag a line as **low-confidence / needs review** when:
- Overseas supplier service invoice with no GST line → **Reverse Charge** decision (depends on buyer's
  input-tax recovery status, not on the document).
- GST amount present but `gst ≠ net × expected_rate` (wrong rate, rounding beyond tolerance, or 7/8/9%
  mismatch vs invoice date).
- Supplier shows GST but **no GST reg. no.** visible (possible non-registered supplier wrongly charging GST).
- Nature-of-supply matches a **zero-rated/exempt** pattern but wording is ambiguous (e.g. "services" to
  a foreign-named entity — "not all services to overseas customers can be zero-rated").
- Customer-accounting candidates (prescribed goods > $10k to GST-registered buyer).
- Any line the rule engine could not resolve and the LLM confidence < threshold (e.g. < 0.8).
- Residential vs commercial property, IPM, or financial-service lines (ES vs SR is high-impact).

### 9.5 Safe defaults
- GST-registered party + indeterminate → **SR** (legal default) but **flagged**.
- Never emit ZR/ES/OS without a positive matched signal.
- Always derive the **rate** from invoice date, but the **tax-code string** from the treatment.
- Treat the target company's **own GST code master list** as the source of truth for strings; the maps
  in §7 are the defaults to reconcile against on first run.

---

## 10. Sources

- IRAS e-Tax Guide — **GST: General Guide for Businesses (17th Ed., 30 Jan 2026)** — rate, four supply types, deemed supplies, reverse charge & OVR footnotes: [PDF](https://www.iras.gov.sg/media/docs/default-source/e-tax/etaxguide_gst_gst-general-guide-for-businesses(1).pdf?sfvrsn=8a66716d_97)
- IRAS — **List of International Services (Section 21(3) extract, w.e.f. 1 Jan 2020)**: [PDF](https://www.iras.gov.sg/media/docs/default-source/uploadedfiles/pdf/list-of-international-services-extract.pdf?sfvrsn=d4781b9c_24)
- IRAS — **Current GST rates**: https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/basics-of-gst/current-gst-rates
- IRAS — **Overview of GST Rate Change**: https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/gst-rate-change/gst-rate-change-for-business/overview-of-gst-rate-change
- IRAS — **Providing international services (zero-rate)**: https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/charging-gst-(output-tax)/when-to-charge-0-gst-(zero-rate)/providing-international-services
- IRAS — **Supplies Exempt from GST**: https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/charging-gst-(output-tax)/when-is-gst-not-charged/supplies-exempt-from-gst
- IRAS — **Local businesses importing services (Reverse Charge)**: https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/gst-and-digital-economy/local-businesses
- IRAS — **Overseas businesses (OVR)**: https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/gst-and-digital-economy/overseas-businesses
- IRAS e-Tax Guide — **GST: Reverse Charge (2nd Ed.)**: https://www.iras.gov.sg/media/docs/default-source/e-tax/gst-taxing-imported-services-by-way-of-reverse-charge-(2nd-edition).pdf
- Singapore Statutes Online — **GST Act 1993, s.21**: https://sso.agc.gov.sg/Act/GSTA1993?ProvIds=pr21- (HTTP 403 to automated fetch; statutory text obtained via the IRAS s.21(3) extract above)
- Xero — **Default tax rates (SG)**: https://central.xero.com/s/article/Default-tax-rates-GL-SG
- Xero — **Set the tax treatment on transactions (SG)**: https://central.xero.com/s/article/Choose-the-right-tax-treatment-on-transactions-SG
- Xero — **Singapore GST in Xero**: https://central.xero.com/s/article/Singapore-GST-in-Xero
- Xero Developer — **Accounting API Tax Rates**: https://developer.xero.com/documentation/api/accounting/taxrates
- Xero PHP SDK — **TaxType enum (SG constants)**: https://github.com/calcinai/xero-php/blob/master/src/XeroPHP/Models/Accounting/TaxType.php

### On-disk evidence used
- `BillTemplate (3).csv` (local test data, not committed) — Xero purchase import; telco bills split into `Standard-Rated Purchases` (9%) and `Zero-Rated Purchases` (0%) lines.
- Source telco bill PDFs (local test data, not committed) — INV-0001, INV-0002 pattern.
- `~/Desktop/LocalTest/header template/Xero Template.xlsx` — Purchase & Sales sheets, both with `*TaxType` column.
- `~/Desktop/LocalTest/header template/AI-Account Template/Expenses Bill Import Template (1).csv` — `Tax Code` values `TX`, `NT`.
- `~/Desktop/LocalTest/header template/AI-Account Template/Sales Invoice Import Template (1).csv` — `Tax Code` value `SR`.
- `~/Desktop/LocalTest/header template/Autocount Template/` & `SQL Header/` — AutoCount and SQL Acc import templates (per-company GST code tables).
