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
**šestnáct nástrojů** pokrývajících vše, co umí program: import CAD, síťování,
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

Všech šestnáct nástrojů vrací JSON objekt s polem `ok`. Při selhání je
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
| `body_kind` | string | podle tvaru | `rocket` (nejdelší strana přes 3,5× ostatní) nebo `fin` (tenká deska; vztaženo k hloubce a půdorysu) |
| `pitch_axis` | string | null | Osa v CAD, kolem které se model naklápí při úhlu náběhu, např. `+Z`; musí být napříč proudem |
| `sizing_altitude_m` | float | 0.0 | |
| `max_targeting_iterations` | int | 4 | Pokusy o zásah cílového počtu buněk |

Vrací `mesh_id`, `cell_count`, `within_target_band`, `reference_length_m`,
`reference_diameter_m`, `reference_area_m2`, `estimated_yplus`,
`boundary_markers`, `min_quality`, `poor_prism_count`, `wall_time_s`, dále `step_file_path`,
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
| `velocity_val` | float | 2.0 | Holé číslo: Mach 0,05–3,5, nebo m/s. Nikdy `"250 m/s"`; km/h převést (÷ 3,6), uzly (× 0,5144) |
| `aoa_deg` | float | 0.0 | ±90; nad ±20 jen orientační |
| `sideslip_deg` | float | 0.0 | ±90; nad ±20 jen orientační |
| `altitude_m` | float | 0.0 | −610 až 32 000 |
| `hinge_axes` | list[object] | `[]` | `{"name", "point": [x,y,z], "direction": [u,v,w], "frame": "rocket"}` — osy rakety, pokud `frame` není `"solver"` |
| `reference_area_m2` | float | null | Přepíše naměřenou hodnotu |
| `reference_length_m` | float | null | Přepíše naměřenou hodnotu |
| `moment_origin` | list[3] | null | Přepíše výchozí |
| `pivot_height_m` | float | null | Posune osu naklápění nahoru (+) / dolů (−) od osy tělesa vůči proudu při nulovém AoA; momenty se berou k ní. I u `preview_orientation` a sweepu |
| `mpi_ranks` | int | 10 | |
| `max_iterations` | int | 5000 | |
| `convergence_residual` | float | −5.0 | log₁₀ RMS hustoty |
| `turbulence_model` | string | `"SST"` | `SST` nebo `SA` |
| `cfl_number` | float | null | Počáteční CFL; při null podle režimu: 5,0 pod Mach 0,6, 2,0 do 1,2, 1,0 nad |
| `cfl_growth` | float | null | Růst adaptivního CFL za iteraci, 1,0–3,0; výchozí podle režimu 1,15 / 1,10 / 1,05 |
| `cfl_max` | float | null | Strop adaptivního CFL; výchozí podle režimu 100 / 50 / 25 |
| `convective_scheme` | string | null | `JST`, `ROE`, `AUSM`, `HLLC`; null volí JST pod Mach 0,8, Roe nad |

Vrací `sim_id`, `mach`, `axis_convention`, `forces_rocket_frame_n`
(`fx`/`fy`/`fz`), `forces_solver_frame_n`, `drag_n`, `lift_n`,
`sideforce_n`, `coefficients` (`cd`/`cl`/`cs`/`cm_pitch`),
`center_of_pressure_rocket_frame`, `center_of_pressure_solver_frame`,
`hinge_torques`, `iterations`, `converged` a dále `rescued` s `notes`.

**Dvě soustavy.** *Soustava rakety* je ta, kterou vidí operátor: špička podél
+Z, počátek v referenčním bodě. Odpor rakety letící špičkou napřed je záporné
`fz`; vztlak od kladného úhlu náběhu je `+fx`; vybočení dává `fy`. *Soustava
řešiče* je soustava sítě — těleso podél +X, proud podél +X — a je přiložena
pro úplnost. Liší se pevnou rotací: x_raketa = z_řešič, y_raketa = y_řešič,
z_raketa = −x_řešič. Lidem uvádějte soustavu rakety. Body a směry závěsů se
na vstupu také čtou v soustavě rakety, pokud osa neříká `"frame": "solver"`.

Studený nadzvukový start se může během několika iterací rozpadnout. To se
nyní zachytí okamžitě a výpočet se automaticky zopakuje v prvním řádu a pak
z jeho výsledku restartuje ve druhém. Pokud k tomu došlo, je `rescued` true
a `notes` to vysvětlí — řekněte to v odpovědi a neprezentujte číslo jako
běžné: případ, který potřeboval záchranu, obvykle znamená, že síť je na daný
režim hrubá.

`cfl_number` nechte null, pokud si uživatel výslovně neřekne o konkrétní
hodnotu. Pevných 5,0 při Mach 1,3 je přesně to, kvůli čemu se výpočet
rozpadl na šesté iteraci. Samotné nízké `cfl_number` z výpočtu opatrný
neudělá: kam CFL dojde, rozhoduje adaptivní rampa, takže snižte `cfl_max`
a `cfl_growth` nechte kolem 1,05. Když se rozpadne i záchrana, další
přesíťování obvykle nepomůže; zkontrolujte `min_quality` sítě a pak zkuste
`convective_scheme: "ROE"` s nízkým `cfl_max`.

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
| `sim_id` | string | — | **Povinné**. Funguje i bod sweepu, jak ho sweep vypíše (`<sweep_id>-003`): každý bod si řešení ponechává |
| `visualization_type` | string | `"surface_pressure"` | `surface_pressure`, `mach_slice`, `schlieren`, `streamlines`, `thermal` |
| `camera_view` | string | `"isometric"` | `isometric`, `front`, `back`, `side`, `top`, `bottom`, `nose_quarter`, `tail_quarter` |
| `slice_normal` | list[3] | `[0,1,0]` | Normála roviny řezu |
| `colormap` | string | `"turbo"` | `turbo`, `coolwarm`, `viridis`, `jet`, `plasma`, `inferno` |
| `resolution` | string | `"4k"` | `preview`, `hd`, `2k`, `4k` |
| `output_path` | string | null | Výchozí do složky výpočtu; zadejte cestu pro export jinam |
| `frame` | string | null | `rocket` (špička podél +Z) nebo `solver`; null znamená `rocket` pro aerodynamické výpočty a `solver` pro tepelné |

Vrací `image_path`, `frame` a v soustavě rakety také `axis_convention`.

Obrázky zapsané do složky výpočtu se objeví v záložce **Graphics** programu,
kde si je operátor prohlédne a vyexportuje; obrázek vykreslený vestavěným
asistentem se ukáže i přímo v jeho konverzaci. Každý nese titulek s režimem,
Machovým číslem, úhlem náběhu a výpočtem. Machův řez se ořízne na raketu a tři
čtvrtiny délky tělesa kolem ní; pro přímý pohled na něj použijte
`camera_view: "side"`.

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

### 3.14 `preview_orientation`

Nakreslí rychlý obrázek modelu v nízkém rozlišení (pohled z boku a šikmo)
s přicházejícím vzduchem jako modrými šipkami a osou naklápění jako oranžovou
tyčí s obloukovou šipkou — přesně tak, jak by je použila síť nebo výpočet se
stejným nastavením. Bere geometrické parametry `set_geometry_and_mesh` (nebo
`mesh_id`) a `aoa_deg`, `sideslip_deg`; vrací `image_path`.

Ve vestavěném asistentovi je povinný: nastavení, které uživatel ještě
neviděl — nový soubor, směr špičky, typ tělesa, osa naklápění nebo nový úhel —
`set_geometry_and_mesh`, `run_aerodynamic_simulation` i `run_parametric_sweep`
odmítnou, dokud nebylo ukázáno **a uživatel neodpověděl**. Asistent po
náhledu ukončí tah; další zpráva uživatele je odpověď a opravené nastavení se
ukáže znovu. Externí MCP klienti omezeni nejsou.


### 3.15 `radiation_shield_study`

Studie radiačního štítu (stínítka teploměru) podle referenční metodiky
(Atmosphere 2026, 17(3), 272, rozšířené o střechu vozidla): Design of
Experiments přes **rychlost větru na vstupu**, **sluneční záření shora** a
**záření zdola**, **response surface** proložená spočtenými body a
**Monte Carlo** analýza na této ploše. Cílová veličina je
`T_monitor - T_inlet` v bodě teploměru.

| Parametr | Typ | Poznámka |
|---|---|---|
| `action` | string | `analytic` (okamžitý orientační model), `sweep` (model spočtený přímo po pevných krocích jednoho vstupu), `preview` (obrázek štítu, zatížení a domény), `prepare_cfd` (síť domény, paprsky pro radiaci, SU2 případy a balíček pro Fluent/Workbench), `solve_cfd` (SU2 na design pointech studie `study_id`; dlouhé), `import` (analýza CSV spočteného jinde), `export_fluent`, `status` |
| `study_id` | string | `shield-...`; potřeba pro `solve_cfd`, `export_fluent`, `status` |
| `setup` | objekt | Cokoli z: `shield_step_path` (prázdné = vestavěný 20cm vícedeskový štít), `scale_to_meters`, `shield_size_m`, `plate_count`, `domain_size_m` (výchozí `[2.0, 2.0, 1.44]`), `thermometer_xyz_m` (vůči středu štítu), `ambient_temp_c`, `wind_speed_ms`, `solar_flux_w_m2` (1000), `bottom_mode` (`ground_flux` nebo `roof_temperature`), `bottom_flux_w_m2` (300), `bottom_temperature_k` (343 = 70 °C), `roof_emissivity`, `sky_longwave_w_m2`, `ground_albedo`, `shield_solar_absorptivity`, `shield_emissivity`, `shield_conductivity_w_mk`, `ventilation_coefficient` |
| `variables` | objekt | Pro každý vstup (`wind_speed_ms`, `solar_flux_w_m2`, `bottom_flux_w_m2`): `minimum`, `maximum`, `distribution` (`uniform`/`normal`/`triangular`), `mean`, `std`, `mode`, `scale` (`linear`/`log`). Výchozí 0,5–5 m/s (log), 800–1200 W/m², 300–800 W/m², rovnoměrně |
| `study` | objekt | `doe` (`ccd` face-centred, 15 bodů; `lhs`), `doe_points`, `surrogate` (`quadratic`, `rbf`), `monte_carlo_samples` (10 000), `tolerance_k` (0,5), `seed` |
| `cfd` | objekt | `mesh_resolution`, `turbulence_model` (`SST`/`SA`), `buoyancy`, `radiation_passes`, `iterations_first_pass`, `iterations_later_passes`, `radiation_classes`, `rays_per_face`, `mpi_ranks` |
| `results_csv` | string | Pro `import`: `design_points.csv` s vyplněným `delta_t_k` (nebo `monitor_temp_k`), nebo export design pointů z Workbenche |
| `max_points` | int | Pro `solve_cfd`: nejvýše tolik bodů na jedno volání |
| `sweep_variable`, `sweep_start`, `sweep_stop`, `sweep_step` | string, čísla | Pro `sweep`: `wind_speed_ms`, `solar_flux_w_m2` nebo `bottom_flux_w_m2`, od, do a krok (např. 0,2; 5; 0,2). Ostatní vstupy zůstanou na výchozích hodnotách `setup` |

Vrací design pointy, kvalitu plochy (`r_squared`, `leave_one_out_rmse_k`) a
souhrn Monte Carlo: průměr, rozptyl, percentily, **nejhorší případ i s jeho
vstupy**, **spolehlivost** P(|dT| ≤ tolerance) a citlivosti (pořadová
korelace).

**Jak SU2 větev modeluje radiaci.** Nestlačitelný řešič SU2 nemá DO/S2S model,
takže radiace se počítá mimo něj: paprsky z každé plošky štítu zjistí, kam
dopadá slunce (lamely se navzájem stíní) a kolik oblohy a země ploška vidí;
pohlcený tok mínus vlastní vyzařování plošky se zadá jako okrajová podmínka
stěny, seskupená do několika markerů. Vyzařování (čtvrtá mocnina teploty) se
linearizuje kolem teploty stěny (SU2 `MARKER_HEATTRANSFER`,
h_r·(T_eq − T_stěna)), takže ho SU2 řeší implicitně; druhý průchod
linearizuje kolem spočtené teploty a obvykle ji změní o méně než 0,01 K. Vedení tepla ve štítu se
neřeší (tenké plastové desky); SU2 nemá standardní k-ε, používá se SST.
Balíček pro Fluent se drží zadání přesně: standardní k-ε, DO radiace, pevná
zóna štítu.

**Sweepy a obrázky.** `sweep` odpovídá na otázku „co dělá jeden vstup“ po
zadaných krocích: každý řádek je přímo analytický model, vrací se jako
`rows` a ukládá jako `sweep.csv` s grafem (`image_path`). Tabulku po pevných
krocích nikdy nečtěte z response surface — kvadratická plocha proložená 15
body si vymýšlí minima a mimo svůj rozsah nic neznamená. `preview` (a také
`analytic`/`sweep` jako `geometry_image_path`) nakreslí geometrii; panel
asistenta ukáže oba obrázky.

**Výpočet jinde.** `prepare_cfd` zapíše `run_design_points.bat`/`.sh`, který
spočte všechny body na počítači, kde se spustí, a složku `fluent/` s
umístěným štítem a STEP souborem tekutiny, `design_points.csv`, journalem pro
Fluent a README pro Workbench a DesignXplorer (CCD, response surface, Six
Sigma Monte Carlo). Spočtené body se vrací přes `import`.


### 3.16 `sps30_housing_study`

Studie krytu prachového senzoru SPS30: zapuštěné statické štěrbiny po obou
stranách, plenum, které vzduch zpomalí, a přepážka, kolem které vzduch
zatočí, ale kapky vody do ní narazí. DoE přes **rychlost platformy**,
**stočení (yaw, boční vítr)** a **průměr kapek**, response surface pro každý
výstup a Monte Carlo. Výstupy: `face_velocity_ms` (největší rychlost vzduchu
2 mm před čelem senzoru, cíl < 1 m/s), `penetration` (podíl kapek, které se
dostaly do krytu a doletěly na čelo senzoru, cíl 0) a `exchange_flow_lpm`
(výměna vzduchu přes komoru senzoru).

| Parametr | Typ | Poznámka |
|---|---|---|
| `action` | string | `analytic`, `sweep`, `preview`, `prepare_cfd`, `solve_cfd`, `import`, `export_fluent`, `status` |
| `study_id` | string | `sps30-...` |
| `setup` | objekt | `housing_step_path` (prázdné = vestavěný kryt 120 × 70 × 80 mm), `scale_to_meters`, `sensor_face_center_m`, `sensor_face_normal`, `sensor_face_size_m`, `chamber_plane_x_m`, `fan_enabled`, `fan_flow_lpm`, `forward_axis`, `domain_multipliers`, `ambient_temp_c`, `water_density_kg_m3`, výchozí `speed_ms`/`yaw_deg`/`droplet_um`, rozměry pro zjednodušený model (`port_area_m2`, `port_width_m`, `plenum_area_m2`, `baffle_gap_m`, `chamber_area_m2`, `chamber_fraction`), cíle `max_face_velocity_ms`, `max_penetration`, `min_exchange_flow_lpm` |
| `variables` | objekt | `speed_ms` (5–35), `yaw_deg` (−20..20), `droplet_um` (10–2000, log): `minimum`, `maximum`, `distribution`, `mean`, `std`, `mode`, `log` |
| `study` | objekt | `doe` (`ccd`/`lhs`), `doe_points`, `surrogate` (`rbf`/`quadratic`), `monte_carlo_samples`, `seed` |
| `cfd` | objekt | `mesh_resolution`, `iterations`, `mpi_ranks`, `droplets`, `random_walk`, `seed` |
| `results_csv` | string | Pro `import` — vyplněný `design_points.csv`, nebo export z Workbenche (`speed`, `yaw`, `droplet_diameter`, `face_velocity`, `sensor_trap_count`, `injected`, `ux_integral`) |
| `max_points` | int | Pro `solve_cfd` |
| `sweep_variable`, `sweep_start`, `sweep_stop`, `sweep_step` | string, čísla | Pro `sweep`: `speed_ms`, `yaw_deg` nebo `droplet_um`, od, do a krok |

Vrací spolehlivost (podíl náhodných podmínek, které splní všechny cíle),
podíl pro každý cíl, nejhorší nevyhovující podmínku a pro každý výstup
statistiky, nejhorší případ, citlivosti a chybu leave-one-out.

`sweep` a `preview` fungují jako u štítu; `preview` ukáže kryt zvenku a v
řezu (štěrbiny, plenum, přepážka, komora senzoru, sání SPS30 s
ventilátorem, odvodňovací otvor).

**Jak SU2 větev řeší kapky.** SU2 nemá model diskrétní fáze. Vzduch se spočte
s SST k-ω (podle zadání); program pak kapky sleduje v tomto poli sám —
odpor Schiller–Naumann, gravitace a náhodná procházka (random walk) z k a ω —
a každou zastaví na stěně, na kterou narazí („trap“). Balíček pro Fluent
používá DPM Fluentu se stejnými podmínkami na stěnách.

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
   └─ mesh_id = "mesh-20260921-101500"
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

**Obálka platnosti.** Mach 0,05–3,5, úhly do ±90°, výška −610 až 32 000 m. Za
±20° se proudění kolem štíhlého tělesa masivně odtrhává a ustálené RANS řešení
je jen orientační — výpočet proběhne a poznámky to uvedou.

**Formát rychlosti pro agenty.** Rychlost je vždy `velocity_type` plus holé
číslo ve `velocity_val`: `"mach"` s `0.8`, nebo `"tas"` s `250` (m/s).
Řetězce jako `"250 m/s"` nebo `"M0.8"` jsou odmítnuty.

**Pant.** `pivot_height_m` posune osu naklápění nahoru (+) nebo dolů (−) vůči
proudu při nulovém AoA; „nahoru“ je strana, kam kladný úhel náběhu model
zvedá. Proudění se nemění — řešič naklání vzduch, ne model — mění se jen bod,
ke kterému se berou momenty. Obrácení smyslu klopení: otočit znaménko `pitch_axis`.

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

Těchto šestnáct nástrojů pohání i asistenta **přímo v programu**, v záložce
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
