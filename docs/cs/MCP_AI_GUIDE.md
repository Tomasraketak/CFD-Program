# AeroThermalStudio — MCP a AI

Jak program ovládat z AI asistenta a kompletní přehled nástrojů.

**Obsah**

1. [Co to je](#1-co-to-je)
2. [Nastavení](#2-nastavení)
3. [Přehled nástrojů](#3-přehled-nástrojů)
4. [Hotové postupy](#4-hotové-postupy)
5. [Jak psát zadání](#5-jak-psát-zadání)
6. [Chybové stavy](#6-chybové-stavy)
7. [Poznámky pro autory agentů](#7-poznámky-pro-autory-agentů)
8. [Meze a soudnost](#8-meze-a-soudnost)
9. [Vestavěný asistent](#9-vestavěný-asistent)

---

## 1. Co to je

Model Context Protocol (MCP) je standardní způsob, jak AI asistenti volají
externí nástroje. AeroThermalStudio obsahuje MCP server, který vystavuje
**třináct nástrojů** pokrývajících vše, co umí program: import CAD, síťování,
aerodynamickou i tepelnou simulaci, vykreslování, parametrické studie a správu
projektů a nastavení.

Připojeného asistenta pak můžete požádat běžnou řečí:

> „Vysíťuj `C:\models\rocket.step` se špičkou podél +X, spusť to při Mach 2 a
> úhlu náběhu 5° a řekni mi odpor a moment na závěsu křidélka v
> [0.9, 0.04, 0] otáčejícím se kolem [0, 1, 0]."

a on celou posloupnost provede.

### Proč se rozhraní a agent shodují

Obojí je tenká vrstva nad stejným typovaným jádrem parametrů. Grafické
rozhraní z modelů staví formuláře; MCP server z týchž modelů generuje schémata
nástrojů. Meze, jednotky a popis pole se napíšou jednou a objeví se v obojím.

Praktický důsledek: agent nemůže udělat nic, co by nemohl člověk, a nemůže
vytvořit nastavení, které by rozhraní odmítlo. Oba sdílejí i jeden registr
výpočtů, takže síť vytvořená agentem se otevře v rozhraní a projekt uložený
člověkem si může načíst agent.

---

## 2. Nastavení

### Claude Desktop

Upravte konfigurační soubor MCP:

- **Windows** — `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`

Přidejte:

```json
{
  "mcpServers": {
    "aerothermalstudio": {
      "command": "C:\\AeroThermalStudio\\Run-MCP-Server.bat"
    }
  }
}
```

Nebo volejte Python přímo:

```json
{
  "mcpServers": {
    "aerothermalstudio": {
      "command": "python",
      "args": ["C:\\AeroThermalStudio\\run_app.py", "--mcp"]
    }
  }
}
```

Klienta restartujte. Nástroje by se měly objevit v jeho seznamu.

### Ostatní klienti

Funguje jakýkoli MCP klient. Server mluví JSON-RPC přes stdin/stdout; spusťte
jej příkazem `python run_app.py --mcp`.

### Ověření

Požádejte asistenta, ať zavolá `check_environment`. Měl by vrátit nalezené
nástroje:

```json
{
  "ok": true,
  "os_name": "Windows",
  "cpu_count": 12,
  "recommended_ranks": 10,
  "mpi_launcher": "C:\\Program Files\\Microsoft MPI\\Bin\\mpiexec.exe",
  "su2_cfd": "C:\\Users\\vy\\AppData\\Local\\AeroThermalStudio\\su2\\bin\\SU2_CFD.exe",
  "solver_ready": true
}
```

Je-li `solver_ready` `false`, seznam `missing` říká, co doinstalovat.
Síťování, analytický tepelný model a vykreslování fungují dál.

### Poznámka ke konzolovému oknu

`Run-MCP-Server.bat` otevře okno, které vypadá nečinně. Tak to má být — server
mluví JSON-RPC na standardních proudech, nevypisuje. Nezavírejte jej, dokud
asistent pracuje.

---

## 3. Přehled nástrojů

Všech třináct nástrojů vrací JSON objekt s polem `ok`. Při selhání je
`ok: false` a `error` nese zprávu určenou k jednání.

### 3.1 `set_geometry_and_mesh`

Načte CAD, zarovná jej, postaví oblast a vytvoří síť.

| Parametr | Typ | Výchozí | Poznámka |
|---|---|---|---|
| `step_file_path` | string | `""` | Vynechte a vysíťuje se to, co je načtené v rozhraní |
| `nose_direction` | string | podle tvaru | Od špičky **k zádi**: `+X`, `-X`, `+Y`, `-Y`, `+Z`, `-Z` |
| `nose_vector` | list[3] | null | Libovolný směr, stejný smysl; přepisuje `nose_direction` |
| `reference_origin` | list[3] | `[0,0,0]` | Bod přesunutý do počátku tunelu |
| `domain_multipliers` | object | viz níže | `{"upstream": 5, "downstream": 10, "radial": 5}` |
| `domain_shape` | string | `"cylinder"` | `cylinder` nebo `box` |
| `mesh_resolution` | string | `"medium"` | `coarse`, `medium`, `fine` |
| `track` | string | `"aerodynamic"` | `aerodynamic` nebo `thermal` |
| `boundary_layers` | int | 7 | 5–8 |
| `target_yplus` | float | 45.0 | 30–300 |
| `scale_to_meters` | float | ze souboru | `0.001` pro milimetry |
| `sizing_mach` | float | 1.0 | Režim, pro který se dimenzuje mezní vrstva |
| `sizing_altitude_m` | float | 0.0 | |
| `max_targeting_iterations` | int | 4 | Pokusy o zásah cílového počtu buněk |

Vrací `mesh_id`, `cell_count`, `within_target_band`, `reference_length_m`,
`reference_diameter_m`, `reference_area_m2`, `estimated_yplus`,
`boundary_markers`, `min_quality`, `wall_time_s`, dále `step_file_path`,
`scale_to_meters`, `nose_direction` a poznámky, jak se k posledním dvěma
došlo.

Trvá desítky sekund až několik minut.

**K `nose_direction`.** Je to osa, po které těleso běží *od špičky k zádi*,
protože právě tenhle směr se otáčí na +X, čímž se špička dostane na návětrný
konec tunelu. Raketa nakreslená nastojato, špičkou nahoru, je `-Y`. Parametr
vynechte a odpoví geometrie: nejdelší osa je osa tělesa a konec, který se
zužuje, je špička. Když jsou oba konce stejné, volání raději selže, než aby
hádalo:

```
set_geometry_and_mesh(step_file_path = "C:\\models\\tube.step")
   └─ ok = false
      error   = "which end is the nose? the body lies along X, but both ends
                 are about equally thick (90 and 90) ..."
      needs   = ["nose_direction"]
      axis    = "X"
```

Tu otázku položte operátorovi. Nevybírejte stranu sám: raketa vysíťovaná
pozpátku vrátí kompletní, věrohodnou polární křivku odporu pro stroj letící
zádí napřed a nic dalšího si toho nevšimne.

### 3.2 `run_aerodynamic_simulation`

Spustí jeden bod RANS simulace.

| Parametr | Typ | Výchozí | Poznámka |
|---|---|---|---|
| `mesh_id` | string | — | **Povinné.** Ze `set_geometry_and_mesh` |
| `velocity_type` | string | `"mach"` | `mach` nebo `tas` |
| `velocity_val` | float | 2.0 | Mach 0,05–3,5, nebo m/s |
| `aoa_deg` | float | 0.0 | ±20 |
| `sideslip_deg` | float | 0.0 | ±20 |
| `altitude_m` | float | 0.0 | −610 až 32 000 |
| `hinge_axes` | list[object] | `[]` | `{"name", "point": [x,y,z], "direction": [u,v,w]}` |
| `reference_area_m2` | float | null | Přepíše naměřenou hodnotu |
| `reference_length_m` | float | null | Přepíše naměřenou hodnotu |
| `moment_origin` | list[3] | null | Přepíše výchozí |
| `mpi_ranks` | int | 10 | |
| `max_iterations` | int | 5000 | |
| `convergence_residual` | float | −5.0 | log₁₀ RMS hustoty |
| `turbulence_model` | string | `"SST"` | `SST` nebo `SA` |
| `cfl_number` | float | null | Při null se vybere podle režimu: 5,0 podzvukově, 2,0 transonicky, 1,0 od Mach 1,2 |

Vrací `sim_id`, `mach`, `forces_n` (`fx`/`fy`/`fz`), `drag_n`, `lift_n`,
`sideforce_n`, `coefficients` (`cd`/`cl`/`cs`/`cm_pitch`),
`center_of_pressure`, `hinge_torques`, `iterations`, `converged` a dále
`rescued` s `notes`.

Studený nadzvukový start se může během několika iterací rozpadnout. To se
nyní zachytí okamžitě a výpočet se automaticky zopakuje v prvním řádu a pak
z jeho výsledku restartuje ve druhém. Pokud k tomu došlo, je `rescued` true
a `notes` to vysvětlí — řekněte to v odpovědi a neprezentujte číslo jako
běžné: případ, který potřeboval záchranu, obvykle znamená, že síť je na daný
režim hrubá.

`cfl_number` nechte null, pokud si uživatel výslovně neřekne o konkrétní
hodnotu. Pevných 5,0 při Mach 1,3 je přesně to, kvůli čemu se výpočet
rozpadl na šesté iteraci.

Trvá 3–8 minut na referenčním stroji. Vyžaduje SU2.

### 3.3 `run_sensor_thermal_simulation`

Předpoví údaj BMP580 a jeho chybu.

| Parametr | Typ | Výchozí | Poznámka |
|---|---|---|---|
| `enclosure_step` | string | `""` | Cesta ke CAD, uloží se s výpočtem |
| `mesh_id` | string | null | Vynechte pro analytickou odpověď |
| `vehicle_speed_ms` | float | 2.0 | 0–50 |
| `ambient_temp_c` | float | 25.0 | −50 až 70 |
| `solar_flux_w_m2` | float | 800.0 | 0–1400 |
| `height_above_roof_m` | float | 0.1 | 0,01–2 |
| `sensor_xyz` | list[3] | `[0.03,0.02,0.015]` | Poloha čipu |
| `housing_solar_absorptivity` | float | 0.30 | |
| `housing_emissivity` | float | 0.90 | |
| `roof_solar_absorptivity` | float | 0.65 | |
| `roof_emissivity` | float | 0.85 | |
| `housing_conductivity_w_mk` | float | 0.18 | |
| `sensor_power_w` | float | 0.001 | |
| `analytic_only` | bool | false | Přeskočí CFD |
| `mpi_ranks` | int | 10 | |

Vrací `method`, `sensor_temp_c`, `ambient_temp_c`, `delta_t_error_k`,
`roof_temp_c`, `housing_temp_c`, `tube_mass_flow_kg_s`,
`roof_thermal_boundary_layer_m`, `intake_in_roof_plume`,
`self_heating_rise_k`, `wall_coupling_rise_k`.

**S `analytic_only: true` vrací výsledek v milisekundách a nepotřebuje síť ani
řešič.** To z něj dělá ideální nástroj pro průzkum — agent zvládne v jednom
kroku vyhodnotit desítky variant návrhu.

### 3.4 `generate_cfd_visualization`

Vykreslí obrázek z dokončené simulace.

| Parametr | Typ | Výchozí | Poznámka |
|---|---|---|---|
| `sim_id` | string | — | **Povinné** |
| `visualization_type` | string | `"surface_pressure"` | `surface_pressure`, `mach_slice`, `streamlines`, `thermal` |
| `camera_view` | string | `"isometric"` | `isometric`, `front`, `back`, `side`, `top`, `bottom`, `nose_quarter`, `tail_quarter` |
| `slice_normal` | list[3] | `[0,1,0]` | Normála roviny řezu |
| `colormap` | string | `"turbo"` | `turbo`, `coolwarm`, `viridis`, `jet`, `plasma`, `inferno` |
| `resolution` | string | `"4k"` | `preview`, `hd`, `2k`, `4k` |
| `output_path` | string | null | Výchozí do složky výpočtu |

Vrací `image_path`.

### 3.5 `run_parametric_sweep`

Dávka simulací přes prohledávaný parametr, se sdílenou sítí.

| Parametr | Typ | Výchozí | Poznámka |
|---|---|---|---|
| `mesh_id` | string | — | **Povinné** |
| `param_name` | string | `"mach"` | `mach`, `aoa`, `sideslip`, `altitude` |
| `values` | list[float] | — | **Povinné** |
| `fixed_params` | object | `{}` | Podmínky držené konstantní |
| `hinge_axes` | list[object] | `[]` | Jako výše |
| `mpi_ranks` | int | 10 | |
| `stop_on_error` | bool | false | Přerušit při prvním selhání |

Vrací `sweep_id`, `points`, `curves`, `extremes` (včetně
`servo_sizing_torque_nm`), `succeeded`, `failed`.

Body běží sériově. **Celkový čas je počet bodů krát čas na bod** — šestibodová
studie je půl hodiny. Než ji spustíte, upozorněte uživatele.

### 3.6 `list_runs`

Vypíše uložené sítě, simulace a studie, od nejnovějších.

| Parametr | Typ | Výchozí |
|---|---|---|
| `kind` | string | null (`mesh`, `aero`, `thermal`, `sweep`) |
| `limit` | int | 25 |

Slouží k dohledání `mesh_id` z dřívější relace.

### 3.7 `check_environment`

Vypíše nalezené nástroje. Volejte jako první, když simulace hlásí chybějící
řešič.

### 3.8 `get_active_geometry`

Vrátí — nebo nastaví — CAD soubor, který má operátor právě otevřený
v grafickém rozhraní.

| Parametr | Typ | Výchozí |
|---|---|---|
| `step_file_path` | string | null (jen čte; když je zadán, zapíše ten soubor) |
| `nose_direction` | string | null |
| `scale_to_meters` | float | null (přečte se ze souboru) |

Rozhraní si pamatuje každý STEP, který v něm byl otevřen, včetně jednotky
délky vyčtené ze souboru a celkového rozměru modelu. Volejte tenhle nástroj
dřív, než se operátora zeptáte na cestu: když je něco načteno,
`set_geometry_and_mesh` to vysíťuje úplně bez cesty.

```
get_active_geometry()
   └─ loaded = true
      geometry.step_file_path = "C:\\Users\\...\\Sapphire.step"
      geometry.units = "millimetres"
      geometry.largest_extent_m = 1.32
      geometry.scale_is_confident = true
```

Když je `scale_is_confident` false, řekněte to dřív, než se utratí čas
řešiče: soubor jednotku nedeklaroval, nebo deklaroval takovou, která
neodpovídá jeho vlastní velikosti.

### 3.9 `save_project`

Zapíše kompletní nastavení do souboru `.atsproj`, který otevře grafické
rozhraní.

Přijímá stejné parametry jako síťovací a simulační nástroje, plus `name`,
`path`, `description`, `author`, `notes`, `sweep_parameter`, `sweep_values`,
`thermal` (objekt) a `mesh_id`.

### 3.10 `load_project`

Načte soubor projektu. Přijímá `path`; vrací všechny sekce plus
`last_mesh_id`.

### 3.11 `list_saved_projects`

Vypíše dostupné projekty s jednořádkovým shrnutím. Volitelně `directory`.

### 3.12 `get_settings` / 3.13 `update_settings`

Čtení a změna trvalých předvoleb: `default_mpi_ranks`, `default_colormap`,
`default_render_resolution`, `default_mesh_resolution` a přepínače chování.
`update_settings` mění jen zadaná pole.

---

## 4. Hotové postupy

### 4.1 Jedna simulace

```
check_environment
   └─ ověř solver_ready

set_geometry_and_mesh(
     step_file_path = "C:\\models\\rocket.step",
     nose_direction = "+X",
     mesh_resolution = "coarse")
   └─ mesh_id = "mesh-20260921-101500-a1b2c3"
      cell_count = 252 303, estimated_yplus = 45.0

run_aerodynamic_simulation(
     mesh_id = mesh_id,
     velocity_val = 2.0,
     aoa_deg = 5.0,
     hinge_axes = [{"name": "fin_1",
                    "point": [0.9, 0.04, 0.0],
                    "direction": [0, 1, 0]}])
   └─ sim_id, cd = 0,42, cl = 0,25, moment na závěsu = 0,084 N·m

generate_cfd_visualization(
     sim_id = sim_id,
     visualization_type = "mach_slice",
     camera_view = "side")
   └─ image_path
```

### 4.2 Návrh serva

```
set_geometry_and_mesh(...)              jednou

run_parametric_sweep(                   najdi nejhorší Mach
     mesh_id = mesh_id,
     param_name = "mach",
     values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
     fixed_params = {"aoa_deg": 10},
     hinge_axes = [...])
   └─ extremes.servo_sizing_torque_nm

run_parametric_sweep(                   zpřesni při tom Machu
     mesh_id = mesh_id,
     param_name = "aoa",
     values = [0, 5, 10, 15, 20],
     fixed_params = {"velocity_val": <nejhorší Mach>},
     hinge_axes = [...])
   └─ skutečný vrchol
```

Uveďte vrchol s doporučenou rezervou a řekněte, který režim jej způsobil.

### 4.3 Průzkum návrhu senzoru

Analytický režim je dost rychlý na prozkoumání návrhového prostoru v jednom
kroku:

```
run_sensor_thermal_simulation(analytic_only = true,
                              housing_solar_absorptivity = 0.1)
run_sensor_thermal_simulation(analytic_only = true,
                              housing_solar_absorptivity = 0.3)
run_sensor_thermal_simulation(analytic_only = true,
                              housing_solar_absorptivity = 0.9)
run_sensor_thermal_simulation(analytic_only = true,
                              height_above_roof_m = 0.05)
run_sensor_thermal_simulation(analytic_only = true,
                              vehicle_speed_ms = 0.0)
```

Pak předložte kompromisy. Vždy zahrňte **stojící** případ: tramvaj na
zastávce na plném slunci je nejhorší případ a uživatelé na něj zapomínají.

### 4.4 Předání práce člověku

```
save_project(
     name = "Navrh krideleku",
     description = "Machova studie pro vyber serva",
     notes = "Vrchol momentu 0,084 N·m pri Mach 2,5 a 10 deg. Doporucuji servo 0,2 N·m.",
     step_file_path = "C:\\models\\rocket.step",
     hinge_axes = [...],
     sweep_parameter = "mach",
     sweep_values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
     mesh_id = mesh_id)
```

Sdělte uživateli cestu. Otevře ji přes **File → Open project** a uvidí přesně
to nastavení, které jste použili.

---

## 5. Jak psát zadání

**Buďte konkrétní ohledně geometrie a natočení.** Směr špičky je nastavení,
které se nejčastěji plete, a chybná odpověď vypadá věrohodně.

> „Vysíťuj `C:\models\rocket.step`. Špička míří podél **+Z** v CAD souboru a
> model je v **milimetrech**."

**Řekněte, co chcete zjistit, ne jen co spočítat.**

> „Potřebuju vybrat servo pro klopná křidélka. Najdi nejhorší moment na závěsu
> přes celou letovou obálku: Mach 0,5 až 3, úhel náběhu do 15°."

**U senzoru dodejte fyzický kontext.**

> „BMP580 v bílé PETG krabičce, 15 cm nad střechou tramvaje, odběrová trubička
> dopředu. Letní poledne, tramvaj jede 5 m/s, ale často stojí. O kolik to
> přestřelí a co mám změnit?"

**Ptejte se na odůvodnění.**

> „Ukaž mi poláru odporu a řekni, kde začíná nárůst odporu."

---

## 6. Chybové stavy

Nástroje vracejí strukturované chyby, nevyhazují výjimky. Nejčastější:

| Chyba obsahuje | Význam | Náprava |
|---|---|---|
| `SU2_CFD was not found` | Řešič není nainstalován | `check_environment`, pak instalace |
| `needs an MPI launcher` | Chybí MPI pro paralelní běh | Nainstalovat MS-MPI, nebo `mpi_ranks: 1` |
| `invalid parameters` | Hodnota mimo meze | Přečtěte zprávu; pojmenuje pole |
| `no record` / `not a mesh` | Špatné `mesh_id` | `list_runs` a najít platné |
| `STEP file not found` | Špatná cesta | Zkontrolovat; cesty ve Windows potřebují v JSON zdvojená zpětná lomítka |
| `no solid volumes` | CAD jen z ploch | Model musí být sešitý do tělesa |
| `not a closed manifold` | Ostrý prvek v CAD | Přidat malé zaoblení na břity a hroty |
| `mesh file not found` | Síť smazána | Vytvořit znovu |

Dobrý agent zprávu přečte a zareaguje, místo aby volání opakoval.

---

## 7. Poznámky pro autory agentů

**Doby běhu se enormně liší.**

| Operace | Typicky |
|---|---|
| `run_sensor_thermal_simulation` s `analytic_only` | milisekundy |
| `check_environment`, `list_runs`, nástroje projektů | milisekundy |
| `generate_cfd_visualization` | sekundy |
| `set_geometry_and_mesh` | 30 s – 3 min |
| `run_aerodynamic_simulation` | 3–8 min |
| `run_parametric_sweep` | body × 3–8 min |

Před čímkoli dlouhým uživatele upozorněte. Šestibodová studie je půl hodiny;
spustit ji bez varování je nepříjemné.

**Znovu používejte síť.** Síťování je drahé a nezávisí na letovém režimu —
úhel náběhu a Mach aplikuje řešič. Jedna síť poslouží celé studii. Novou
vytvářejte jen při změně geometrie, oblasti nebo nastavení sítě.

**Začínejte hrubě.** Rozlišením `coarse` ověřte nastavení, pak zjemněte.
Zjistit špatný směr špičky až po síti `fine` je ztráta minut.

**Kontrolujte `within_target_band`.** Je-li `false`, generátor se nedostal na
požadovaný počet buněk. Síť je použitelná, ale odhad doby běhu nebude sedět.

**Kontrolujte `converged`.** Nezkonvergovaný výsledek hlaste jako takový,
nepředkládejte jej jako fakt.

**Nevymýšlejte si působiště.** `NaN` při nulovém náběhu je správné a smysluplné
— bez příčné síly působiště skutečně definováno není. Navrhněte místo toho
spuštění při 2–5°.

**Než výsledku uvěříte, ověřte jej.** Jeden CFD výpočet není ověřená odpověď.
U čehokoli důležitého doporučte kontrolu nezávislosti na síti a řekněte
otevřeně, když výsledek ověřený nebyl.

---

## 8. Meze a soudnost

**Obálka platnosti.** Mach 0,05–3,5, úhly do ±20°, výška −610 až 32 000 m. Za
±20° se proudění kolem štíhlého tělesa masivně odtrhává a ustálené RANS řešení
přestává mít smysl — číslo by se stále objevilo a bylo by chybné.

**Sdružená tepelná větev je méně prověřená** než analytický model. Cesta
síťování pevné oblasti nebyla spuštěna proti skutečnému vícezónovému výpočtu
SU2. Analytický model je dobře otestovaný a uzavření jeho energetických bilancí
je ověřeno.

**Záření je linearizováno** mezi vnějšími iteracemi, není plně sdružené.

**Rozlišení křidélek je heuristika** podle radiálního dosahu. Vypisuje se ve
zprávě o síti, aby se dalo zkontrolovat.

**CFD je model.** Dává přesné odpovědi na položenou otázku, která není vždy ta
zamýšlená. Modely turbulence jsou korelace; rozlišení sítě mění výsledky;
okrajové podmínky obsahují předpoklady. Předkládejte výsledky s tímto
kontextem, ne jako měření.

---

## 9. Vestavěný asistent

Těchto třináct nástrojů pohání i asistenta **přímo v programu**, v záložce
**AI Assistant**. Je pro obsluhu, která chce napsat požadavek a nechat ho
provést, aniž by vůbec spouštěla externího MCP klienta.

### V čem se liší od MCP klienta

| | MCP server | Vestavěný asistent |
|---|---|---|
| Spouští | Externí klient (Claude Desktop, agentní framework) | Záložka AI Assistant |
| Přenos | stdio, protokol MCP | HTTP API OpenRouteru kompatibilní s OpenAI |
| Model | Ten, který běží v klientovi | Libovolné id modelu z OpenRouteru, volené v záložce |
| Pověření | Vlastní klientova | Klíč OpenRouteru, uložený programem |
| Schémata nástrojů | `server.list_tools()` | Totéž volání, převedené do tvaru funkcí OpenAI |

Obě rozhraní míří do stejných funkcí: `mcp_server.TOOL_FUNCTIONS` mapuje
jméno každého registrovaného nástroje na funkci, kterou volá i MCP handler, a
`mcp_server.call_tool(name, arguments)` ji zavolá přímo — vrací stejný
strukturovaný výsledek `{"ok": ...}` místo výjimky. Test ověřuje, že rejstřík
a seznam registrovaných nástrojů jsou totožné, takže nástroj nemůže být
dostupný na jednom rozhraní a chybět na druhém.

### Smyčka agenta

`backend.ai_agent.AIAssistant.ask()` točí obvyklou smyčku volání nástrojů:
pošli konverzaci se schématy nástrojů, proveď volání, která model vrátil,
přidej každý výsledek jako zprávu typu `tool` a opakuj, dokud model
neodpoví textem nebo se nevyčerpá strop kol. Přenos je schovaný za rozhraním
`ChatClient`, takže `FakeChatClient` přehrává nascénované konverzace a celá
smyčka je testovaná bez sítě a bez klíče.

Dvě chování jsou záměrná. `mcp_server.LONG_RUNNING_TOOLS` jmenuje tři
nástroje, které zaberou stroj na minuty až hodiny; před každým se program
ptá schvalovací funkce a odmítnuté volání se modelu ohlásí jako odmítnutí,
které nemá beze změny opakovat. Strop kol (`ai_max_tool_rounds`, výchozí 12)
omezuje, co může jeden požadavek utratit.

### Jeho systémový prompt

Asistent má v instrukcích to, co model jinak v téhle úloze dělá špatně:
používat jedno `mesh_id` napříč simulacemi, protože síťování nezávisí na
letovém režimu; říct dopředu, co daný krok bude stát; hlásit
`converged: false` a `within_target_band: false` místo předkládání čísla jako
hotové věci; brát NaN u působiště při nulovém úhlu náběhu jako správný
výsledek a navrhnout místo toho 2–5°; a na rovinu říct, že neumí spouštět
příkazy shellu ani číst libovolné soubory.

---

## Viz také

- **[TUTORIAL.md](TUTORIAL.md)** — úvod krok za krokem
- **[USER_GUIDE.md](USER_GUIDE.md)** — přehled všech nastavení
- **[../../README.md](../../README.md)** — architektura a implementace
