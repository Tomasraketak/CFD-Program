# AeroThermalStudio — Tutoriál

Návod krok za krokem, od instalace až po ovládání pomocí AI asistenta.
Napoprvé projděte lekce v pořadí; každá navazuje na předchozí.

> **Poznámka k terminologii.** Odborné pojmy uvádím česky s anglickým
> originálem v závorce, protože názvy nastavení v programu jsou anglicky.

**Obsah**

1. [Instalace](#1-instalace)
2. [Lekce 1 — První spuštění a ukázka](#lekce-1--první-spuštění-a-ukázka)
3. [Lekce 2 — První simulace rakety](#lekce-2--první-simulace-rakety)
4. [Lekce 3 — Moment na závěsu křidélka](#lekce-3--moment-na-závěsu-křidélka)
5. [Lekce 4 — Parametrické studie](#lekce-4--parametrické-studie)
6. [Lekce 5 — Tvorba obrázků](#lekce-5--tvorba-obrázků)
7. [Lekce 6 — Případ senzoru BMP580](#lekce-6--případ-senzoru-bmp580)
8. [Lekce 7 — Projekty a nastavení](#lekce-7--projekty-a-nastavení)
9. [Lekce 8 — Ovládání pomocí AI](#lekce-8--ovládání-pomocí-ai)
10. [Řešení problémů](#řešení-problémů)

---

## 1. Instalace

### Co potřebujete

| Požadavek | Poznámka |
|---|---|
| Windows 11 | Funguje i Windows 10; analytická část běží i na Linuxu a macOS |
| Python 3.11 nebo novější | Z [python.org](https://www.python.org/downloads/). **Zaškrtněte „Add Python to PATH"** |
| Microsoft MPI | Pro paralelní výpočty. [Ke stažení](https://www.microsoft.com/en-us/download/details.aspx?id=105289) — nainstalujte **oba** soubory: `msmpisetup.exe` i `msmpisdk.msi` |
| SU2 8.x | Řešič proudění. `Setup.bat` jej umí stáhnout, nebo jej nainstalujte sami |
| ~4 GB volného místa | Sítě a výsledky zaberou hodně |

Referenční stroj je procesor se 6 jádry / 12 vlákny a 16 GB RAM. Vše je na něj
naladěno: výchozích 10 MPI procesů, sítě dimenzované na výpočet 3–8 minut.

### Postup

1. Složku `CFD-Program` umístěte natrvalo — například do
   `C:\AeroThermalStudio`. Vyhněte se OneDrive a synchronizovaným složkám,
   výstupy simulací jsou velké a neustále se mění.
2. Dvakrát klikněte na **`Setup.bat`**. Zkontroluje počítač, nainstaluje
   Python balíčky a nabídne stažení SU2. Napoprvé to trvá několik minut.
3. Dvakrát klikněte na **`Check-Environment.bat`**. Chcete vidět:

   ```
   MPI launcher     : C:\Program Files\Microsoft MPI\Bin\mpiexec.exe
   SU2_CFD          : C:\Users\vy\AppData\Local\AeroThermalStudio\su2\bin\SU2_CFD.exe

     Solver stack ready.
   ```

Pokud u SU2 svítí `NOT FOUND`, nejde o fatální problém — tvorba sítě, analýza
senzoru i veškeré vykreslování fungují i bez něj. SU2 potřebujete jen pro
samotný výpočet proudění. Viz [Řešení problémů](#řešení-problémů).

> **Ke stahování SU2.** `Setup.bat` odmítne nainstalovat archiv, ke kterému
> nemá uložený kontrolní součet SHA-256, a místo toho vypíše ruční postup. Je
> to záměr: stažený soubor se na vašem počítači bude spouštět, takže
> instalovat jej neověřený není pohodlí, které stojí za to. Stáhněte si SU2
> sami ze [stránky vydání](https://github.com/su2code/SU2/releases),
> rozbalte jej a buď umístěte do
> `%LOCALAPPDATA%\AeroThermalStudio\su2\bin`, nebo nastavte proměnnou
> prostředí `SU2_RUN` na složku se souborem `SU2_CFD.exe`.

### Spouštěče

| Soubor | Co dělá |
|---|---|
| `AeroThermalStudio.bat` | Spustí program. Tento používáte denně |
| `Setup.bat` | První instalace |
| `Check-Environment.bat` | Vypíše, co je nainstalováno |
| `Run-Demo.bat` | Vytvoří a vysíťuje ukázkovou raketu — bez řešiče |
| `Run-MCP-Server.bat` | Server pro AI (obvykle jej spouští AI klient) |
| `Run-Tests.bat` | Spustí testy |

---

## Lekce 1 — První spuštění a ukázka

**Cíl:** ověřit, že instalace funguje, ještě než investujete čas do vlastního
modelu.

Dvakrát klikněte na **`Run-Demo.bat`**. Program vytvoří v CAD raketu
s křidélky, natočí ji, vytáhne mezní vrstvu (*boundary layer*) a vysíťuje
výpočetní oblast. Řešič se nepoužívá, takže to funguje i před instalací SU2.

Po zhruba minutě uvidíte něco takového:

```
Mesh report
----------------------------------------------
  Cells            : 252,303
  Nodes            : 109,810
  Target band      : 250,000 - 400,000
  Within band      : True
  Reference length : 0.9954 m
  Reference diam.  : 0.0800 m
  Achieved y+      : 45.0
  Minimum quality  : 0.380
  Markers          : {'WALL_ROCKET': 18030, 'WALL_FINS': 11686, 'FARFIELD': 406}
```

**Jak to číst:**

- **Cells / Within band** — generátor sítě si sám upravil velikost buněk, aby
  se jejich počet vešel do cílového rozsahu. Ten je zvolen tak, aby výpočet
  trval 3–8 minut na referenčním stroji.
- **Achieved y+ 45.0** — y⁺ je bezrozměrná vzdálenost první buňky od stěny.
  Pásmo 30–60 je oblast, kde platí stěnové funkce (*wall functions*). Správné
  y⁺ je to, co dělá přesné řešení cenově dostupným.
- **Minimum quality** — nejhorší buňka v síti jako podíl dokonale tvarované.
  Nad ~0,1 je v pořádku; 0,38 je zdravé.
- **Markers** — pojmenované plochy, na které řešič aplikuje okrajové podmínky.
  `WALL_ROCKET` je trup, `WALL_FINS` křidélka, `FARFIELD` vnější hranice.

Pokud jste se dostali sem, instalace je v pořádku.

---

## Lekce 2 — První simulace rakety

**Cíl:** z CAD souboru získat hodnoty odporu a vztlaku.

### 2.1 Spusťte program

Dvakrát klikněte na **`AeroThermalStudio.bat`**. Otevře se tmavé okno se dvěma
záložkami. Zůstaňte na **Aerodynamics & Fins**.

### 2.2 Získejte model

Nemáte-li po ruce CAD, použijte **File → Create sample rocket CAD**. Vytvoří
referenční raketu a rovnou doplní cestu.

Jinak přetáhněte svůj soubor `.step` nebo `.stp` do okna, případně použijte
**Browse**.

> **Váš CAD musí být objemové těleso** (*solid*), ne sada ploch. Pokud váš
> modelář vytvořil plošný model, před exportem jej sešijte do tělesa. Program
> vám jasně řekne, když žádné těleso nenajde.

### 2.3 Určete, kudy je „dopředu"

Tento krok se nejčastěji plete a závisí na něm všechno ostatní.

Program potřebuje vědět, kterým směrem míří **špička** *v souřadném systému
vašeho CAD souboru*. Klikněte na odpovídající tlačítko: `+X`, `-X`, `+Y`,
`-Y`, `+Z` nebo `-Z`.

Není-li model zarovnán s osou, zaškrtněte **Use custom vector** a zadejte
směr, například `0.0, 0.3, 0.95`. Vektor se normalizuje sám, takže na
velikosti nezáleží — jen na směru.

**Reference origin** je bod, který se přesune do počátku větrného tunelu.
Použijte špičku. Všechno, co program následně hlásí — zejména působiště
aerodynamické síly — se měří odsud, takže rozumná volba usnadní interpretaci.

**Scale to metres** převádí jednotky CAD. Modelovali jste v milimetrech?
Nastavte `0.001`. Chyba zde udělá těleso tisíckrát větší a Reynoldsovo číslo
nesmyslné.

### 2.4 Nastavte výpočetní oblast

Tři posuvníky určují, kolik vzduchu kolem tělesa se počítá, v násobcích délky
tělesa:

| Posuvník | Výchozí | Význam |
|---|---|---|
| Upstream | 5 L | Vzdálenost od špičky ke vstupu |
| Downstream | 10 L | Vzdálenost od dna k výstupu |
| Radial | 5 L | Vzdálenost od osy k vnější hranici |

Výchozí hodnoty jsou pro většinu práce vhodné. Downstream je větší, protože
úplav (*wake*) potřebuje prostor, než dorazí k hranici. Oblast zvětšete, pokud
vidíte rušení řešení u okrajů; zmenšujte jen když šetříte buňky a víte, že se
proudění chová klidně.

Volte **cylinder** pro rakety (u štíhlého tělesa plýtvá méně buňkami) a
**box** pro krabicové případy.

### 2.5 Nastavte letový režim

- **Speed as** — Machovo číslo, nebo pravá vzdušná rychlost v m/s.
- **Speed** — pro nadzvukovou raketu zkuste 2,0.
- **Angle of attack** — úhel náběhu, o kolik je špička sklopena vůči proudu,
  ve stupních. Rozsah ±20°.
- **Sideslip** — totéž ve vybočení.
- **Altitude** — výška nastaví tlak, teplotu a hustotu ze standardní
  atmosféry.

Pod posuvníky je živý výpis:

```
M 2.000  |  V 680.6 m/s  |  p 101.3 kPa  |  T 288.2 K
```

Než budete pokračovat, zkontrolujte, že vypadá správně. Je to nejrychlejší
způsob, jak odhalit chybu v jednotkách.

### 2.6 Zvolte síť

| Nastavení | Doporučení |
|---|---|
| **Resolution** | Začněte `coarse`. Na `medium` nebo `fine` přejděte, až bude nastavení správné |
| **Prism layers** | 7 je dobrá výchozí hodnota (rozsah 5–8) |
| **Target y+** | Nechte 45, pokud nevíte, proč chcete jinak |
| **MPI ranks** | O dvě méně, než máte vláken. Na 12vláknovém CPU tedy 10 |

### 2.7 Vytvořte síť

Klikněte na **Generate mesh**. V logu běží postup:

```
meshing attempt 1 (size scale 1.000)
classified 4 airframe and 17 fin faces (airframe radius 40.0 mm)
generating surface mesh
extruding prism layers from 22136 wall triangles
generating tetrahedral farfield mesh
Mesh ready: 190,070 cells, y+ 45.0, quality 0.380
```

**Zkontrolujte řádek s klasifikací.** Říká, kolik ploch bylo označeno jako
trup a kolik jako křidélka. Rozdělení se určuje podle toho, jak daleko od osy
každá plocha sahá. Má-li váš model neobvyklý tvar a rozdělení vypadá špatně,
výsledky jsou přesto platné — ovlivněno je jen vykazování sil po plochách.

Pokud se počet buněk netrefí do cílového rozsahu, program automaticky
přesíťuje s upravenou velikostí buněk, až čtyřikrát.

### 2.8 Spusťte výpočet

Klikněte na **Run simulation**. Graf reziduí se plní, jak řešič běží.

**Jak číst graf:** `RMS[Rho]` je reziduum hustoty v logaritmickém měřítku.
Mělo by soustavně klesat. Pokles o pět řádů (třeba z −1 na −6) znamená dobře
zkonvergováno. `C_d` a `C_l` by se měly ustálit — to je obvykle lepší
ukazatel, protože síly se často ustálí dřív, než se dosáhne cílového rezidua,
a program zastaví na kterékoli z podmínek.

Po dokončení se naplní výsledkové karty:

| Karta | Význam |
|---|---|
| **Drag coefficient (C_d)** | Bezrozměrný odpor. U štíhlé rakety při Mach 2 čekejte zhruba 0,3–0,6 |
| **Lift coefficient (C_l)** | Nulový při nulovém úhlu náběhu na symetrickém tělese |
| **Drag force / Lift force** | Totéž v newtonech při vašem letovém režimu |
| **Hinge torque** | Viz lekce 3 |
| **Centre of pressure** | Osová poloha, kde aerodynamická síla efektivně působí |

> **Působiště ukazuje `--`?** Při nulovém úhlu náběhu je to správně. Bez
> příčné síly není působiště definováno a program to řekne, místo aby si číslo
> vymyslel. Nastavte úhel náběhu 2–5° a spusťte znovu.

### 2.9 Ověřte si výsledek

Než jakémukoli CFD výsledku uvěříte:

1. **Sedí řád veličiny?** Porovnejte s ručním výpočtem nebo publikovanými daty
   pro podobný tvar.
2. **Zkonvergovalo to?** Log to říká výslovně.
3. **Mění se to se sítí?** Spusťte znovu s rozlišením `medium`. Pokud se C_d
   posune o více než pár procent, síť není dost jemná. Tomu se říká studie
   nezávislosti na síti a u vážné práce není volitelná.

---

## Lekce 3 — Moment na závěsu křidélka

**Cíl:** navrhnout servo, které udrží řídicí křidélko.

To je otázka, kterou konstruktér řízení skutečně potřebuje: *jak silně tlačí
vzduch na mé křidélko kolem osy, na které se otáčí?*

### 3.1 Definujte závěs

V panelu **Fin hinge axis**:

- **Name** — označení, např. `fin_pitch`.
- **Point** — libovolný bod na ose závěsu, v metrech, v zarovnané soustavě
  (špička v počátku, osa tělesa podél +X). Pro křidélko 0,9 m od špičky,
  uchycené na poloměru trupu 40 mm: `0.9, 0.04, 0.0`.
- **Direction** — osa, kolem které se křidélko otáčí. Křidélko ve vodorovné
  rovině, které se natáčí kvůli klopení, má směr `0, 1, 0`.

Klikněte na **Show axis in 3D** a uvidíte čáru vykreslenou ve výřezu. Ověřit
si to vizuálně je mnohem snazší než kontrolovat čísla.

### 3.2 Co se počítá

Program spočítá celkový aerodynamický moment, přenese jej do vašeho bodu
závěsu a promítne do vašeho směru závěsu. Výsledkem je jediné číslo
v newtonmetrech: moment, který musí servo udržet.

Dvě vlastnosti, které stojí za to znát — obě ověřené testy:

- Síla působící přesně **skrz** osu závěsu dává **nulový** moment, bez ohledu
  na to, kde leží referenční bod řešiče.
- Otočení směrového vektoru otočí znaménko. Pro návrh serva je rozhodující
  velikost; znaménko říká, kterým směrem to tlačí.

### 3.3 Návrh serva

Jedna simulace dá moment v jednom letovém režimu. Potřebujete **nejhorší
případ** přes celou obálku — o tom je lekce 4.

Praktické pravidlo: volte servo dimenzované alespoň na dvojnásobek
maximálního aerodynamického momentu, aby pokrylo dynamický překmit, tření a
výrobní odchylky.

---

## Lekce 4 — Parametrické studie

**Cíl:** získat křivku místo jednoho bodu.

### 4.1 Nastavení

Ve spodní části ovládacího panelu:

- **Sweep parameter** — `mach`, `aoa`, `sideslip` nebo `altitude`.
- **Sweep values** — seznam oddělený čárkami, např.
  `0.5, 1.0, 1.5, 2.0, 2.5, 3.0`.

Vše ostatní zůstává podle ostatních ovládacích prvků. Takže pro poláru odporu
při Mach 2 nastavte rychlost 2,0 a prohledávejte `aoa` přes
`-10, -5, 0, 5, 10`.

### 4.2 Spuštění

Klikněte na **Batch sweep**. Body běží jeden po druhém — záměrně, protože
16 GB RAM neudrží dvě velká řešení najednou.

Postup se hlásí po bodech:

```
point 1/6: mach=0.5 (ok)
point 2/6: mach=1 (ok)
point 3/6: mach=1.5 (ok)
```

Bod, který nezkonverguje, se zaznamená a studie pokračuje. Jeden špatný režim
nezahodí celou drahou dávku.

### 4.3 Výsledek

Log hlásí extrémní hodnoty, mimo jiné:

```
peak hinge torque 0.0840 N m
```

To je číslo, podle kterého se navrhuje servo — nejhorší případ napříč vším,
co jste prohledali.

### 4.4 Praktický postup

Pro skutečný návrh křidélka:

1. Prohledejte Machovo číslo při svém maximálním očekávaném úhlu náběhu.
   Moment obvykle vrcholí poblíž nejvyššího dynamického tlaku, ne nejvyššího
   Mach.
2. Při nejhorším Mach prohledejte úhel náběhu a najděte skutečný vrchol.
3. Přidejte 100 % rezervy a zvolte servo.

---

## Lekce 5 — Tvorba obrázků

**Cíl:** pochopit proudění a vytvořit obrázky do zprávy.

K dispozici jsou čtyři druhy obrázků:

| Režim | Ukazuje | K čemu |
|---|---|---|
| `surface_pressure` | Tlak nebo C_p na tělese | Kde působí zatížení |
| `mach_slice` | Machovo číslo v rovině řezu | Rázové vlny a expanzní vějíře |
| `streamlines` | Proudnice | Odtržení a nasávání do senzoru |
| `thermal` | Teplotní pole | Případ senzoru |

Ve výřezu použijte voliče **Colormap** a **View**. Pro publikační obrázky
vykreslujte ve 4K přes MCP nástroj nebo ze skriptu.

**Jak číst Machův řez nadzvukové rakety:**

- Před špičkou stojí **čelní rázová vlna** (*bow shock*). Při Mach 2 s ostrým
  kuželem leží blízko špičky; tupá špička ji posune dopředu a stojí víc
  odporu.
- Za rameny, kde kužel přechází v trup, se Mach **zvyšuje** — to je
  Prandtlův–Meyerův expanzní vějíř.
- Každá náběžná hrana křidélka tvoří **vlastní šikmou rázovou vlnu**.
- Za dnem je pomalý **úplav** a odpor dna může při nadzvukové rychlosti tvořit
  třetinu celkového odporu.

---

## Lekce 6 — Případ senzoru BMP580

**Cíl:** zjistit, o kolik teplotní senzor přestřelí, a co s tím.

Tato větev odpovídá na jinou otázku než raketová část: BMP580 sedí
v 3D tištěné krabičce, napájené náporovým vzduchem přes odběrovou trubičku,
umístěné nad sluncem rozpálenou střechou tramvaje. **O kolik naměří víc, než
je skutečná teplota vzduchu?**

Přepněte na záložku **Sensor Microclimate (BMP580)**.

### 6.1 Předpověď je okamžitá

Na rozdíl od raketové větve tato záložka ukáže odpověď hned a aktualizuje ji,
jak hýbete posuvníky. Řeší analyticky provázané energetické bilance — střecha,
krabička, průtok trubičkou a komora senzoru — během milisekund.

S výchozími hodnotami (2 m/s, 25 °C, 800 W/m², 0,1 m nad střechou):

| Údaj | Hodnota |
|---|---|
| Senzor naměří | 26,97 °C |
| Skutečná teplota | 25,00 °C |
| **Chyba měření** | **+1,97 K** |
| Povrch střechy | 63,4 °C |
| Krabička | 28,7 °C |
| Průtok nasávání | 92,7 mg/s |
| Tepelná vrstva střechy | 15,4 mm |

Údaj o chybě je barevně odlišený: zelená pod 0,5 K, oranžová pod 2 K, červená
nad.

### 6.2 Co ta čísla znamenají

**Střecha 63 °C.** Tmavá střecha pod slunečním zářením 800 W/m² a jen při
2 m/s ofukování se opravdu tak zahřeje. To je zdroj problému.

**Tepelná vrstva střechy 15,4 mm.** To je klíčové geometrické číslo. Vzduch do
zhruba 15 mm od střechy je jí ohřátý. Vaše nasávání je ve 100 mm, takže
odebírá skutečně okolní vzduch — panel to říká slovy.

**Přesto chyba +1,97 K.** Když je nasávaný vzduch okolní, proč senzor měří
víc? Protože slunce ohřívá i *krabičku* a vzduch se cestou komorou k senzoru
ohřeje. To je dominantní člen.

**Vlastní ohřev je zanedbatelný.** 1 mW samotného BMP580 přispěje asi 5 mK —
tisícina chyby. Dobré vědět: problém je v okolí, ne v senzoru.

### 6.3 Vyzkoušejte si to

Táhněte posuvníky a sledujte:

| Změna | Efekt | Proč |
|---|---|---|
| Sluneční tok → 0 (noc) | Chyba jde **do záporu**, asi −0,5 K | Střecha i krabička vyzařují do studené oblohy a klesnou pod okolní teplotu |
| Rychlost → 0 (stojí) | Chyba vyskočí asi na **+13 K** | Není náporový vzduch, který by komoru propláchl, a nasávání je teď v tepelném oblaku střechy |
| Rychlost → 10 m/s | Chyba klesne asi na **+0,5 K** | Rychlejší proplachování |
| Výška → 0,01 m | Chyba se zhruba **ztrojnásobí** | Nasávání je nyní uvnitř tepelné vrstvy střechy |
| Pohltivost krabičky 0,3 → 0,1 | Chyba se zhruba **zmenší na polovinu** | Bílá či odrazivá krabička pohltí mnohem méně slunce |

### 6.4 Co s tím

V pořadí podle účinnosti na vynaložené úsilí:

1. **Udělejte krabičku bílou nebo odrazivou.** Pohltivost 0,3 → 0,1 zhruba
   půlí chybu. Nejlevnější možná náprava.
2. **Držte nasávání nad tepelnou vrstvou střechy.** Zkontrolujte hlášenou
   tloušťku vrstvy při nejnižší očekávané rychlosti a umístěte nasávání
   výrazně výš.
3. **Zastiňte krabičku**, pokud to instalace dovolí.
4. **Rozšiřte odběrovou trubičku.** Větší průtok stáhne měření blíž k okolní
   teplotě.
5. **Izolujte mezi krabičkou a komorou**, aby se teplo ze stěn méně přenášelo
   do odebíraného vzduchu.

Pamatujte, že nejhorší případ je **stojící vozidlo na plném slunci** —
tramvaj na zastávce. Pokud na tom vaší aplikaci záleží, navrhujte na to.

### 6.5 Plná CFD verze

Okamžitá odpověď používá zjednodušený model. Pro plné 3D řešení sdruženého
přenosu tepla — teplotní pole v celé tištěné krabičce i ve vnitřním vzduchu —
načtěte CAD krabičky, vysíťujte jej v tepelné větvi a spusťte sdružený řešič.
Ten rozliší detaily, které zjednodušený model průměruje.

---

## Lekce 7 — Projekty a nastavení

**Cíl:** přestat ztrácet práci.

### 7.1 Projekty

Projekt ukládá **všechno**: cestu ke CAD a natočení, oblast, nastavení sítě,
letový režim, nastavení řešiče, osy závěsů, scénář senzoru, definici
parametrické studie a vaše poznámky.

| Akce | Jak |
|---|---|
| Uložit | **File → Save project** (Ctrl+S) |
| Uložit pod novým názvem | **File → Save project as** |
| Otevřít | **File → Open project** (Ctrl+O) |
| Otevřít nedávný | **File → Open recent** |
| Začít znovu | **File → New project** (Ctrl+N) |
| Upravit název a poznámky | **File → Project details** |

Projekty jsou prostý JSON s příponou `.atsproj`. Dají se číst v textovém
editoru, porovnávat ve verzovacím systému a ukládat vedle CAD, ke kterému
patří. Ukládání je atomické — přerušené uložení nemůže poškodit existující
soubor.

**Využívejte pole s poznámkami.** Za šest týdnů si nevzpomenete, proč byla
oblast 8 L proti proudu. Zapište si to.

### 7.2 Nastavení

**Settings → Preferences** obsahuje věci, které nepatří k žádné konkrétní
studii: výchozí počet MPI procesů, výchozí barevnou mapu a rozlišení a
přepínače chování. Zachovají se mezi spuštěními.

**Settings → Open data folder** otevře složku, kde jsou uloženy sítě, výsledky
a projekty — standardně `%LOCALAPPDATA%\AeroThermalStudio`. Výstupy simulací
jsou velké; sem se dívejte, když potřebujete uvolnit místo na disku.

---

## Lekce 8 — Ovládání pomocí AI

**Cíl:** nechat asistenta, aby studie prováděl za vás.

Všechno, co je v grafickém rozhraní, je dostupné i přes Model Context
Protocol, takže AI asistent umí připravit geometrii, vysíťovat, spočítat,
prohledat parametry i vykreslit.

Stručně: nasměrujte svého AI klienta na `Run-MCP-Server.bat` a pak požádejte
běžným jazykem —

> „Vysíťuj raketu v `C:\models\rocket.step` se špičkou podél +X, pak
> prohledej Mach od 0,5 do 3,0 v šesti krocích při úhlu náběhu 5°, se závěsem
> křidélka v [0.9, 0.04, 0] otáčejícím se kolem [0, 1, 0]. Řekni mi maximální
> moment na závěsu."

Kompletní nastavení, přehled nástrojů a hotové postupy najdete v
[MCP_AI_GUIDE.md](MCP_AI_GUIDE.md).

---

## Řešení problémů

### „Python was not found"

Nainstalujte Python 3.11+ z python.org a při instalaci **zaškrtněte „Add
Python to PATH"**. Pak spusťte `Setup.bat`.

### „SU2_CFD was not found"

Tvorba sítě, analýza senzoru i vykreslování fungují bez něj — SU2 potřebujete
jen pro výpočet proudění. Instalace:

1. Stáhněte si sestavení pro Windows ze
   [stránky vydání SU2](https://github.com/su2code/SU2/releases).
2. Rozbalte, například do `C:\SU2`.
3. Nastavte `SU2_RUN` na složku se souborem `SU2_CFD.exe`:
   ```
   setx SU2_RUN "C:\SU2\bin"
   ```
4. Otevřete **nový** terminál a spusťte `Check-Environment.bat`.

### „a 10-rank run needs an MPI launcher"

Nainstalujte Microsoft MPI — **oba** soubory, `msmpisetup.exe` i
`msmpisdk.msi` — a otevřete terminál znovu. Nebo nastavte MPI ranks na 1 pro
sériový výpočet, který je pomalejší, ale funguje.

### CAD soubor nejde načíst

- Program potřebuje **objemové těleso**, ne plochy. Před exportem model sešijte
  do tělesa.
- Zkontrolujte **Scale to metres**: `0.001` pro milimetry.
- Velmi složité sestavy může trvat dlouho ozdravit. Zjednodušte vnitřní
  detaily, které proudění nikdy neuvidí.

### Nepovede se mezní vrstva

Obvykle za to může matematicky ostrý prvek — břit nebo dokonalý hrot — který
nejde čistě vysíťovat. Přidejte malé zaoblení, klidně 0,1 mm. Skutečné
součásti ho stejně mají.

Pokud síť projde, ale hlásí zmrazené uzly (*frozen vertices*), je to normální
a lokální: pár bodů dostane tenčí mezní vrstvu, zbytek je nedotčený.

### Počet buněk se netrefí do cílového rozsahu

Zvyšte **max targeting iterations** nebo změňte rozlišení. U velmi malých či
velmi velkých modelů je někdy potřeba upravit i násobky oblasti.

### Výpočet diverguje

- Snižte **CFL number** (přes soubor projektu nebo MCP nástroje). Výchozí je
  5; zkuste 2.
- Zkontrolujte kvalitu sítě. Pod ~0,05 to bývá problém.
- Ověřte, že je letový režim fyzikálně smysluplný — velmi vysoký úhel náběhu
  při vysokém Mach nemusí mít ustálené řešení vůbec.

### Výsledky vypadají špatně

1. Zkontrolujte výpis odvozeného režimu (`M ... | V ... | p ... | T ...`).
2. Zkontrolujte **Scale to metres**.
3. Zkontrolujte směr špičky — těleso vysíťované pozpátku dá věrohodně vypadající,
   ale chybná čísla.
4. Než čemukoli uvěříte, udělejte studii nezávislosti na síti.

### Kde jsou moje soubory?

**Settings → Open data folder**, nebo `%LOCALAPPDATA%\AeroThermalStudio`:

```
runs/        jedna složka na každou síť, simulaci a studii
projects/    uložené soubory .atsproj
samples/     vygenerovaný ukázkový CAD
settings.json
```

---

## Kam dál

- **[USER_GUIDE.md](USER_GUIDE.md)** — přehled všech nastavení a jak volit
  hodnoty.
- **[MCP_AI_GUIDE.md](MCP_AI_GUIDE.md)** — kompletní rozhraní pro AI.
- **[../../README.md](../../README.md)** — architektura a technické poznámky.
