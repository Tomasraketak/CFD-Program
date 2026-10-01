# Studie krytu SPS30 — podrobný návod od nuly

Návod provede celou studií krytu prachového senzoru **SPS30** na jedoucí
platformě: od odhadu za pár sekund, přes CFD výpočet vzduchu a kapek v tomto
programu (SU2), až po stejný výpočet v **Ansys Student** (Fluent s modelem
diskrétní fáze, Workbench, DesignXplorer). Je psaný pro člověka, který CFD
nikdy nedělal a Ansys skoro nezná.

Obecné pojmy (síť, okrajová podmínka, konvergence, design point, response
surface, Monte Carlo…) vysvětluje slovníček ve [Studii radiačního štítu,
kapitola 2](SHIELD_STUDY_GUIDE.md#2-slovníček) — tady jsou jen pojmy navíc.

> **Upřímně na začátek.** Část o programu je ověřená na skutečném SU2. Část
> o Ansysu je napsaná podle dokumentace Fluentu, ale autor ji v Ansys Student
> klik po kliku neprošel — dialogy se mezi verzemi liší. Proto má každý krok
> **kontrolu** (kapitola 7.8), podle které poznáte, že je nastavený správně.

**Obsah**

1. [O co jde](#1-o-co-jde)
2. [Pojmy navíc](#2-pojmy-navíc)
3. [Plán práce](#3-plán-práce)
4. [Příprava: software a CAD krytu](#4-příprava-software-a-cad-krytu)
5. [Část A — odhad za pár sekund](#5-část-a--odhad-za-pár-sekund)
6. [Část B — CFD v tomto programu](#6-část-b--cfd-v-tomto-programu)
7. [Část C — Ansys Student krok za krokem](#7-část-c--ansys-student-krok-za-krokem)
8. [Část D — jak dostat co nejpřesnější data](#8-část-d--jak-dostat-co-nejpřesnější-data)
9. [Jak číst výsledky](#9-jak-číst-výsledky)
10. [Když něco nejde](#10-když-něco-nejde)
11. [Program × Fluent](#11-program--fluent)

---

## 1. O co jde

SPS30 měří prach tak, že si slabým ventilátorem nasává vzduch. Potřebuje:

- **pomalý vzduch u vstupu** (pod 1 m/s) — rychlý proud by měření zkreslil
  nebo by ventilátor „přetlačil“,
- **žádnou vodu** — kapka v senzoru ho zničí nebo zkreslí,
- **čerstvý vzduch** — aby neměřil zatuchlý vzduch v krytu.

![Vestavěný kryt SPS30 zvenku a v řezu](../images/sps30_housing_example.png)

*Vlevo: vestavěný kryt 120 × 70 × 80 mm zvenku s přicházejícím vzduchem
(zde 20 m/s, yaw 10°) a jednou ze dvou bočních štěrbin. Vpravo: podélný řez —
vzduch vejde štěrbinou, klesne plenem, musí se otočit přes přepážku (baffle)
a teprve pak se dostane do komory senzoru k sání SPS30 (zeleně); fialová
šipka je vlastní ventilátor SPS30, který vzduch nasává; voda, která se
dostane dovnitř, odteče odvodňovacím otvorem (weep hole). Stejný obrázek pro
váš vlastní STEP nakreslí záložka (**3D geometry**, tlačítko **Draw 3D
geometry**) i asistent.*

Kryt to řeší třemi triky:

1. **Statické štěrbiny po stranách** místo nasávání dopředu. Na boku jedoucí
   krabičky je jen statický tlak, ne „nápor“ větru. Dvě štěrbiny proti sobě
   se tlakově vyrovnávají — vzniká jen jemný průtok napříč, ne podtlak.
2. **Plenum (rozšíření)** hned za štěrbinami: vzduch se rozleje do velkého
   prostoru a zpomalí.
3. **Přepážka (baffle)** — vzduch ji obteče ostrou zatáčkou, ale těžké kapky
   zatáčku nevyberou, narazí do přepážky a stečou ven **odtokovým otvorem**
   (weep hole) ve dně.

Studie zjistí pro rychlosti **5–35 m/s**, boční vítr (**yaw ±20°**) a kapky
od mlhy (**10 µm**) po déšť (**2000 µm**):

- nejvyšší rychlost vzduchu u čela senzoru,
- podíl kapek, které doletí na čelo senzoru (cíl: nula),
- kolik vzduchu se v komoře senzoru vymění,
- a s jakou **pravděpodobností** jsou všechny cíle splněny (Monte Carlo).

> **Co čekat.** Velké kapky (stovky µm a víc) separace spolehlivě zastaví.
> **Jemná mlha** (10–20 µm) je ve zpomaleném vzduchu tak lehká, že proud
> poslušně následuje — setrvačnost ji nezastaví. Pokud studie ukáže průnik
> mlhy, není to chyba výpočtu, ale fyzika; řešením je hydrofobní membrána,
> filtr nebo delší labyrint.

---

## 2. Pojmy navíc

| Pojem | Co to znamená |
|---|---|
| **Statický tlak** | Tlak vzduchu „do boku“; na boční stěně jedoucího tělesa nezávisí na náporu větru. |
| **Nápor (ram / dynamický tlak)** | Tlak, který vzniká, když vzduch naráží čelně (½ρV²). Otvor dopředu ho „chytá“. |
| **Yaw (stočení)** | Úhel, pod kterým vzduch přichází z boku (boční vítr). Při yaw jedna štěrbina „vidí“ vítr víc než druhá. |
| **DPM** | *Discrete Phase Model* ve Fluentu — sleduje jednotlivé kapky v proudu vzduchu. |
| **Trap** | Okrajová podmínka pro kapky: kapka se na stěně zastaví (a spočítá). |
| **Escape** | Kapka opustí výpočet (vtok, výtok). |
| **Random walk (DRW)** | Náhodné „postrkování“ kapek turbulentními víry — malé kapky se tak rozptylují. |
| **Stokesovo číslo** | Poměr setrvačnosti kapky k tomu, jak rychle ji proud stáčí. Malé → kapka letí se vzduchem; velké → letí rovně a narazí. Hranice pro odlučovač je kolem 0,6. |
| **Penetration (průnik)** | Podíl kapek, které se dostaly do krytu a doletěly na čelo senzoru. |
| **Exchange flow (výměna)** | Kolik vzduchu za minutu projde komorou senzoru (L/min). |

---

## 3. Plán práce

```
A  Odhad v programu (zjednodušený model) ... 1 minuta
B  CFD v programu (SU2 + kapky) ............ 1–2 dny (počítá bez obsluhy)
C  Ansys Student (Fluent + DPM) ............ 3–5 dní práce + výpočet
D  Porovnání a Monte Carlo ................. 1–2 hodiny
```

Doporučené pořadí: A → B (nejdřív test sítě na jednom bodě) → C na stejných
bodech → D porovnání v programu.

---

## 4. Příprava: software a CAD krytu

- **Program** a **SU2 + MS-MPI**: [Tutoriál](TUTORIAL.md), kapitola 1
  (`Setup.bat`); kontrola v menu **Help → Check environment**.
- **Ansys Student**: viz [Studie radiačního štítu, 4.2](SHIELD_STUDY_GUIDE.md#42-ansys-student-jen-pro-část-c)
  (instalace, limit ~1 milionu buněk).

**CAD krytu** (nebo nechte pole prázdné = vestavěný kryt):

- STEP, jedno **těleso** (solid), ve kterém jsou **vyříznuté** všechny vnitřní
  prostory: štěrbiny, plenum, průchod kolem přepážky, komora senzoru a
  odtokový otvor. Vzduch se v programu počítá všude, kde není materiál.
- **Čelo senzoru musí být samostatná rovná plocha** (např. čelo malého
  kvádru, který představuje sací hrdlo SPS30). Program ji najde podle
  středu, směru a velikosti, které zadáte.
- Bez šroubků, textů a zaoblení pod ~0,5 mm.
- Zapište si: kterým směrem platforma v CAD jede (např. −x), střed čela
  senzoru, kam čelo hledí (do komory) a jeho rozměr.

Vestavěný kryt: 120 × 70 × 80 mm, jede směrem −x, štěrbiny 20 × 3 mm po
stranách u přídě, přepážka 58–61 mm od přídě (nahoře mezera 15 mm), čelo
senzoru 20 × 20 mm se středem (107; 0; 30) mm hledící na −x, odtokový otvor
4 mm ve dně.

---

## 5. Část A — odhad za pár sekund

1. Spusťte program → záložka **Sensor Microclimate (BMP580)** → podzáložka
   **SPS30 housing study**.
2. **Housing and sensor**: *Housing STEP* (prázdné = vestavěný), *Travel
   direction* (kam platforma jede), *Sensor face centre / looks / size*,
   *Chamber plane x* (rovina přes komoru senzoru, na které se měří výměna),
   *Model the SPS30 fan* a *Fan flow* (průtok ventilátoru — výchozích 0,3 L/min
   je odhad; pokud znáte skutečný, zadejte ho).
3. **Tunnel, air and goals**: velikost tunelu v délkách krytu (výchozí stačí),
   teplota vzduchu, cíle: *face velocity below* 1 m/s, *penetration at most*
   0, *exchange flow at least* (0 = jen hlásit).
4. **Lumped model dimensions**: rozměry pro zjednodušený model — plocha jedné
   štěrbiny, její šířka, průřez plena a komory, mezera u přepážky. U
   vlastního krytu je přepište podle CAD.
5. **Uncertain inputs**: rychlost 5–35 m/s, yaw −20..20°, kapky 10–2000 µm
   (zaškrtnuté *Log* = každý řád velikostí stejně pravděpodobný).
6. **Run analytic study**.

Zjednodušený model je **orientační** — ukáže trendy (kdy roste rychlost u
čela, které kapky projdou), ne finální čísla.

### 5.1 Jeden vstup po pevných krocích (sweep)

**Sweep one input** projde jeden vstup (rychlost, yaw nebo velikost kapek)
od *From* do *To* po *Step*, ostatní nechá na výchozích hodnotách, a každý
bod spočítá přímo zjednodušeným modelem — graf a tabulka na záložce
**Sweep**, uloženo i jako `sweep.csv`. Asistentovi: *„SPS30 sweep, kapky
10–200 µm po 10“* (akce `sweep`). Takové tabulky nečtěte z response
surface — to je proložení, ne model.

---

## 6. Část B — CFD v tomto programu

### 6.1 Co program udělá

1. Otočí kryt tak, aby vítr foukal podél +x, a vloží ho do virtuálního
   tunelu (3 délky krytu před, 6 za, 3 do stran a nahoru/dolů).
2. Vytvoří síť vzduchu — venku i uvnitř krytu (štěrbiny, plenum, komora).
3. **SU2** spočte proudění s modelem turbulence **SST k-ω** (to zadání
   vyžaduje). Boční vítr (yaw) se zadá natočením vektoru rychlosti na vtoku;
   návětrná boční stěna tunelu je také vtok, závětrná výtok. Ventilátor SPS30
   je malé předepsané odsávání čelem senzoru.
4. Pak **sleduje kapky** zadaného průměru: vypustí je před krytem, počítá
   odpor vzduchu, gravitaci a náhodné turbulentní postrkování, a každou
   zastaví na stěně, na kterou narazí. Kapka na čele senzoru = selhání.
5. Změří rychlost 2 mm před čelem senzoru a výměnu vzduchu v komoře.

### 6.2 Nastavení (skupina *SU2 CFD + droplet tracking*)

| Pole | Doporučení |
|---|---|
| *Mesh* | začněte `coarse` (≈ 0,5 mil. buněk) |
| *Iterations* | 2000 |
| *MPI ranks* | počet fyzických jader |
| *Droplets per point* | 2000 (víc = přesnější průnik; viz 8.4) |
| *Discrete random walk* | zapnuto |
| *Points to solve now* | 1 pro test sítě, pak *all* |

### 6.3 Test sítě a výpočet

1. *Mesh* `coarse` → **Prepare CFD cases + Fluent package**.
2. *Points to solve now* = 1 → **Solve CFD design points (SU2 + droplets)**.
   Spočte se středový bod DP0 (20 m/s, 0°, ~140 µm). Zapište si rychlost u
   čela a výměnu.
3. Totéž s `medium`. Pokud se rychlost u čela změní o < 10 %, síť `coarse`
   stačí.
4. Vybranou studii → *Points to solve now* = all → Solve. 15 bodů je práce
   na noc (nebo spusťte `cfd/run_design_points.bat` ze složky studie).

### 6.4 Kontrola

- Log u každého bodu: `face … m/s, exchange … L/min, N of M droplets inside
  reached the sensor`.
- Složka `cfd/points/DPx/`: `result.json` (čísla včetně počtu kapek, které
  skončily na stěnách krytu, uletěly nebo zůstaly nevyřešené) a `flow.vtu`
  (celé pole, např. do ParaView).
- **M (kapky uvnitř)** by nemělo být 0 — jinak se do krytu nedostala žádná
  kapka a průnik nic neříká (u velkých kapek při 0° je to normální).

---

## 7. Část C — Ansys Student krok za krokem

### 7.0 Podklady z programu

Po **Prepare CFD cases + Fluent package** → **Open study folder** →
podsložka **`fluent/`**:

| Soubor | K čemu |
|---|---|
| `fluid_domain.step` | tunel se „vyříznutým“ krytem, už otočený (vítr +x, nahoru +z) |
| `housing_placed.step` | samotný kryt ve stejné poloze |
| `design_points.csv` | design pointy (otevřít v Excelu) |
| `README_FLUENT.md` | čísla pro vaši studii (souřadnice čela, průtok ventilátoru, rovina komory) |

### 7.1 Workbench a geometrie

1. Workbench → přetáhněte **Fluid Flow (Fluent)** do *Project Schematic* →
   uložte projekt (cesta bez mezer a diakritiky).
2. **Geometry → Import Geometry → Browse** → `fluid_domain.step`.
3. Dvojklik na Geometry: jedno těleso; kvádr tunelu; uvnitř dutina krytu.
   Změřte délku krytu (např. 120 mm). Zavřete.

### 7.2 Síť

V *Ansys Meshing* (dvojklik na **Mesh**), filtr výběru ploch (*Face*):

**Named Selections** (výběr → pravé tlačítko → *Create Named Selection*):

| Jméno | Plochy |
|---|---|
| `inlet` | stěna tunelu x = min (proti větru) |
| `outlet` | stěna x = max |
| `side_neg` | stěna y = min |
| `side_pos` | stěna y = max |
| `top`, `bottom` | stěny z = max, z = min |
| `sensor` | **jen** čelo senzoru (přibližte si komoru; pomůže *Hide* stěn krytu) |
| `housing` | všechny ostatní plochy krytu (Ctrl+A → Ctrl-klikem odeberte 6 stěn tunelu a čelo senzoru) |

**Nastavení sítě** (klik na *Mesh*, okno *Details*): *Physics Preference*
CFD, *Solver* Fluent, *Element Size* 30 mm, *Growth Rate* 1,15.

- **Sizing** na `housing` a `sensor`: 1,5 mm.
- **Sizing** na hrany štěrbin (filtr *Edge*): 0,75 mm (aby štěrbina měla
  aspoň 4 buňky na šířku).
- **Body of Influence** (volitelně): kvádr kolem krytu, 3 mm.
- **Inflation** na `housing`: *First Layer Thickness* 0,05 mm, 3–5 vrstev,
  růst 1,2.

**Generate Mesh** → *Statistics* → počet buněk musí být pod limitem
studentské verze; když ne, zvětšete velikosti (2 mm na krytu, 1 mm u
štěrbin, méně vrstev). Kvalita: *Skewness* max < 0,95, *Orthogonal
Quality* min > 0,1. Zavřete, ve Workbenchi **Mesh → Update**.

### 7.3 Fluent — obecné a modely

Dvojklik na **Setup** → *Double Precision* → Start.

- **General**: Pressure-Based, **Steady**. Gravitaci zapněte (Z = −9,81) —
  u kapek je důležitá. *Scale* → zkontrolujte rozměry tunelu.
- **Models → Viscous**: **k-omega (2 eqn)** → **SST** → OK.
- **Models → Discrete Phase**:
  - *Interaction with Continuous Phase*: vypnuto (kapek je málo, vzduch
    neovlivní),
  - *Tracking → Max. Number of Steps* 50 000, *Step Length Factor* 5,
  - *Physical Models*: nic navíc.
- **Materials**: vzduch — konstantní hustota (výchozí). Kapalina: z databáze
  (*Fluent Database*) přidejte **water-liquid**.

### 7.4 Okrajové podmínky

Nejdřív **vstupní parametry** jako *Named Expressions* (*Parameters &
Customization → Expressions → New*), u každého zaškrtněte **Use as Input
Parameter**:

```
speed            = 20 [m/s]
yaw              = 0 [deg]
droplet_diameter = 0.0001 [m]
```

| Zóna | Typ | Nastavení | DPM (záložka *DPM*) |
|---|---|---|---|
| `inlet`, `side_neg`, `side_pos` | velocity-inlet | *Specification Method*: **Components**; X = `speed*cos(yaw)`, Y = `speed*sin(yaw)`, Z = 0; turbulence 5 %, poměr 10 | escape |
| `outlet` | pressure-outlet | 0 Pa | escape |
| `top`, `bottom` | symmetry | — | reflect |
| `housing` | wall | no slip | **trap** |
| `sensor` | mass-flow-outlet (ventilátor) — hmotnostní průtok z `README_FLUENT.md` (např. 6e-6 kg/s); nebo wall, pokud ventilátor nemodelujete | **trap** |

> Boční stěny jako *velocity inlet* s celým vektorem větru: tam, kde vzduch
> ven vytéká, se chovají jako předepsaný výtok. Pro vzdálené stěny tunelu je
> to běžný a dostatečně přesný postup, který funguje pro kladný i záporný
> yaw bez přepínání typů.

Odtokový otvor ve dně krytu nepotřebuje vlastní podmínku — ústí do okolního
vzduchu, který má tlak 0 Pa (přesně to zadání chce).

### 7.5 Kapky (injekce)

1. **Results → Surfaces → Create → Plane**: *Point and Normal*, bod
   (x = 0,5 délky krytu před přídí, y = 0, z = střed krytu), normála
   (1, 0, 0) → jméno `droplet_plane`.
2. **Models → Discrete Phase → Injections → Create**:
   - *Injection Type*: **surface**, *Release From Surfaces*: `droplet_plane`,
   - *Material*: water-liquid,
   - *Diameter*: `droplet_diameter` (volba *expression*/parametr),
   - *Velocity*: X = `speed*cos(yaw)`, Y = `speed*sin(yaw)`, Z = 0,
   - *Total Flow Rate*: 1e-6 kg/s (při vypnuté interakci na čísle nezáleží),
   - záložka **Turbulent Dispersion**: zaškrtněte **Discrete Random Walk
     Model**, *Number of Tries* 10, *Time Scale Constant* 0,15.

> Rovina protíná celý tunel, takže většina kapek krytu mine. Nevadí — cílem
> je, aby na čelo senzoru nedopadla **žádná**.

### 7.6 Výstupy

**a) Rychlost u čela.** *Solution → Report Definitions → New → Surface Report
→ Facet Maximum* → *Velocity Magnitude* → surface `sensor` → zaškrtněte
**Create Output Parameter** → jméno `face_velocity`.

**b) Kapky na senzoru.** Po výpočtu *Results → Reports → Discrete Phase →
Summary* (nebo *Particle Fates*) ukáže, kolik kapek skončilo (*trapped*) na
zóně `sensor` a kolik bylo vypuštěno. Pokud vaše verze nabízí *Report
Definitions → New → DPM Report* s počtem zachycených částic na zóně, udělejte
z něj výstupní parametr `sensor_trap_count` (a `injected`). Jinak čísla po
každém design pointu přepište do CSV ručně (kapitola 7.9).

**c) Výměna vzduchu v komoře.**
1. *User-Defined → Field Functions → Custom*: `abs_ux` = **abs(X Velocity)**.
2. *Surfaces → Create → Iso-Surface*: *X-Coordinate* = rovina komory z
   `README_FLUENT.md` → `xcut`.
3. *Surfaces → Create → Iso-Clip*: z `xcut` podle *Y-Coordinate* v mezích
   krytu → `xcut_y`; pak z `xcut_y` podle *Z-Coordinate* → `chamber_plane`.
4. *Report Definitions → New → Surface Report → Integral* → `abs_ux` na
   `chamber_plane` → výstupní parametr `ux_integral` (m³/s; program ho při
   importu vydělí dvěma a převede na L/min).

### 7.7 Výpočet

- *Methods*: **Coupled**, *Pseudo Time Method*, vše **Second Order**.
- *Residuals*: 1e-4 (continuity, rychlosti, k, ω).
- *Convergence Conditions*: `face_velocity`, 1e-4, 100 předchozích hodnot.
- *Hybrid Initialization* → *Run Calculation* 2000 iterací.
- DPM se u ustáleného proudění dopočítá na konci (nebo každých N iterací
  podle nastavení *DPM Iteration Interval*); po doběhnutí se podívejte na
  *Particle Tracks* (Results → Graphics → Particle Tracks, obarvit podle
  průměru).

### 7.8 Kontroly před spuštěním všech bodů

| Kontrola | Jak | Co musí vyjít |
|---|---|---|
| Vítr jde správně | Contours → Velocity, rovina z = střed krytu | proud podél +x (při yaw šikmo), u krytu obtékání |
| Štěrbiny nejsou „lopatky“ | Vectors v rovině přes štěrbiny | při 0° jen malý průtok štěrbinami; při yaw vtéká návětrnou a vytéká závětrnou |
| Vzduch v krytu je pomalý | Contours → Velocity uvnitř | v plenu a komoře desetiny m/s, ne jednotky |
| Ventilátor táhne | Report → Fluxes → Mass Flow Rate na `sensor` | zadaný průtok |
| Kapky dělají, co mají | Particle Tracks | velké kapky letí rovně / končí na přepážce či stěnách; malé následují proud |
| Konvergence | residuály, `face_velocity` | ustálené |
| Shoda s programem | stejný bod jako DP0 z části B | stejný řád rychlosti u čela a výměny |

### 7.9 Všechny design pointy

1. Zavřete Fluent (uložit). Ve Workbenchi otevřete **Parameter Set**.
2. Do tabulky zkopírujte z `design_points.csv` sloupce `speed_ms` → `speed`,
   `yaw_deg` → `yaw`, `droplet_um` → `droplet_diameter` **převedené na
   metry** (÷ 1 000 000, např. 141,4 µm → 0,0001414 m).
3. **Update All Design Points**.
4. Tabulka → pravé tlačítko → **Export Table Data as CSV**. Pokud počty kapek
   nemáte jako parametr, doplňte v Excelu sloupce `sensor_trap_count` a
   `injected` ručně.

Program při importu rozpozná sloupce `speed`, `yaw`, `droplet_diameter`
(v m, mm i µm — podle jednotky v hlavičce), `face_velocity`,
`sensor_trap_count` + `injected` (nebo přímo `penetration`) a `ux_integral`
(nebo `exchange_flow_lpm`).

### 7.10 Volitelně DesignXplorer

Stejně jako u štítu ([7.10 tam](SHIELD_STUDY_GUIDE.md#710-volitelně-designxplorer-přímo-v-ansysu)):
*Response Surface* (CCD face-centred) → *Six Sigma Analysis* s rozděleními
vstupů. Průměr kapek dejte jako *Lognormal* nebo *Uniform* v rozsahu.

### 7.11 Import do programu

Podzáložka **SPS30 housing study** → formulář nastavte jako ve Fluentu →
**Import solved design points (CSV)…** → program proloží plochy a spustí
Monte Carlo.

---

## 8. Část D — jak dostat co nejpřesnější data

1. **Nezávislost na síti** — rychlost u čela a výměna se mezi dvěma sítěmi
   změní o < 10 %. Štěrbiny potřebují aspoň 4–6 buněk na šířku.
2. **Konvergence** — `face_velocity` ustálená; při 5 m/s a 0° může proud
   v krytu pomalu kolísat — pak průměr posledních iterací.
3. **Počet kapek** — cíl je *nula* kapek, takže rozhoduje statistika: když z
   2000 kapek uvnitř krytu žádná nedoletí, je průnik s 95% jistotou pod
   ~0,15 % (pravidlo 3/N). Pro přísnější důkaz zvyšte *Droplets per point*
   (program) nebo *Number of Tries*/hustotu injekce (Fluent) a hlavně
   **počítejte nejhorší případy přímo** (malé kapky, velký yaw).
4. **Nejhorší případy přepočítat** — hodnoty z *worst case* zadejte jako
   extra design point; response surface je v rozích rozsahu nejméně přesná,
   a u průniku kapek (skoro všude nula, někde prudce nenulový) obzvlášť.
   Pro průnik je proto lepší *Radial basis* a víc bodů (*Latin hypercube*
   30+) než polynom.
5. **Ventilátor** — průtok SPS30 ovlivní rychlost u čela i průnik malých
   kapek. Pokud ho můžete změřit (nebo najít v dokumentaci), zadejte ho.
6. **Porovnání program × Fluent** — shoda řádů rychlosti a výměny a shodný
   verdikt o kapkách (které velikosti projdou) = výsledku lze věřit.
7. **Validace** — v dešti/mlze na jedoucím voze a kontrola, zda se v krytu
   objevuje voda (indikační papírek u čela senzoru), a porovnání měření
   SPS30 v krytu s referenčním měřidlem.

**Co model nezahrnuje:** tříštění a odraz kapek od stěn (vše je „trap“),
stékání vodního filmu (voda z přepážky ve skutečnosti teče k odtoku —
pozor, aby film nemohl dotéct k senzoru), vypařování, nárazový déšť a
nestacionární poryvy.

---

## 9. Jak číst výsledky

| Karta | Význam |
|---|---|
| **Worst face velocity** | nejvyšší rychlost u čela ze všech náhodných podmínek (zelená = pod cílem) |
| **Worst penetration** | nejhorší podíl kapek uvnitř krytu, které doletí na čelo |
| **Lowest exchange flow** | nejmenší výměna vzduchu (typicky při 0° a nízké rychlosti) |
| **Reliability** | kolik % náhodných podmínek splní **všechny** cíle; pod kartami podíl pro každý cíl |

Pod kartami: nejhorší případ každého výstupu (rychlost, yaw, průměr kapky),
**nejhorší nevyhovující podmínka**, a poznámky (např. že jemná mlha prošla).
Histogram lze přepnout na každý výstup; červená čára je cíl.

Příklad: „Rychlost u čela je vždy pod 0,12 m/s (cíl 1 m/s splněn s
rezervou). Kapky nad 50 µm neprojdou nikdy; mlha 10–20 µm při malém yaw
prochází v jednotkách procent. Spolehlivost 48 % — všechny selhání jsou
jemná mlha → přidat membránu před senzor.“

---

## 10. Když něco nejde

| Problém | Řešení |
|---|---|
| *no housing face matches the sensor face* | zkontrolujte střed, směr a velikost čela senzoru (v CAD jednotkách převedených na metry); čelo musí být samostatná rovná plocha |
| *cutting the housing out left N air volumes* | v CAD je uzavřená dutina bez spojení ven — zkontrolujte, že štěrbiny opravdu prochází stěnou |
| meshing selže | jemnější síť, v CAD odstraňte drobné hrany |
| `0 of 0 droplets inside` | do krytu nevletěla žádná kapka — u velkých kapek normální; u malých zkontrolujte, že injekce míří na kryt (yaw) |
| Fluent: síť nad limitem | větší buňky na krytu (2 mm), 3 vrstvy inflation, menší tunel (2/4/2 délky) |
| Fluent: kapky se „zasekávají“ (*incomplete*) | zvyšte *Max. Number of Steps*; v pomalém plenu malé kapky dlouho krouží |
| Import: *lacks the column(s)* | přejmenujte sloupce na `speed`, `yaw`, `droplet_diameter`, … |

---

## 11. Program × Fluent

| | Program (SU2) | Ansys Fluent |
|---|---|---|
| Turbulence | SST k-ω | SST k-ω |
| Kapky | vlastní sledování v poli SU2: odpor, gravitace, náhodná procházka | DPM s Discrete Random Walk |
| Kapka na stěně | trap | trap (nastavíte) |
| Ventilátor SPS30 | předepsaná rychlost odsávání čelem | mass-flow outlet na `sensor` |
| Boční vítr | natočený vektor na vtoku, návětrná stěna vtok, závětrná výtok | natočený vektor na vtoku i obou bocích |
| Limit sítě | žádný | ~1 milion buněk |
| Design pointy | automaticky, přes noc skriptem | Parameter Set → Update All |
| Plochy + Monte Carlo | v programu | v programu (po importu) nebo DesignXplorer |

---

## Související

- [Uživatelská příručka](USER_GUIDE.md), kapitola 9 — všechna pole studie.
- [MCP a AI](MCP_AI_GUIDE.md), nástroj `sps30_housing_study`.
- [Studie radiačního štítu](SHIELD_STUDY_GUIDE.md) — obecný postup v Ansysu
  podrobněji (Workbench, síť, parametry, DesignXplorer).
