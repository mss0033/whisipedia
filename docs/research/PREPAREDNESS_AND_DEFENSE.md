# Source Research: Preparedness & Resilience, and Defensive & Security Literature

| | |
|---|---|
| Research date | 2026-10-10 |
| Method | Desk research plus live checks from the research host (generic `Whisipedia/0.1` User-Agent, GET of index pages, HEAD on files, OAI-PMH `Identify`/`ListIdentifiers`, JSON APIs, GitHub repo metadata). No bulk data was downloaded. |
| Legend | **✓** measured live on the research date · **≈** estimate · **R** endpoint answered 200 · **W** endpoint answered 403 to a non-browser client from this host (a WAF block on most `.mil` and some `.gov` sites; the material is public, but the adapter needs an approved access path, see §5.6) · **M** moved/renamed (new location recorded) |
| Scoring | PLAN §5.2 axes and weights, with the deltas in §2. License **and issuer authority** are hard gates. |
| Machine-readable form | [`../../registry/sources.yaml`](../../registry/sources.yaml) (vault shelves `preparedness` and `defense`) |
| Parent document | [`SOURCES.md`](SOURCES.md) |

Two collections:

1. **Preparedness & resilience** (`preparedness` shelf): how to grow and keep food, make water safe, give medical care with little support, keep shelter, power and tools working, navigate and communicate, and run communities and governments through and after a disaster.
2. **Defensive & security literature** (`defense` shelf): electronics and radar fundamentals, publicly released doctrine on planning and leading operations, intelligence analysis and counterintelligence history, protective security from the defender's side, and cybersecurity standards.

---

## 1. Selection philosophy

The quality bar is **"what a government would hand its own people to keep things running after a disaster"**, not "ten survival hacks". In practice:

1. **Issuer first.** A document gets in because of *who* issued it: a national agency with the mandate (FEMA, CISA, USDA and land-grant extension, CDC/ASPR, EPA, NOAA, USGS, NGA, FAA, USCG), a civil-protection agency of another country, a UN agency, a military doctrine or training command, an official historian's office, or a peer-reviewed open publisher. Blogs, forums, prepper sites and personality-driven content are out, however popular.
2. **Tested over clever.** Prefer documents that are themselves the output of testing and review: USDA-tested canning procedures, WHO water guidelines, Joint Trauma System clinical practice guidelines, NIST standards, doctrine that has gone through staffing.
3. **Field manuals and fundamentals over news.** The library should teach a capable adult to *do* the thing. Training manuals (NEETS, FEMA Independent Study, Seabee rate manuals, FAA handbooks), handbooks (Bowditch, Wood Handbook) and standards come first; reports and analysis second.
4. **Historical works are kept as history.** USDA Farmers' Bulletins, 1950s–80s civil defense manuals and ORNL reports such as *Nuclear War Survival Skills* are valuable and public domain, but some of their advice is outdated or unsafe (arsenical pesticides, obsolete canning methods, superseded radiation guidance). They are shelved and labelled as historical and never outrank the current edition (§2.3).
5. **Defensive framing.** For security topics the library is a defender's, planner's and historian's library: how to protect people and facilities, detect hostile surveillance, analyse information honestly, plan and lead, secure systems. §3 sets the boundaries.

---

## 2. Rubric deltas for these collections

The PLAN §5.2 axes and weights stay the same. Three rules are added.

### 2.1 Issuer authority is a hard gate

Each source gets an **issuer class**:

| Class | Who | Eligible tiers |
|---|---|---|
| **A** | National government agency or military command acting within its mandate; UN specialised agency; official historian | T1–T3 |
| **B** | Land-grant university extension, federally funded research centre, peer-reviewed open textbook or journal, recognised standards body (MITRE CVE/CWE program, OWASP) | T1–T3 |
| **C** | Established NGO with an editorial and clinical review process (Hesperian, MSF, Sphere) | T2–T3 |
| **D** | Community wikis, individual authors, commercial "survival" publishers, unsourced compilations | not eligible (may be referenced only through an existing broad source such as Wikipedia or the Survivor Library ZIM) |

An Authority axis score below 3 blocks `active`, the same way the license gate does. Example: Appropedia (CC BY-SA, open and well meaning) is class D and is not proposed; its useful material is covered by FAO, Peace Corps and USACE originals.

### 2.2 Currency versus historical value

Each document gets a `currency` label in its per-document metadata (§5.2):

- `current`: the issuer's current edition.
- `superseded`: replaced by a newer edition from the same issuer (`superseded_by` points to it).
- `historical`: from a series that has ended, kept for its history or for techniques nobody republished (Farmers' Bulletins, Office of Civil Defense manuals).
- `rescinded`: withdrawn by the issuer for being wrong or unsafe. Kept only in the vault history and never surfaced in default search.

The Oracle ranks `current` above `superseded` and `historical` for the same query, and the Navigator shows a banner on anything not `current` ("Superseded by FM 5-0 (2022)"; "Historical USDA advice: check current extension guidance before use").

### 2.3 Superseded doctrine and guidance

Doctrine, standards and guidance are re-issued often (Army FM 5-0 in 2010, 2014 and 2022; NIST SP 800-53 rev 4 and rev 5). **Superseded editions are kept** as history, as the Wikipedia full-history policy does. A superseded edition is a new *document version* (not a deletion) and keeps its publication number, edition date and checksum. That gives a researcher "what did Army planning doctrine say in 2014?" while default answers come from the current edition.

### 2.4 Scoring notes

- **Uniqueness** counts offline value: a 200 MB set of FEMA continuity guidance scores higher than its size suggests, because nothing else in the library covers continuity of government.
- **Bulk accessibility** is usually 2–3 here: most issuers publish index pages and PDFs, not dumps. The `doc-collection` adapter (§5.2) exists to make that tractable.
- **Cost efficiency** is high across both collections: the top 30 recommended sources together are ≈ 0.2–0.4 TB, against ≈ 20–35 TB for the year-one library.

---

## 3. Boundaries (policy)

These rules are binding. The registry encodes them with `legal_basis`, `status`, `tier` and `notes`, and the `doc-collection` adapter enforces them per document (§5.3).

### 3.1 Only lawfully public material

In scope **only** if at least one of these holds, as stated by the issuer:

- public domain (for example US federal works under 17 U.S.C. §105, or works whose copyright has expired);
- an open license (OGL v3, CC BY, CC BY-SA, CC BY-NC-SA for personal non-commercial use, CC0, or an equivalent issuer licence);
- a US DoD **Distribution Statement A** ("Approved for public release; distribution is unlimited") or an equivalent public-release marking from another government.

**Excluded**, regardless of where a copy can be found:

- classified material, and material leaked or published without authorisation (even if widely mirrored);
- **Distribution Statements B, C, D, E, F**, FOUO, CUI, "for official use only", "UK government and MOD use only", and similar restricted markings;
- **export-controlled technical data** (ITAR/USML, EAR) or anything carrying an export-control warning;
- anything behind an access agreement that forbids redistribution or offline copying (for example OSAC products, TRIPwire, HSDL restricted holdings);
- anything obtained from piracy or shadow libraries (already excluded in SOURCES §11).

A document that is public domain *as a work* but carries a restriction marking is still excluded: the marking wins.

### 3.2 Safety exclusions (regardless of availability or license)

The library does not hold material whose **primary content** is:

- synthesis, production, enhancement, dissemination or weaponisation of chemical, biological, radiological or nuclear agents;
- explosives, improvised munitions, weapon or firearm manufacture or modification;
- weapons guidance, targeting, fire control, or weapons effects for planning attacks;
- offensive tooling or techniques for attacking computer systems or people (exploit code, malware, intrusion playbooks, offensive surveillance of individuals).

**In scope**: defensive, protective, preparedness, medical-response, historical-analytical and educational fundamentals. For example: radar *principles* and weather radar; CBRN *medical management*, detection awareness, decontamination and shelter-in-place guidance; antiterrorism and physical security from the defender's side; surveillance *detection* and counter-surveillance awareness for protecting people and facilities; threat assessment; vulnerability *catalogues and defensive mappings* (CVE, CWE, ATT&CK as used by defenders); security configuration baselines.

### 3.3 Mixed collections are filtered per document

Many issuers publish in-scope and excluded documents side by side. The Army Publishing Directorate lists planning doctrine next to munitions and targeting manuals; the Joint Publications include a targeting series; DTIC and NPS Calhoun hold everything from water purification theses to weapons research. For these, **the collection is never ingested whole**. The `doc-collection` adapter applies, in order (§5.3):

1. the **distribution gate** (§3.1), read from the document itself;
2. a **series allow-list** (publication series known to be in scope);
3. a **series and keyword deny-list** (§3.2 topics);
4. **owner review** for everything that is not clearly allowed (§5.5).

Default is deny: a document that matches no allow rule is held for review, never fetched into the library automatically.

---

## 4. Sources by domain

Columns: **Access** = index page / API / OAI-PMH / bulk / git. **Live** = result of the 2026-10-10 check (legend above). **Basis** = license or distribution basis, from the issuer's own page or the document's front matter where reachable. Scores use PLAN §5.2 with the §2 deltas.

### 4.1 Agriculture & food

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **USDA Farmers' Bulletins (1889–1990s)** | USDA (digitised by NAL) | ≈ 2,000+ practical bulletins: crops, livestock, home food, small farms, building | PD (US federal) | archive.org metadata API (`usdanationalagriculturallibrary` collection ✓ 268,458 items; ✓ 577 hits for the bulletin title) | ≈ 30 GB | T1 | 4.1 | R. Historical: label `historical`; pesticide and canning advice superseded (§2.2) |
| **USDA Complete Guide to Home Canning** (AIB 539) + **NCHFP** | USDA NIFA / University of Georgia | Tested home canning, freezing, drying, curing procedures | Guide PD; NCHFP site pages © UGA (per page) | index page | ≈ 0.2 GB | T1 | 4.3 | R. The safety-critical reference for preservation: only tested procedures |
| **NAL Digital Collections** | USDA National Agricultural Library | Agricultural research and extension history | PD (federal), per item | Primo discovery (`search.nal.usda.gov`) | ≈ 50 GB select | T2 | 3.9 | R/M (redirects from naldc.nal.usda.gov) |
| **FAO Knowledge Repository** | UN FAO | Farming, fisheries, forestry, food security, seed, small-scale processing manuals | **CC BY 4.0 since Jan 2025**; older items mostly CC BY-NC-SA 3.0 IGO; per item | OAI-PMH (DSpace 7) + REST | ≈ 1–3 TB all; ≈ 200 GB select | T2 | 3.9 | R (OAI `Identify` timed out once; retry). Check licence per item |
| **USDA FoodData Central** | USDA ARS | Nutrient composition of foods | CC0 | bulk zip | ✓ 475 MB (2025-04-24 CSV) | T2 | 3.8 | R |
| **NRCS National Engineering Handbook, Conservation Practice Standards, Field Office Technical Guide** | USDA NRCS | Soil, irrigation, ponds, drainage, erosion control | PD | index page (`directives.nrcs.usda.gov`) | ≈ 5 GB | T2 | 3.8 | W |
| **Forest Service Treesearch** incl. **Wood Handbook** (FPL-GTR-282) | USDA Forest Service | Forestry, wood as a building material, fire | PD | index page | ≈ 100 GB select | T2 | 3.7 | R. Old FPL PDF URLs redirect to a landing page: resolve via Treesearch |
| **USDA PLANTS Database** | USDA NRCS | Plant taxonomy, distribution, uses | PD | bulk download | ≈ 0.1 GB | T2 | 3.4 | R/M (`plants.sc.egov.usda.gov`) |
| **Peace Corps ICE manuals** | Peace Corps | Appropriate technology: gardening, fish ponds, water, construction | PD (federal) | `files.peacecorps.gov`; ERIC copies | ≈ 5 GB | T2 | 3.6 | W |
| Land-grant Cooperative Extension publications | Individual universities | Regional growing, livestock, food safety | © per institution; some CC BY-NC | per institution | ≈ 50 GB | T3 | 3.5 | Per-institution licence review; start with those publishing under CC |
| ATTRA | NCAT (USDA-funded NGO) | Sustainable agriculture guides | © NCAT, free download | index page | ≈ 2 GB | T3 | 3.2 | R/M. Needs permission |

### 4.2 Water & sanitation

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **WHO IRIS: drinking water, sanitation and WASH-in-emergencies** (GDWQ 4th ed. + addenda; WHO/WEDC emergency technical notes) | WHO | Water quality guidelines, household treatment, emergency sanitation | CC BY-NC-SA 3.0 IGO (per item) | OAI-PMH ✓ (`iris.who.int/server/oai`, ✓ 277,401 records) | ≈ 20 GB select | T1 | 4.2 | R. Personal, non-commercial offline use fits the licence |
| **EPA water emergency guidance** (Emergency Disinfection of Drinking Water, water-sector security and resilience) | US EPA | Disinfection, boil notices, utility resilience | PD | index page | ≈ 0.5 GB | T1 | 4.0 | R |
| **EPA NEPIS** | US EPA | 1970–present EPA documents, incl. water and wastewater engineering manuals | PD | search + per-document PDF | ≈ TB scale; ≈ 50 GB select | T2 | 3.8 | R |
| **CDC emergency water and WASH guidance** | CDC | Making water safe in an emergency, hygiene, disaster sanitation | PD | index page | ≈ 0.1 GB | T1 | 4.1 | W (`cdc.gov`) |
| Sphere Handbook (2018) | Sphere Association | Humanitarian minimum standards: WASH, food, shelter, health | © Sphere, "all rights reserved"; educational reproduction, but posting online or other copying needs written permission | index page | ≈ 0.05 GB | T3 | 3.3 | W. Needs permission |

Army TC 4-02.3 *Field Hygiene and Sanitation* and USACE water-supply manuals are covered through the doctrine and engineering collections (§4.4, §4.8).

### 4.3 Austere medicine & public health

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **Joint Trauma System Clinical Practice Guidelines** | DoD Defense Health Agency | Evidence-based trauma and prolonged-field-care guidelines | PD; public release | index page (✓ 157 CPG links) | ≈ 1 GB | T1 | 4.5 | R/M (`/index.cfm/CPGs/cpgs`) |
| **Borden Institute** (*Emergency War Surgery* 5th US ed., Textbooks of Military Medicine) | US Army MEDCoE | Surgery, trauma, field medicine, military preventive medicine | PD (federal); some chapters contain third-party figures | index page | ≈ 10 GB | T1 | 4.3 | R |
| **WHO IRIS: clinical and emergency health** (IMAI District Clinician Manual, Emergency Triage, Essential Medicines, mass casualty management) | WHO | Clinical care where resources are scarce | CC BY-NC-SA 3.0 IGO | OAI-PMH ✓ | ≈ 50 GB select | T1 | 4.2 | R. Same registry entry as §4.2 |
| **ASPR TRACIE** | HHS ASPR | Healthcare emergency preparedness: surge, crisis standards of care, austere care | PD for ASPR-authored; third-party items linked | index page | ≈ 2 GB | T2 | 3.9 | R. Filter to ASPR-authored documents |
| **CHEMM** | HHS ASPR | Medical management of chemical casualties: triage, decontamination, antidotes | PD | index page | ≈ 0.5 GB | T2 | 3.8 | R. Medical response only (§3.2) |
| **REMM** | HHS ASPR | Radiation emergency medical management | PD | index page | ≈ 0.5 GB | T2 | 3.8 | R/M (`remm.hhs.gov`; `remm.hlth.gov` no longer resolves) |
| **CDC Stacks** (MMWR, Yellow Book, EID, public-health emergency guidance) | CDC | Public health reference | PD | repository (OAI-PMH) | ≈ 1 TB all; ≈ 50 GB select | T2 | 3.9 | W (site and OAI) |
| **NIOSH publications** incl. Pocket Guide to Chemical Hazards | CDC NIOSH | Occupational safety, PPE, tool and chemical hazards | PD | index page | ≈ 20 GB | T2 | 3.8 | W |
| MSF Clinical and Essential Drugs guidelines | Médecins Sans Frontières | Field clinical protocols | © MSF; free online | index page | ≈ 0.2 GB | T3 | 3.5 | R. Needs permission |
| Hesperian *Where There Is No Doctor* family | Hesperian Health Guides | Community health care without a doctor | Hesperian Open Copyright: non-commercial copying allowed, but **any digital format needs permission** | index page | ≈ 1 GB | T3 | 3.5 | Needs permission (request worth making) |
| NLM Disaster Information Management Research Center | NLM | Disaster medicine bibliography | PD | n/a | n/a | — | — | `disasterinfo.nlm.nih.gov` does not resolve: treat as retired, use archived copies only if found in an open archive |

### 4.4 Shelter, energy, tools & engineering

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **FEMA Building Science** (P-361 safe rooms, P-320, flood/wind/seismic retrofits) | FEMA | Hazard-resistant construction and retrofit | PD | index page | ≈ 5 GB | T1 | 4.2 | W (`fema.gov`) |
| **USACE publications** (EM/ER/EP/TM: water supply, power, construction) | US Army Corps of Engineers | Engineering manuals | Dist A per document | index page | ≈ 10 GB | T2 | 3.8 | W. Document-level filter (§5.3) |
| **Navy Seabee rate training manuals** (Builder, Utilitiesman, Construction Electrician, Engineering Aid, Equipment Operator) | NETC | Construction trades training | Dist A on title page | DTIC copies; EverySpec mirror for cross-checks | ≈ 3 GB | T2 | 3.7 | W (DTIC). No live official host |
| **OSTI.GOV**: DOE technical reports, incl. NREL energy reports and ORNL civil defense research (ORNL-5037 *Nuclear War Survival Skills* ✓ found) | DOE OSTI | Energy systems, power, shelter research | PD for most DOE reports; per record | API ✓ (`/api/v1/records`) | ≈ 50 GB select | T2 | 4.0 | R |
| Survivor Library (already held) | — | Pre-industrial practical know-how | PD scans | Kiwix ZIM | ✓ 220 GB | T2 | 3.7 | Already in `kiwix-curated-t2`; class D compilation of class A/B originals: no new entry |

### 4.5 Navigation & communications

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **Bowditch, *American Practical Navigator*** (Pub 9) | NGA | The standard navigation text: piloting, celestial, electronic navigation, weather, seamanship | PD | index page | ≈ 0.1 GB | T1 | 4.6 | R (index); direct PDF download W |
| **NGA nautical publications** (Pubs 229, 249, 102 *International Code of Signals*, 117 *Radio Navigational Aids*, Sailing Directions, Light Lists) | NGA | Sight reduction tables, signals, radio aids | PD | index page | ≈ 2 GB | T1 | 4.4 | R |
| **NOAA ENCs + US Coast Pilot** | NOAA Office of Coast Survey | US nautical charts (S-57) and sailing guidance | PD | bulk | ✓ ENC all 829 MB (updated 2026-10-10); ✓ Coast Pilot vol 1 7.7 MB; ≈ 0.2 GB all volumes | T1 | 4.3 | R. Daily ENC updates: quarterly snapshot |
| **USCG Navigation Center** (Inland/International Navigation Rules, Light Lists, Local Notices) | USCG | Rules of the road, aids to navigation | PD | index page | ≈ 1 GB | T1 | 4.2 | R |
| **FAA handbooks** (Pilot's Handbook of Aeronautical Knowledge, Aviation Weather Handbook, Airplane Flying Handbook, AIM) | FAA | Aeronautical knowledge, weather, navigation | PD | index page | ≈ 2 GB | T1 | 4.3 | R |
| **NCVEC amateur radio question pools** (Technician 2026–30, General 2023–27, Extra 2024–28) | NCVEC Question Pool Committee | Radio theory, rules, operating practice | **Released into the public domain** by the committee | index page | < 10 MB | T1 | 4.2 | R |
| **CISA SAFECOM field guides** (NIFOG, AUXFOG) | CISA | Interoperable public-safety and auxiliary (amateur) emergency communications | PD | index page | < 0.1 GB | T1 | 4.4 | R |
| **NTIA Redbook** | NTIA | Federal spectrum management manual | PD | index page | ≈ 0.05 GB | T2 | 3.7 | W |
| **NWS JetStream + WDTD Radar & Applications Course** | NOAA NWS | Weather fundamentals; Doppler weather radar interpretation | PD | index pages | ≈ 3 GB | T2 | 3.9 | WDTD R; JetStream W/M (now `noaa.gov/jetstream`) |
| **USGS topographic maps** (US Topo + Historical Topographic Map Collection) | USGS | 1:24,000 maps, 1880s–present | PD | TNM API | ≈ 10–20 TB all; select by region | T3 | 3.8 | R. Budget decision |
| ARRL Handbook / ARES manuals | ARRL | Amateur radio reference | © ARRL | — | — | T3 | 3.2 | Needs permission; NCVEC pools + FCC Part 97 (already via eCFR) cover the basics |

### 4.6 Emergency management & continuity of government

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **FEMA preparedness doctrine** (National Response Framework, NIMS, CPG 101, CPG 201 THIRA, NDRF, Planning Considerations series) | FEMA | How US emergency management is organised and how plans are written | PD | index pages | ≈ 1 GB | T1 | 4.5 | W |
| **FEMA continuity** (Federal Continuity Directives 1 and 2, Continuity Guidance Circular, plan templates) | FEMA National Continuity Programs | Continuity of operations and of government | PD | index page | ≈ 0.2 GB | T1 | 4.5 | W |
| **FEMA Independent Study courses** (IS-100/200/700/800, continuity, debris, mass care…) | FEMA EMI, now **NDEMU** | Self-paced EM training: student manuals and job aids | PD | index page (course list) | ≈ 20 GB | T1 | 4.3 | R/M (page now titled "NDEMU Independent Study") |
| **Ready.gov** | FEMA | Household and business preparedness | PD | site crawl | ≈ 1 GB | T1 | 4.0 | R |
| **CISA infrastructure resilience** (NIPP, sector resources, Infrastructure Resilience Planning Framework) | CISA | Critical infrastructure protection and recovery | PD | index page | ≈ 5 GB | T1 | 4.2 | R |
| **UK resilience guidance** (Civil Contingencies Act guidance, Emergency Preparedness, Emergency Response and Recovery, UK Government Resilience Framework, Prepare campaign) | UK Cabinet Office | Civil protection doctrine and household guidance | **OGL v3** (GOV.UK default) | GOV.UK Search API ✓ + content pages | ≈ 1 GB | T1 | 4.3 | R |
| **USGS Publications Warehouse** (earthquake, volcano, landslide, flood, water resources) | USGS | Natural hazards science and guidance | PD | API ✓ (✓ 186,593 records) | ≈ 200 GB select | T1 | 4.2 | R |
| **CRS reports** | Congressional Research Service | Non-partisan analysis: continuity of government, emergency powers, defense, infrastructure | PD | congress.gov (W); EveryCRSReport bulk mirror (R) | ≈ 20 GB | T1 | 4.3 | Prefer the official copy, verify mirror checksums against it |
| **GovInfo** (GAO reports, hearings, committee reports) | GPO | Oversight of preparedness and defense programs | PD | API ✓ (api.data.gov key) + bulkdata ✓ | ≈ 100 GB select | T2 | 4.0 | R |
| **Historical US civil defense corpus** (FCDA/OCD/DCPA/FEMA 1950–1990 manuals; ORNL reports) | US federal civil defense agencies | Shelter, radiological monitoring, community survival planning | PD | archive.org (✓ 3,827 "civil defense" texts 1945–1990, needs curation) + OSTI | ≈ 50 GB | T2 | 3.8 | R. Label `historical` |
| Sweden **MCF** (formerly MSB) *If Crisis or War Comes* + publications | Swedish Civil Defence and Resilience Agency | Household and municipal civil defence | Not stated on the page; Swedish agency texts are copyrighted unless decisions or laws | index page | ≈ 1 GB | T3 | 3.6 | R/M (`msb.se` now redirects to `mcf.se`). Ask the agency |
| Switzerland FOCP/BABS | Federal Office for Civil Protection | Civil protection, shelters, emergency planning | Terms page not reachable (W); federal open-data label is "open use, must provide the source" for datasets only | index page | ≈ 1 GB | T3 | 3.4 | R. Verify terms |
| Nordic household guidance (Norway DSB, Finland 72 tuntia, Denmark "Forberedt på kriser") | National civil-protection bodies | Household preparedness | Per agency | index pages | < 0.5 GB | T3 | 3.4 | Licence check per agency |
| Homeland Security Digital Library | NPS CHDS | Homeland security policy collection | Mixed; much is restricted to .gov/.mil users | — | — | T3 | 3.0 | R. Public subset only; overlaps GovInfo/CRS/Calhoun |

### 4.7 Radar, electronics & physics fundamentals

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **NEETS**, Navy Electricity and Electronics Training Series (NAVEDTRA 14173–14196, 24 modules) | Naval Education and Training Command | DC/AC, generators, solid state, power supplies, transmitters, antennas, radar principles, digital, microwave | Dist A on title page | DTIC copies + mirror cross-check | ≈ 0.6 GB | T1 | 4.3 | W. No live official host: take ≥ 2 copies, require matching checksums and the Dist A statement |
| **DOE Fundamentals Handbooks** (DOE-HDBK-1011 to 1019: electrical science, thermodynamics/fluids, instrumentation, mechanical science, chemistry, nuclear physics and reactor theory) | DOE | Plant-operator fundamentals | PD; approved for public release | DOE Technical Standards Program | ≈ 0.3 GB | T1 | 4.3 | R/M (old `standards.doe.gov` URLs redirect to energy.gov) |
| **NAWCWD TP 8347 *EW and Radar Systems Engineering Handbook*** (4th ed. 2013, DTIC ADA617071) | Naval Air Warfare Center Weapons Division | Radar equation, antennas, propagation, receivers | Dist A | DTIC | ≈ 0.03 GB | T1 | 4.0 | W. **Owner review**: in scope as engineering fundamentals; contents to be skimmed for §3.2 before promotion |
| **NIST Technical Series** (incl. historical NBS handbooks and circulars on radio propagation, measurement) | NIST | Measurement science, electronics, radio | PD in the US (17 U.S.C. §105) | `nvlpubs.nist.gov` + metadata repo (✓ git 376 MB) | ≈ 1 TB all; ≈ 100 GB select | T2 | 4.1 | R |
| **NASA NTRS** (radar, communications, power systems subsets) | NASA | Technical reports | PD for NASA-authored; per record | API ✓ (✓ 647,968 citations) | ≈ 100 GB select | T2 | 3.9 | R. Already noted in SOURCES §4 |
| MIT Radiation Laboratory Series (1947–53, 28 vols) | MIT / McGraw-Hill | The classic radar engineering series | Copyright status unverified (renewals) | archive.org | ≈ 5 GB | T3 | 3.6 | Rights check before any copy |

### 4.8 Operations planning & leadership doctrine

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **Army Publishing Directorate, public publications** (ADP, FM, ATP, TC, selected AR/DA Pam) | Department of the Army | Doctrine: planning (FM 5-0), operations, leadership (ADP 6-22), sustainment, engineering, field hygiene, physical security, land navigation | Dist A per document (stated in the PDF front matter, **not** in the index) | index pages per series (✓ ADP, FM; ✓ ≈ 600 ATP and ≈ 434 TC detail links) | ✓ ADP 1 PDF 8.2 MB; ≈ 30 GB filtered | T1 | 4.3 | R. **Document-level filter** (§5.3) |
| **Joint Publications** (JP 1, JP 3-0, JP 4-0, JP 5-0 Joint Planning, JP 3-28 Defense Support of Civil Authorities…) | Joint Chiefs of Staff | Joint doctrine | Dist A for the public JEL set | index page | ≈ 2 GB | T1 | 4.4 | W. Deny JP 3-60 targeting and fires series |
| **USMC MCPEL** (MCDP 1 *Warfighting*, MCDP 5 *Planning*, MCDP 6 *Command and Control*, MCWP 5-10) | US Marine Corps | Doctrine and philosophy of command | Dist A / PD | index page | ≈ 3 GB | T1 | 4.4 | W |
| **US Army Center of Military History** | US Army | Official histories ("Green Books"), lessons learned | PD | index page | ≈ 30 GB | T1 | 4.3 | W |
| **Army University Press / Combat Studies Institute** | US Army TRADOC | *Military Review*, Leavenworth Papers, staff-ride and campaign studies | PD for staff-authored; some articles author © | index page | ≈ 20 GB | T2 | 3.9 | W |
| **US Army War College Press** (*Parameters*, SSI monographs) | US Army War College | Strategy and leadership | PD mostly; per item | OAI-PMH ✓ (✓ 3,711 records) | ≈ 5 GB | T2 | 3.9 | R |
| **Naval War College Digital Commons** (*NWC Review*, Newport Papers, CMSI) | US Naval War College | Strategy, operations, maritime history | Per item | OAI-PMH ✓ (✓ 10,364 records) | ≈ 15 GB | T2 | 3.9 | R |
| **NDU Press** (*Joint Force Quarterly*, *PRISM*, Strategic Forum) | National Defense University | Joint operations and strategy | PD / Dist A | index page | ≈ 5 GB | T2 | 3.9 | W |
| **Air University Press** | Air University | Books and journals on airpower, leadership, strategy | Dist A on title pages; copyright per title | index page | ≈ 10 GB | T2 | 3.8 | W |
| **Air Force doctrine** (AFDP series) | LeMay Center | Air Force doctrine | Dist A | index page | ≈ 0.2 GB | T2 | 3.8 | W |
| **NPS Calhoun** (theses, faculty research, CHDS homeland security theses) | Naval Postgraduate School | Graduate research across all domains | Per item: in scope only with "Approved for public release; distribution is unlimited" in `dc.rights`/description | OAI-PMH ✓ (DSpace 7; ✓ 74,327 records; `oai_dc`, `mods`, `dim`, `qdc`…) | ≈ 1–2 TB all; ≈ 100 GB filtered | T2 | 3.9 | R. Filter on rights + subject |
| **DTIC public technical reports** | Defense Technical Information Center | Public-release DoD research | Dist A per record | public citation pages (no bulk API) | TB scale; select by accession list | T3 | 3.7 | W. Only by curated accession lists |
| UK Defence Doctrine (JDP series) | UK MOD (Defence Futures, formerly DCDC) | UK doctrine | **Not OGL**: Crown copyright, reproduction "for UK government and MOD use only" | — | — | T3 | 2.8 | Needs MOD permission; excluded from bulk copy |
| NATO Allied Joint Publications | NATO Standardization Office | Allied doctrine | NATO copyright; public subset only | — | — | T3 | 3.0 | W. Licence check |

### 4.9 Intelligence analysis & counterintelligence history

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **CIA Center for the Study of Intelligence** (*Studies in Intelligence* unclassified 1992–, books and monographs incl. Heuer, *Psychology of Intelligence Analysis*; Sherman Kent essays) | CIA | Analytic tradecraft, intelligence history | PD (US federal works); CSI notes some items are sold via GPO | index pages; PIA PDF on site | ≈ 3 GB | T1 | 4.5 | R |
| **Foreign Relations of the United States** (FRUS) | State Dept Office of the Historian | The official documentary history of US foreign policy, TEI XML | **PD** (repository README) | git ✓ | ✓ 14.8 GB repo | T1 | 4.6 | R |
| **CIA FOIA Electronic Reading Room** (CREST, historical collections) | CIA | Declassified documents | PD; officially declassified and released | index + per-document PDFs | ≈ 5–10 TB all; ≈ 200 GB curated collections | T3 | 3.9 | R. Budget: start with the curated historical collections |
| **NSA Center for Cryptologic History** (United States Cryptologic History series, Friedman collection, VENONA) | NSA | Cryptologic and SIGINT history | PD; declassified releases | index page | ≈ 20 GB | T2 | 4.0 | W |
| **ODNI / NCSC** (ICD 203 Analytic Standards, insider-threat, supply-chain and CI awareness) | ODNI / NCSC | Analytic standards; counterintelligence awareness | PD | index pages | ≈ 1 GB | T1 | 4.2 | W |
| **DCSA Center for Development of Security Excellence** | DCSA | Security, insider-threat and CI awareness courses, job aids, espionage case studies | PD | index page | ≈ 5 GB | T2 | 3.9 | W |
| **FBI Vault** (selected: VENONA, Ames, Hanssen, historical espionage cases) | FBI | FOIA releases | PD | index page | ≈ 1 TB all; ≈ 20 GB select | T2 | 3.7 | R |
| National Security Archive (GWU) briefing books | George Washington University | Annotated declassified document sets | Underlying documents PD; essays and compilation © GWU | index page | ≈ 50 GB | T3 | 3.6 | R. Needs permission for the site; fetch the underlying government documents from the issuer instead |

### 4.10 Protective security & surveillance detection (defensive)

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **CISA protective security** (Soft Targets and Crowded Places guide, Active Shooter Preparedness, Insider Threat Mitigation Guide, bomb threat checklist and procedures, vehicle ramming mitigation) | CISA | Protecting people, venues and facilities | PD | index page | ≈ 2 GB | T1 | 4.3 | R |
| **Interagency Security Committee** (Risk Management Process for Federal Facilities, Items Prohibited, Active Shooter guide, compliance benchmarks) | ISC (CISA) | Facility security standards | PD for the public documents; RMP appendices (Design-Basis Threat, countermeasures) are restricted and excluded | index page | ≈ 0.2 GB | T1 | 4.2 | R. Public documents only |
| **US Secret Service NTAC** (behavioural threat assessment, *Mass Attacks in Public Spaces*, school safety guides) | USSS National Threat Assessment Center | Preventing targeted violence | PD | index page | ≈ 0.2 GB | T1 | 4.4 | R (landing); `/research` sub-path 404 |
| **Army physical security and antiterrorism doctrine** (ATP 3-39.32 *Physical Security*, ATP 3-37.2 *Antiterrorism*, incl. surveillance detection) | Department of the Army | Defender-side planning, hostile-surveillance detection | Dist A per document | via APD (§4.8) | in APD entry | T1 | 4.2 | Part of the APD allow-list |
| **NIJ + NCJRS Virtual Library** | DOJ National Institute of Justice | Policing, security technology standards (body armour, communications), threat assessment research | PD for NIJ-authored; grantee reports per item | index page | ≈ 100 GB select | T2 | 3.8 | R |
| UK NPSA (formerly CPNI) and ProtectUK (NaCTSO) | MI5 / Counter Terrorism Policing | Protective security guidance | Crown copyright reported; OGL not confirmed | index page | ≈ 1 GB | T3 | 3.6 | NPSA W; ProtectUK R. Verify terms |
| **OSAC** | State Dept Bureau of Diplomatic Security | Overseas security reports | Login-gated; "for internal US private-sector use only"; redistribution can cost account access | — | — | **X** | 1.5 | W. **Excluded** (§3.1) |
| **TRIPwire** | DHS Office for Bombing Prevention | Bomb-prevention information sharing | Restricted, vetted access | — | — | **X** | — | **Excluded** (§3.1, §3.2) |

### 4.11 Cybersecurity standards

| Source | Issuer | What | Basis | Access | Size | Tier | Score | Live / notes |
|---|---|---|---|---|---|---|---|---|
| **NIST CSRC publications** (SP 800, SP 1800, FIPS, IR, CSWP; Cybersecurity Framework 2.0) | NIST | The reference set of federal security standards and guidance | PD in the US | index page + `nvlpubs` PDFs | ≈ 15 GB | T1 | 4.7 | R |
| **NIST OSCAL content** (SP 800-53 rev 5 catalog and baselines, machine-readable) | NIST | Controls as JSON/XML/YAML | PD (NIST) | git ✓ | ✓ 91 MB repo | T1 | 4.4 | R |
| **DISA STIGs and SRGs** | DISA | Secure configuration baselines | PD; public download | index page | ≈ 5 GB | T1 | 4.3 | R/M (`public.cyber.mil` → `www.cyber.mil`; landing page changed, re-check the download path) |
| **CISA advisories, Known Exploited Vulnerabilities, Cross-Sector CPGs, Secure by Design** | CISA | Defensive advisories and prioritisation | PD | KEV JSON (✓ updated 2026-10-08) + git (✓ `cisagov/kev-data` 3.8 MB) + index | ≈ 2 GB | T1 | 4.2 | R |
| **MITRE ATT&CK (STIX), CWE, CAPEC** | MITRE (federally funded) | Defender knowledge bases: adversary behaviour, weakness and attack-pattern taxonomies | MITRE terms of use: royalty-free, reproduction with attribution | git ✓ (attack-stix-data 129 MB) + CWE/CAPEC XML downloads | ≈ 0.3 GB | T1 | 4.2 | R. Taxonomies, not tooling (§3.2) |
| **NVD CVE API 2.0** | NIST | Vulnerability catalogue with CVSS and CPE | PD (NVD); CVE records under CVE terms of use | API ✓ (✓ 404,126 CVEs) | ≈ 2 GB JSON | T2 | 4.0 | R |
| **NSA cybersecurity advisories and technical guidance** | NSA | Hardening guides, advisories | PD | index page (`media.defense.gov` PDFs) | ≈ 1 GB | T2 | 4.0 | W |
| **UK NCSC guidance** | NCSC (GCHQ) | Cyber security guidance for organisations and individuals | Crown copyright; OGL for most content (to verify; copyright page moved) | index page | ≈ 0.5 GB | T2 | 3.9 | R |
| **OWASP Cheat Sheet Series + ASVS** | OWASP Foundation | Secure development guidance | CC BY-SA 4.0 | git ✓ | ✓ 2.8 GB repo (mostly history) | T2 | 3.9 | R. Mirror the default branch only |
| **ENISA publications** | EU Agency for Cybersecurity | Threat landscape, good practice | © ENISA, reproduction authorised with acknowledgement | index page | ≈ 5 GB | T2 | 3.8 | R |
| CIS Benchmarks | Center for Internet Security | Configuration benchmarks | CC BY-NC-SA 4.0 behind registration | — | — | T3 | 3.4 | Registration terms to review; STIGs cover the same ground |

**Count**: 104 rows (11 + 5 + 11 + 5 + 11 + 14 + 6 + 14 + 8 + 8 + 11). Of these, **81 are T1/T2 candidates**, 19 are T3 (permission, licence check or budget), 2 are excluded (X), and 2 are not new entries (one retired upstream, one already held).

---

## 5. Tracking design

### 5.1 Which adapters fit

| Adapter | Status | Sources here |
|---|---|---|
| `git-mirror` | planned (PLAN §6.2) | FRUS, NIST OSCAL content, MITRE ATT&CK STIX, CISA `kev-data`, OWASP Cheat Sheets |
| `oai-pmh` | planned (PLAN §6.2) | NPS Calhoun, WHO IRIS, FAO, Army War College Press, Naval War College, CDC Stacks |
| `http-files` | planned | NOAA ENC bulk, FoodData Central, NVD (paged API into dated JSON files), CWE/CAPEC XML |
| `doc-collection` | **new, specified below** | everything published as an index page of PDFs: APD, JEL, MCPEL, FEMA, CISA, NGA, FAA, USCG, CIA CSI, NEETS, Borden, JTS, NCVEC, USACE… and API-backed document sets (OSTI, NTRS, USGS Pubs Warehouse, GovInfo, archive.org) whose records are documents |

OAI-PMH repositories also need the document-level filter, so the `oai-pmh` adapter emits candidate documents into the same filter (§5.3) instead of fetching every bitstream.

### 5.2 `doc-collection` adapter specification

A **curated document set**: one registry source made of many independently published documents, each with its own identity, edition and distribution marking. It implements the standard port (PLAN §6.1).

**Registry configuration** (`access`, all keys optional unless marked):

```yaml
access:
  adapter: doc-collection
  issuer: Department of the Army           # required; written into every document record
  discovery:                                # one or more discoverers, results are unioned
    - kind: index-page                      # index-page | api | oai-pmh | archive-org | list
      url: https://armypubs.army.mil/ProductMaps/PubForm/FM.aspx
      item_selector: "a[href*='Details.aspx']"   # CSS selector for document links
      detail:                               # optional second hop to a detail page
        pdf_selector: "a[href$='.pdf']"
        fields: {pub_number: "Pub/Form Number", date: "Pub/Form Date", title: "Pub/Form Title", status: "Pub/Form Status"}
    - kind: list                            # fixed list for one-off documents (NEETS module numbers, DTIC accessions)
      items: [ {pub_number: "NAVEDTRA 14173", urls: ["…", "…"]} ]
  origins: ["armypubs.army.mil"]            # allowed hosts for document bytes (HTTPS only)
  filter:
    distribution: {require: [A], read_from: pdf-front-matter}   # A | public-release | license:<SPDX>
    allow_series: ["ADP", "FM 3-0", "FM 5-0", "FM 6-0", "FM 6-22", "ATP 3-39.32", "ATP 3-37.2", "TC 3-25.26", "TC 4-02.1", "TC 4-02.3"]
    allow_series_prefix: ["ADP ", "FM 4-", "ATP 4-02", "ATP 3-34"]
    deny_series_prefix: ["FM 3-09", "ATP 3-09", "TC 3-09", "ATP 3-60", "TM 9-", "TM 43-", "ATP 3-11.", "ATP 3-34.210", "TC 3-22.", "TC 3-23."]
    deny_keywords: ["demolition", "munition", "explosive ordnance", "targeting", "fire support", "weapon system", "small arms", "marksmanship", "sniper", "chemical agent production", "improvised"]
    review_keywords: ["electronic warfare", "chemical", "biological", "radiological", "nuclear", "surveillance", "cyberspace operations", "information operations"]
    default: review                         # what happens to documents no rule matches: review | deny
  keep_superseded: true
  rate: {requests_per_second: 1}
```

The deny lists above are illustrative: the real lists live in `registry/filters/<source>.yaml` (reviewed in PRs like the registry) so they can be shared between sources and diffed.

**Per-document metadata** (one record per document version, stored as `meta/documents.jsonl` in the generation, and carried into each `DocRecord.meta`):

| Field | Example | Source |
|---|---|---|
| `doc_id` | `army:FM-5-0:2022-11-04` | issuer prefix + normalised publication number + edition date |
| `issuer` | Department of the Army | registry |
| `pub_number` | FM 5-0 | index or detail page |
| `title` | Planning and Orders Production | index page / PDF metadata |
| `edition_date` | 2022-11-04 | index or front matter |
| `distribution` | `A` / `public-release` / `license:CC-BY-NC-SA-3.0-IGO` / `PD` | **PDF front matter text** (first 4 pages), else repository rights field |
| `distribution_text` | "DISTRIBUTION RESTRICTION: Approved for public release; distribution is unlimited." | verbatim, for audit |
| `currency` | current / superseded / historical / rescinded | §2.2 |
| `supersedes` / `superseded_by` | `army:FM-5-0:2014-...` | detail page, front-matter "This publication supersedes…", or inferred from a newer edition with the same number |
| `filter_decision` | allow:series=FM 5-0 / deny:keyword=targeting / review:keyword=chemical | §5.3 |
| `origin_url`, `fetched_at`, `http` (ETag, Last-Modified, content-length), `tls_host` | provenance | fetcher |
| `sha256`, `blake3`, `bytes` | checksum | vault (upstream rarely publishes checksums) |
| `mirror_sha256` | for documents fetched from more than one copy (NEETS) | cross-check |

**Port methods**:

- `discover`: runs the discoverers, builds the candidate list, applies the **metadata-level** filter (series allow/deny, title keywords) and returns one `ReleaseRef` per cycle whose `version` is the cycle date and whose `meta` holds the candidate list hash. Documents that need review are written as proposals (§5.5) and left out of the release until approved.
- `plan`: diffs the candidate list against the previous generation by `(pub_number, edition_date)` and by `ETag`/`Last-Modified`/size; only new or changed documents are fetched.
- `fetch`: HTTPS only, from `origins` only; resumable; politeness limit per host.
- `verify`: (1) the file is a well-formed PDF/EPUB/HTML; (2) the **distribution gate** reads the front matter text and requires the configured marking; a document carrying B–F, FOUO, CUI, "export controlled", "ITAR", "EAR" or "not for public release" text is **rejected** even if the index listed it, and an alert is raised (an index that lists a restricted document is itself news); (3) the **content-level** keyword deny/review pass over the first pages (title page, table of contents, preface); (4) mirror cross-check when more than one copy is configured.
- `documents`: text via `pypdfium2` (as the arXiv adapter), one `DocRecord` per document; `native_id = doc_id`; `revision = edition_date`; the Navigator banner fields (`currency`, `superseded_by`) travel in `meta`.

**Superseded editions as history**: when a new edition of the same `pub_number` appears, the new document is added as `current`, the old one is re-labelled `superseded` with `superseded_by`, and both stay in the vault. The snapshot diff (adapters doc) records it as a `modified` change of the publication, so the net-diff segment keeps the edition history. A document that disappears from the index with no successor is labelled `rescinded` only if the issuer says so (rescission notice); otherwise `withdrawn-from-index` and kept.

### 5.3 Filtering order

```
index/API/OAI record
  → origin and HTTPS check
  → series allow/deny (metadata)          deny → dropped, logged
  → title keyword deny/review (metadata)  review → proposal
  → fetch
  → distribution gate (front matter)      not public → rejected, alert
  → content keyword deny/review           review → quarantined, proposal
  → vault
```

Rules are applied deny-first: a `deny_series_prefix` beats an `allow_series_prefix`. `default: review` means a new series the lists have never seen is held, not fetched into the library. Every decision is written to `filter_decision`, so the owner can audit why any document is or is not in the library.

### 5.4 Cadence, integrity and provenance

- **Cadence**: quarterly (`update.cadence: P3M`) for doctrine, guidance and standards sets; discovery probes still run with the daily health monitor so a new edition of a high-value document (NIST SP 800-53, FM 5-0, NRF) can raise a "new edition available" proposal between cycles. Git and OAI sources follow the same quarterly cadence with incremental harvests (`from=` the last harvest date).
- **Integrity**: HTTPS only, from the `origins` allow-list (official `.gov`/`.mil`/issuer domains; mirrors only where configured as cross-check sources). Upstream checksums are rare, so the vault's SHA-256/BLAKE3 at first fetch becomes the reference; a later fetch of the *same* edition with different bytes is flagged (silent re-issue or tampering) and kept as a separate version, not overwritten.
- **Provenance**: per document, the origin URL, the full redirect chain, response headers, fetch time, TLS host and the verbatim distribution text are kept in `meta/documents.jsonl` and in the ledger. That is the evidence that each document was lawfully public when fetched.
- **Index pages** are themselves snapshotted (`meta/index/<host>/<path>.html`) each cycle, so the library can show what the issuer listed at the time.

### 5.5 Owner-review workflow for borderline items

Borderline documents use the existing curator proposals (`wsp proposals`), one proposal per document batch:

```sh
wsp proposals list --status open            # e.g. "army-pubs-public: 7 documents need review"
wsp proposals show 41                       # each document: title, pub number, distribution text,
                                            # matched rule (review:keyword=chemical), first-page excerpt
wsp proposals accept 41 --note "CBRN medical management, in scope"
wsp proposals reject 42 --note "weapons employment, out of scope"
```

- A proposal carries a drafted change to `registry/filters/<source>.yaml`: accepting adds the publication numbers to `allow_documents`; rejecting adds them to `deny_documents`, so the decision is remembered and the document is never proposed again.
- The gate never auto-promotes from these collections: the PLAN §5.4 auto-promote rule does not apply to `doc-collection` review proposals.
- Quarantined bytes (fetched, then held by the content pass) live outside the library tree in `_quarantine/<source>/` and are deleted on rejection.

### 5.6 Access to WAF-blocked origins

Most `.mil` sites (and `fema.gov`, `cdc.gov`, `gao.gov`, `congress.gov`) answered **403** to a plain client from the research host, including to the ordinary `Whisipedia/0.1` User-Agent, while the same documents are public. Options, in order of preference:

1. an official API or bulk path where one exists (GovInfo for GAO/CRS, CDC Stacks OAI if the block is lifted, DTIC's documented interfaces);
2. the same document from another official host (a DTIC copy of a NEETS module; a GovInfo copy of a FEMA document) with the provenance recorded;
3. owner-run fetches from a normal browser session into `_incoming/<source>/<cycle>/`, followed by `wsp ingest` (as already done for bulk first downloads in the add-a-source runbook);
4. **not** User-Agent spoofing or WAF evasion: the adapter identifies itself honestly.

---

## 6. Recommended first ingestion (top 15)

Chosen for authority × offline value per byte, with live endpoints that a generic client can reach today:

1. NIST CSRC publications + OSCAL content
2. FRUS (git)
3. Bowditch + NGA nautical publications
4. Joint Trauma System CPGs
5. CIA CSI (*Studies in Intelligence*, Heuer, monographs)
6. FEMA Independent Study (NDEMU) course materials
7. UK Cabinet Office resilience guidance (GOV.UK API, OGL)
8. NOAA ENCs + US Coast Pilot
9. FAA handbooks
10. USDA Complete Guide to Home Canning + Farmers' Bulletins (historical)
11. Army Publishing Directorate public doctrine (filtered)
12. CISA protective security + ISC public standards + NTAC
13. WHO IRIS WASH and clinical subsets (OAI-PMH)
14. NCVEC question pools + CISA SAFECOM field guides
15. DOE Fundamentals Handbooks + NEETS (once the access path in §5.6 is settled)

FEMA doctrine and continuity directives, JEL and MCPEL are top-value too, but blocked to a plain client today (§5.6); they lead the second wave.

---

## 7. Open questions for the owner

1. **Access to WAF-blocked `.mil`/`.gov` origins** (§5.6): accept owner-run browser fetches into `_incoming/` for those sources, or wait for API routes?
2. **Permission requests** worth making: Hesperian (digital format), Sphere, Swedish MCF, MSF, UK MOD (JDPs). Should the curator draft the letters?
3. **Historical material that is dangerous if followed** (old pesticide, canning and radiation advice): keep in default search with a banner (current design) or hide from default search entirely?
4. **Borderline technical references** such as NAWCWD TP 8347 (public release, but written for EW engineers): include as fundamentals after review, or keep out?
5. **Budget for the large optional sets**: CIA reading room (≈ 5–10 TB), USGS historical topo maps (≈ 10–20 TB), full NPS Calhoun and DTIC selections.
6. **Non-English civil-protection material** (Swedish, Swiss, Nordic): add languages beyond English for these collections?
