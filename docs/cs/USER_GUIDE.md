# AeroThermalStudio — Uživatelská příručka

Přehled všech nastavení: co dělají, na co mají vliv a jak volit hodnoty.
Úvod krok za krokem najdete v [TUTORIAL.md](TUTORIAL.md).

> **Poznámka.** Názvy nastavení jsou v programu anglicky, proto je zde uvádím
> anglicky s českým vysvětlením.

**Obsah**

1. [Konvence](#1-konvence)
2. [Geometrie](#2-geometrie)
3. [Výpočetní oblast](#3-výpočetní-oblast)
4. [Letový režim](#4-letový-režim)
5. [Osa závěsu křidélka](#5-osa-závěsu-křidélka)
6. [Síť](#6-síť)
7. [Řešič](#7-řešič)
8. [Výsledky](#8-výsledky)
9. [Mikroklima senzoru](#9-mikroklima-senzoru)
10. [Vizualizace](#10-vizualizace)
11. [Projekty a nastavení](#11-projekty-a-nastavení)
12. [AI asistent](#12-ai-asistent)
13. [Soubory na disku](#13-soubory-na-disku)
14. [Poznámky k fyzice](#14-poznámky-k-fyzice)

---

## 1. Konvence

**Jednotky.** Metry, sekundy, kilogramy, newtony, pascaly, kelviny — kromě
případů, kdy název pole říká jinak (`_deg` pro stupně, `_c` pro stupně
Celsia, `_ms` pro metry za sekundu). Každé pole nese jednotku v názvu nebo
v nápovědě.

**Souřadný systém.** Po zarovnání leží osa tělesa podél **+X** a proud
přichází z −X. Vámi zvolený referenční bod je v (0, 0, 0). Všechny výsledky —
síly, momenty, působiště, polohy závěsů — jsou v této soustavě, ne ve vaší
CAD soustavě.

**Úhly.** Úhel náběhu a vybočení aplikuje *řešič* nakloněním přicházejícího
proudu. Síť se vždy tvoří v soustavě tělesa. Proto změna úhlu nevyžaduje nové
síťování.

**Kontrola vstupů.** Každý vstup má meze. Hodnota mimo rozsah je odmítnuta
s vysvětlením, ne tiše oříznuta — stejně v grafickém rozhraní i přes MCP.

---

## 2. Geometrie

### STEP file

Cesta k souboru `.step` nebo `.stp`. Přetáhněte do okna, nebo vyberte přes
**Browse**.

Soubor musí obsahovat **objemové těleso** (*solid*), ne sadu ploch. Pokud
import hlásí, že žádné těleso nenašel, sešijte plochy do tělesa ve svém CAD
programu.

Při importu se geometrie ozdravuje: rozdělené plochy se sešijí, degenerované
hrany odstraní, drobné plošky opraví. Pokud ozdravení těleso zničí — což se
u některých jednoduchých tvarů stává — použije se neozdravený import a zpráva
to uvede.

### Nose direction

Kudy míří špička **v souřadném systému vašeho CAD souboru**.

| Volba | Kdy |
|---|---|
| `+X` … `-Z` | Těleso je zarovnáno s osou CAD |
| Vlastní vektor | Ve všech ostatních případech |

Vlastní vektor se automaticky normalizuje; záleží jen na směru. Nulový vektor
je odmítnut.

Toto je nastavení s největšími důsledky. Když je špatně, těleso se vysíťuje
bokem nebo pozpátku a všechny výsledky budou věrohodné a chybné.

### Reference origin

Bod přesunutý do počátku větrného tunelu. Použijte **špičku**.

Je to referenční bod pro působiště, takže smysluplná volba dělá výsledky přímo
interpretovatelné — „působiště je 0,62 m za špičkou" místo „0,62 m od nějakého
libovolného bodu".

### Scale to metres

Násobek převádějící jednotky CAD na metry.

| Jednotky CAD | Hodnota |
|---|---|
| Metry | `1.0` |
| Milimetry | `0.001` |
| Centimetry | `0.01` |
| Palce | `0.0254` |

**Tohle se vyplní samo.** Při importu STEP souboru se z něj přečte
deklarovaná jednotka a doplní se sem; řádek pod názvem souboru pak říká, co
se našlo — *„Sapphire.step, read as millimetres — 1.32 m across"*. Tu větu si
přečtěte. Když rozměr neodpovídá očekávání, je jednotka špatně a nic dalšího
si toho nevšimne.

Deklaraci program věří jen potud, pokud dává smysl. Některé exportéry,
OpenCASCADE mezi nimi, zapíšou milimetrovou hlavičku do modelu, jehož
souřadnice jsou zjevně v metrech; slepé následování by z metrové rakety
udělalo milimetrovou. Když deklarovaná jednotka a vlastní velikost modelu
nesouhlasí, vyhrává velikost a poznámka to uvede. Když soubor nedeklaruje
nic, poznámka vyzve ke kontrole.

Chybná hodnota škáluje Reynoldsovo číslo stejným poměrem a znehodnotí
všechno. Zkontrolujte referenční délku ve zprávě o síti proti očekávání.

### Předání souboru asistentovi

Import STEP souboru zároveň řekne vestavěnému asistentovi, o který soubor
jde. Můžete pak napsat *„vysíťuj model, co jsem právě naimportoval, špička
podél +Y, coarse"* a cestu znovu psát nemusíte — asistent použije tentýž
soubor i tutéž jednotku, jakou vidíte ve formuláři. V odpovědi potvrdí, který
soubor použil.

---

## 3. Výpočetní oblast

Objem vzduchu kolem tělesa, rozměřený v násobcích délky tělesa `L`.

| Nastavení | Výchozí | Rozsah | Poznámka |
|---|---|---|---|
| Upstream | 5 L | 1–50 | Špička ke vstupu |
| Downstream | 10 L | 1–50 | Dno k výstupu |
| Radial | 5 L | 1–50 | Osa k vnější hranici |
| Shape | cylinder | cylinder / box | |

**Volba hodnot.** Výchozí nastavení vyhovuje většině vnější aerodynamiky.

- *Příliš malá* oblast a hranice ovlivňuje řešení — klasický příznak je
  výsledek, který se změní po zvětšení oblasti.
- *Příliš velká* a buňky se plýtvají na vzduch, kde se nic neděje.

Downstream je větší, protože úplav potřebuje vzdálenost k rozvinutí. Při
nadzvukových rychlostech lze často zmenšit vzdálenost proti proudu, protože
poruchy se nemohou šířit dopředu před čelní rázovou vlnu. Při transsonických
rychlostech udělejte opak: poruchy se šíří daleko, dejte jim víc prostoru.

**Tvar.** `cylinder` plýtvá kolem štíhlého tělesa méně buňkami. `box` se hodí
pro krabicové případy a situace blízko země, jako je krabička senzoru.

---

## 4. Letový režim

| Nastavení | Rozsah | Poznámka |
|---|---|---|
| Speed as | mach / tas | Jak se interpretuje hodnota rychlosti |
| Speed | Mach 0,05–3,5, nebo m/s | |
| Angle of attack | ±20° | Klopení vůči proudu |
| Sideslip | ±20° | Vybočení vůči proudu |
| Altitude | −610 až 32 000 m | Nastaví tlak, teplotu, hustotu |

**Mach, nebo pravá vzdušná rychlost.** Machovo číslo řídí fyziku —
stlačitelnost, rázové vlny, volbu numerického schématu. Pravou vzdušnou
rychlost hlásí palubní počítač. Souvisí spolu přes rychlost zvuku, která
závisí na teplotě a tedy na výšce, takže 200 m/s u hladiny moře a v 10 km dává
různá Machova čísla.

**Mez ±20°** není libovolná. Za ní se proudění kolem štíhlého tělesa masivně
odtrhává a ustálené RANS řešení přestává mít smysl. Číslo by se stále objevilo;
nebylo by správné.

**Výška** používá standardní atmosféru ISA-1976, ověřenou proti publikovaným
tabulkám. Alternativně lze zadat tlak a teplotu přímo (v souborech projektu a
přes MCP), když máte naměřené podmínky.

Odvozený stav se zobrazuje živě:

```
M 2.000  |  V 680.6 m/s  |  p 101.3 kPa  |  T 288.2 K
```

---

## 5. Osa závěsu křidélka

| Nastavení | Význam |
|---|---|
| Name | Označení ve výsledcích a křivkách |
| Point | Libovolný bod na ose závěsu, metry, v zarovnané soustavě |
| Direction | Osa otáčení |

**Jak se moment počítá.** Řešič hlásí aerodynamický moment kolem referenčního
bodu. To není, co cítí servo. Program:

1. Vezme celkovou sílu a moment kolem referenčního bodu;
2. Přenese moment do vašeho bodu závěsu pomocí vztahu pro posun osy
   `M_závěs = M_ref + (r_ref − r_závěs) × F`;
3. Promítne výsledek do vašeho směru závěsu.

Výstupem je skalár v newtonmetrech. Otáčet křidélkem může jen složka podél
osy; zbytek nese ložisko.

**Znaménko.** Kladné podle pravidla pravé ruky kolem zadaného směru. Otočení
směru otočí znaménko. Pro návrh serva je rozhodující velikost.

**Kontrola.** Síla působící přesně skrz osu závěsu dává přesně nulový moment,
bez ohledu na to, kde leží reference řešiče. Toto ověřují testy.

---

## 6. Síť

### Rozlišení (Resolution)

| Předvolba | Buňky, aerodynamika | Buňky, teplo |
|---|---|---|
| coarse | 250 000 – 400 000 | 150 000 – 230 000 |
| medium | 400 000 – 600 000 | 230 000 – 320 000 |
| fine | 600 000 – 750 000 | 320 000 – 400 000 |

Generátor sítě neaplikuje jen předvolenou velikost. Vysíťuje, spočítá buňky a
upraví charakteristickou velikost, dokud se počet netrefí do rozsahu — až
čtyři pokusy. To dělá z rozsahu záruku, ne naději, i na libovolné geometrii.

Rozsahy jsou zvoleny tak, aby výpočet trval 3–8 minut na šestijádrovém stroji
s 16 GB RAM.

### Prism layers

Rozsah 5–8, výchozí 7. Tenké ploché buňky vrstvené u stěny, které rozlišují
mezní vrstvu.

Více vrstev lépe rozliší profil u stěny a stojí buňky. Sedm je dobrý
kompromis pro modelování turbulence se stěnovými funkcemi.

### Target y+

Rozsah 30–300, výchozí 45.

y⁺ je bezrozměrná vzdálenost od stěny. Říká, kde leží střed první buňky
v rámci mezní vrstvy:

| y⁺ | Oblast | Vhodné pro |
|---|---|---|
| < 5 | Viskózní podvrstva | Modely rozlišující stěnu, velmi drahé |
| 30–300 | Logaritmická vrstva | **Stěnové funkce** — co používá tento program |
| mezi | Přechodová vrstva | Vyhnout se; ani jeden přístup neplatí |

Držte se 30–60, pokud nevíte, proč chcete jinak. Generátor sítě spočítá
potřebnou výšku první vrstvy z letového režimu, takže zadáváte fyzikální cíl a
on dopočítá geometrii.

Dosažené y⁺ se po vysíťování vypíše a mělo by přesně odpovídat zadání.

### Minimum quality

Hlásí se po vysíťování: nejhorší buňka jako podíl dokonale tvarované.

| Hodnota | Hodnocení |
|---|---|
| > 0,3 | Zdravé |
| 0,1 – 0,3 | Přijatelné |
| < 0,05 | Očekávejte potíže s konvergencí |

---

## 7. Řešič

| Nastavení | Výchozí | Poznámka |
|---|---|---|
| Turbulence model | SST | SST k-ω, nebo SA (Spalart–Allmaras) |
| Convective scheme | automaticky | JST pod Mach 0,8, Roe nad |
| CFL number | 5,0 | Větší konverguje rychleji, ale méně robustně |
| Max iterations | 5000 | Tvrdý strop |
| Convergence residual | −5,0 | log₁₀ RMS rezidua hustoty |
| MPI ranks | 10 | O dvě méně než vláken |

### Volba schématu

Vybírá se automaticky podle Machova čísla:

- **Pod Mach 0,8** — centrální diference JST se skalární disipací. Účinné a
  přesné v hladkém podzvukovém proudění.
- **Od Mach 0,8 výše** — protiproudé schéma Roe s rekonstrukcí MUSCL druhého
  řádu a Venkatakrishnanovým limiterem, plus entropická korekce. Limiter je
  to, co brání oscilacím na rázových vlnách; entropická korekce brání
  Roeovu řešiči připustit nefyzikální expanzní rázy.

Přepisujte jen, když víte proč.

### Konvergence

Výpočet se zastaví, když **buď**:

- reziduum hustoty dosáhne prahu, **nebo**
- součinitel odporu je ustálený po zadaný počet posledních iterací.

Druhé je v praxi důležité: RANS výpočet často dosáhne použitelných sil dávno
před cílovým reziduem, a právě o síly vám jde.

### MPI ranks

Na kolik procesů se řešič rozdělí. Použijte o dvě méně, než máte logických
procesorů, aby rozhraní zůstalo svižné — na 12vláknovém CPU tedy 10.

Více procesů než fyzických jader má klesající přínos; každý proces navíc
potřebuje paměť a příliš mnoho jich na velké síti vyčerpá 16 GB.

---

## 8. Výsledky

| Veličina | Značka | Význam |
|---|---|---|
| Součinitel odporu | C_d | Odpor / (q·S) |
| Součinitel vztlaku | C_l | Vztlak / (q·S) |
| Součinitel boční síly | C_s | Boční síla / (q·S) |
| Klopivý moment | C_m | Moment / (q·S·L) |
| Síly | F_x, F_y, F_z | Složky v soustavě tělesa, newtony |
| Odpor / vztlak / boční síla | | V soustavě proudu, newtony |
| Působiště | | Osová poloha, metry |
| Moment na závěsu | τ | Newtonmetry na každém závěsu |

Zde `q = ½ρV²` je dynamický tlak, `S` referenční plocha (průřez tělesa, není-li
přepsána) a `L` referenční délka (průměr tělesa, není-li přepsána).

**Soustava tělesa versus soustava proudu.** Soustava tělesa je pevně spojená
s raketou. Soustava proudu je zarovnaná s prouděním. Při nulovém úhlu náběhu
splývají; při úhlu se liší přesně o něj. Odpor a vztlak jsou z definice
veličiny v soustavě proudu.

**Působiště** hlásí `NaN`, když není příčná síla — při nulovém náběhu na
symetrickém tělese působiště skutečně není definováno a uvést číslo by svádělo
věřit něčemu bezvýznamnému. Spusťte při 2–5°, abyste dostali použitelnou
hodnotu.

---

## 9. Mikroklima senzoru

### Prostředí

| Nastavení | Výchozí | Rozsah | Poznámka |
|---|---|---|---|
| Vehicle speed | 2,0 m/s | 0–50 | Žene náporový vzduch nasáváním |
| Ambient air | 25 °C | −50 až 70 | Skutečná teplota vzduchu — reference |
| Solar flux | 800 W/m² | 0–1400 | ~1000 je jasné poledne; 0 je noc |
| Height above roof | 0,1 m | 0,01–2 | Porovnává se s tepelnou vrstvou střechy |

### Vlastnosti povrchů

| Nastavení | Výchozí | Poznámka |
|---|---|---|
| Housing solar absorptivity | 0,30 | Podíl pohlceného slunce. Bílá ~0,1, černá ~0,95 |
| Roof solar absorptivity | 0,65 | Lakovaný plech. Holý hliník ~0,2 |
| Housing emissivity | 0,90 | Dlouhovlnné vyzařování. Většina nekovů 0,85–0,95 |
| Housing conductivity | 0,18 W/(m·K) | PLA/PETG. Hliník ~200 |
| Sensor position | — | Poloha čipu BMP580 uvnitř krabičky |

Pohltivost a emisivita jsou různé vlastnosti na různých vlnových délkách. Bílý
nátěr může mít pohltivost 0,2 ve slunečním světle a emisivitu 0,9
v infračervené oblasti — právě proto bílý nátěr zůstává chladný.

### Výstupy

| Údaj | Význam |
|---|---|
| Sensor reads | Co by BMP580 naměřil |
| True ambient | Skutečná teplota vzduchu |
| **Measurement error** | Rozdíl — číslo, o které jde |
| Roof surface | Jak se rozpálí střecha |
| Housing | Teplota krabičky |
| Intake flow | Hmotnostní průtok nasávání, mg/s |
| Roof thermal layer | Tloušťka ohřáté vrstvy vzduchu u střechy, mm |

Chyba je barevně odlišená: zelená pod 0,5 K, oranžová pod 2 K, červená nad.

**Číslo tepelné vrstvy je to důležité.** Je-li vaše výška nasávání pod ním,
odebíráte vzduch, který střecha ohřála, a žádný návrh krabičky to nespraví.
Panel říká, ve kterém režimu jste.

### Dvě úrovně přesnosti

**Analytická** (okamžitá, výchozí v rozhraní). Řeší provázané energetické
bilance střechy, krabičky, průtoku trubičkou a komory. Dost rychlá na
aktualizaci při tahání posuvníkem, což z ní dělá návrhový nástroj, ne dávkovou
úlohu.

**Sdružené CFD** (minuty, potřebuje tepelnou síť). Plné 3D řešení s vedením
tepla pevnou krabičkou a prouděním vnitřního vzduchu. Rozliší detaily, které
analytický model průměruje.

Analytický model zároveň připravuje linearizaci záření pro CFD výpočet, takže
spolu spolupracují, nejsou to alternativy.

---

## 10. Vizualizace

### Režimy

| Režim | Ukazuje |
|---|---|
| `surface_pressure` | C_p nebo absolutní tlak na tělese |
| `mach_slice` | Machovo číslo v rovině řezu |
| `streamlines` | Proudnice barvené rychlostí nebo teplotou |
| `thermal` | Teplotní pole s vyznačeným senzorem |

### Barevné mapy

`turbo`, `coolwarm`, `viridis`, `jet`, `plasma`, `inferno`.

Pro teplotu použijte **coolwarm** — je divergentní, takže se přirozeně čte
kolem střední hodnoty. Pro cokoli, co bude tištěno v odstínech šedi nebo
prohlíženo barvoslepým čtenářem, použijte **viridis**; je percepčně
rovnoměrná. **turbo** dává největší vizuální kontrast pro tlak a Machovo
číslo.

### Pohledy kamery

`isometric`, `front`, `back`, `side`, `top`, `bottom`, `nose_quarter`,
`tail_quarter`.

### Rozlišení

`preview` (960×540), `hd` (1920×1080), `2k` (2560×1440), `4k` (3840×2160).

Při zkoumání používejte `preview`, pro obrázky do zprávy `4k`.

### Export

`.vtk` / `.vtu` pro analýzu v ParaView, `.gltf` pro interaktivní 3D scénu,
která se otevře v prohlížeči.

---

## 11. Projekty a nastavení

### Projekty (`.atsproj`)

Projekt ukládá kompletní nastavení: geometrii a natočení, oblast, nastavení
sítě, letový režim, nastavení řešiče, osy závěsů, scénář senzoru, definici
parametrické studie, poznámky a naposledy použitou síť.

Prostý JSON — čitelný, porovnatelný a vhodný k uložení do verzovacího systému
vedle vašeho CAD.

| Akce | Zkratka |
|---|---|
| Nový | Ctrl+N |
| Otevřít | Ctrl+O |
| Uložit | Ctrl+S |
| Uložit jako | Ctrl+Shift+S |

Projekt odkazující na síť, která byla mezitím smazána, to při načtení oznámí a
zakáže spuštění, místo aby selhal později uvnitř řešiče.

Projekt zapsaný novější verzí programu je **odmítnut**, ne částečně načten.
Tiché zahození polí, kterým starší sestavení nerozumí, by ztratilo nastavení
bez upozornění.

### Předvolby

**Settings → Preferences:**

| Nastavení | Efekt |
|---|---|
| Default MPI ranks | Výchozí hodnota pro nové výpočty |
| Default colormap | Předvolená ve výřezu a při vykreslování |
| Render resolution | Předvolené pro ukládané obrázky |
| Mesh resolution | Výchozí předvolba pro nové projekty |
| Ask before discarding | Potvrdit, než Nový/Otevřít zahodí neuloženou práci |
| Autosave before solve | Uložit projekt při spuštění výpočtu |
| Live sensor preview | Přepočítat při pohybu posuvníků |
| Warn about environment | Upozornit při startu na chybějící SU2 nebo MPI |

Poškozený soubor nastavení se nahradí výchozími hodnotami, místo aby zabránil
spuštění.

---

## 12. AI asistent

Záložka **AI Assistant** umožňuje napsat běžnou větou, co chcete, a nechat to
program udělat. Asistent sahá na program přesně přes těch třináct nástrojů,
které vystavuje MCP server — umí tedy to, co umíte vy přes rozhraní, a nic
víc.

Běží na modelu podle vaší volby přes [OpenRouter](https://openrouter.ai),
takže potřebujete účet na OpenRouteru a API klíč. Samotný program žádný model
neobsahuje a dokud klíč nezadáte, nikam nic neposílá.

### Zadání API klíče

1. Klíč získáte na [openrouter.ai/keys](https://openrouter.ai/keys).
2. Vložte ho do pole **Key** vlevo v záložce AI Assistant a stiskněte
   **Save key**.

Políčko se hned po uložení vymaže a klíč se už nikdy nezobrazí — jen jeho
maskovaná podoba, například `sk-or-...a1b2`, a informace, kde je uložený.

Kde je uložený, závisí na počítači:

| Úložiště | Kdy se použije | Bezpečné |
|---|---|---|
| Proměnná prostředí `OPENROUTER_API_KEY` | Má vždy přednost, pokud je nastavená | Ano — na disk se nic nezapisuje |
| Správce pověření Windows | Kdykoli je nainstalovaný balík `keyring` | Ano — chráněný vaším přihlášením |
| `credentials.json` | Jen když žádné úložiště pověření není | **Ne** — prostý text, práva jen pro vlastníka |

Poslední případ program oznámí dialogem, který to říká na rovinu. Je to
náhradní řešení, ne bezpečnostní opatření: klíč chrání před ostatními účty na
stroji a před ničím dalším. `Setup.bat` balík `keyring` nainstaluje, aby se
použil Správce pověření.

**Klíč se nikdy nedostane do souboru projektu ani do `settings.json`.** To
jsou prosté JSONy určené k verzování; klíč v nich by skončil v repozitáři.
Tlačítko **Remove** klíč smaže ze všech úložišť naráz.

### Volba modelu

Funguje libovolné id modelu z OpenRouteru. Rozbalovací seznam začíná několika
návrhy; **Fetch available models** je nahradí živým katalogem, na který váš
účet skutečně dosáhne — katalog se totiž neustále mění a napevno zapsaný
seznam by rychle zastaral.

Výchozí je `deepseek/deepseek-chat` — levný, rychlý a na tuhle práci
dostatečný. Model musí umět volání nástrojů (tool calling), jinak si s vámi
umí jen povídat. Volba se pamatuje mezi spuštěními.

**Za to, co asistent spotřebuje, platíte OpenRouteru** podle tokenů a sazby
daného modelu. Krátký dotaz stojí zlomek centu, delší série volání nástrojů
víc. Počet tokenů každého požadavku se ukáže ve stavovém řádku.

### Jak s ním mluvit

Napište požadavek a stiskněte **Send** nebo Ctrl+Enter. Odpovídá v jazyce, ve
kterém píšete. Užitečné požadavky vypadají takhle:

- *„Vysíťuj C:\models\rocket.step se špičkou podél +X, rozlišení coarse."*
- *„O kolik přestřelí BMP580 při 900 W/m² a rychlosti 3 m/s?"*
- *„Porovnej chybu senzoru pro bílou a černou krabičku."*
- *„Projeď Mach 0,5 až 3 při 5 stupních a řekni mi nejhorší moment na závěsu."*
- *„Zkontroluj prostředí a řekni mi, jestli můžu spustit výpočet."*

Každé volání nástroje se objeví v přepisu, jakmile proběhne, i s argumenty a
dobou trvání. Asistent, který by potichu spustil půlhodinovou studii, by byl
horší než žádný.

**New conversation** zapomene historii. Udělejte to při změně tématu: s každým
požadavkem se posílá celá konverzace, takže dlouhá stojí víc a dává modelu
větší šanci splést si dvě úlohy.

### Kontrola nad tím, co dělá

| Přepínač | Co dělá |
|---|---|
| Ask before meshing, solving or sweeping | Potvrzení před každým dlouhým krokem, s názvem nástroje, argumenty a očekávanou dobou běhu. Standardně zapnuto. |
| Max tool rounds | Strop počtu kol volání nástrojů na jeden požadavek. Zabrání zmatenému modelu utrácet kredit ve smyčce. Výchozí 12. |

Když krok odmítnete, model se to dozví a zeptá se, co byste chtěli místo
toho — nezkusí totéž znovu.

### Co neumí

- **Žádné příkazy shellu, žádný přístup k libovolným souborům, žádné spouštění
  kódu.** Hranicí je seznam nástrojů a ty berou jen validované parametry.
- **Nevymýšlí si čísla.** Má v instrukcích hlásit, co nástroje vrátily, včetně
  toho, že výpočet nezkonvergoval nebo že počet buněk minul cílové pásmo.
  Když na čísle záleží, porovnejte přepis s panelem výsledků.
- **Není to aerodynamik.** Program umí obsluhovat dobře; že máte špatně
  geometrii, vám neřekne. Úsudek zůstává na vás.

Požadavky, které napíšete, a parametry simulace se posílají OpenRouteru a
poskytovateli zvoleného modelu. Výsledky a soubory sítí zůstávají u vás — po
síti jdou jen čísla z výsledků nástrojů.

### Když něco nefunguje

| Hlášení | Co znamená |
|---|---|
| *No OpenRouter API key is set* | Uložte klíč, nebo nastavte `OPENROUTER_API_KEY` |
| *OpenRouter rejected the API key* | Klíč je špatný nebo zneplatněný — ověřte na openrouter.ai/keys |
| *insufficient credit* | Dobijte kredit, nebo zvolte levnější model |
| *rate-limiting this key* | Chvíli počkejte; modely zdarma jsou omezované |
| *could not reach OpenRouter* | Síť, proxy nebo firewall |
| *I stopped after N rounds* | Rozdělte požadavek na menší kroky |

---

## 13. Soubory na disku

Výchozí umístění `%LOCALAPPDATA%\AeroThermalStudio`, lze přepsat proměnnou
prostředí `ATS_DATA_ROOT`.

```
AeroThermalStudio/
├── runs/                      jedna složka na síť, simulaci a studii
│   ├── mesh-RRRRMMDD-HHMMSS-xxxxxx/
│   │   ├── record.json        metadata
│   │   ├── mesh.su2           síť
│   │   ├── mesh_request.json  co bylo zadáno
│   │   └── mesh_result.json   co vyšlo
│   ├── aero-.../
│   │   ├── solver.cfg         konfigurace SU2
│   │   ├── history.csv        průběh konvergence
│   │   ├── forces_breakdown.dat
│   │   ├── flow.vtu           objemové řešení
│   │   ├── result.json        zpracovaný výsledek
│   │   └── renders/           vytvořené obrázky
│   └── sweep-.../
│       ├── point_000/ …       jedna složka na bod
│       └── sweep.json         křivky a extrémy
├── projects/                  uložené soubory .atsproj
├── samples/                   vygenerovaný ukázkový CAD
├── su2/                       binárky řešiče, pokud jsou zde
└── settings.json
```

Objemová řešení jsou velká. Staré složky výpočtů mažte pro uvolnění místa —
program čte registr pokaždé znovu a co je pryč, prostě nevypíše.

---

## 14. Poznámky k fyzice

### Proč na y⁺ tolik záleží

U stěny se rychlost mění extrémně rychle. Rozlišit to přímo vyžaduje buňky tak
tenké, že by realistický model narostl na miliony buněk a hodiny výpočtu.

Stěnové funkce modelují profil u stěny analyticky místo jeho rozlišení, což
dovolí první buňce být mnohem větší — ale jen pokud padne do oblasti
logaritmického zákona, y⁺ mezi 30 a 300. Příliš blízko a předpoklad neplatí;
příliš daleko a logaritmická vrstva není rozlišená.

Proto generátor sítě pracuje pozpátku: zadáte cíl y⁺ a letový režim, on
dopočítá výšku buňky.

### Proč prizmata, ne tetraedry

Buňky mezní vrstvy musí být tenké kolmo ke stěně, ale mohou být podél ní
mnohem větší — poměry stran 1000:1 jsou běžné. Tetraedry to neumí dobře;
protažené tetraedry mají příšerné numerické vlastnosti.

Prizmata ano: trojúhelník protažený ve směru normály stěny. Tento program je
vytahuje vlastním algoritmem postupných vrstev, protože 3D vytahování mezní
vrstvy v Gmsh spolehlivě nefunguje. Podrobnosti jsou v README.

Bez prizmat by y⁺ 45 na metrovém tělese vyžadovalo miliony povrchových
trojúhelníků samotných — zhruba pětinásobek stropu buněk, který drží výpočty
v cílovém čase.

### Proč rázové vlny potřebují jiné numerické schéma

Rázová vlna je nespojitost: tlak, hustota i teplota skokem mění hodnotu na
vzdálenosti několika středních volných drah molekul.

Centrální diference předpokládá hladké řešení. Na nespojitosti vytvoří
oscilace, které rostou, až výpočet selže.

Protiproudá schémata respektují směr šíření informace a zachytí ráz čistě, za
cenu větší numerické disipace v hladkých oblastech. Limiter sníží schéma na
první řád poblíž rázu — kde jde o robustnost — a jinde ponechá druhý řád.

Odtud automatické přepnutí na Mach 0,8.

### Proč senzor měří víc

Tři mechanismy, v pořadí důležitosti pro výchozí tramvajový případ:

1. **Ohřev krabičky.** Slunce ohřívá krabičku; odebíraný vzduch se cestou
   komorou ohřeje. Dominantní.
2. **Poloha nasávání.** Je-li nasávání uvnitř tepelné mezní vrstvy střechy,
   odebírá předehřátý vzduch. Ve 100 mm zanedbatelné, v 10 mm dominantní.
3. **Vlastní ohřev.** 1 mW samotného BMP580. Asi 5 mK — tisícina celku.

V noci se znaménko obrátí: bez slunce krabička vyzařuje do studené oblohy a
údaj klesne *pod* okolní teplotu.

---

## Viz také

- **[TUTORIAL.md](TUTORIAL.md)** — úvod krok za krokem
- **[MCP_AI_GUIDE.md](MCP_AI_GUIDE.md)** — rozhraní pro AI
- **[../../README.md](../../README.md)** — architektura a implementace
