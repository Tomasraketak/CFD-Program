# AeroThermalStudio — Documentation / Dokumentace

Documentation is available in English and Czech.
Dokumentace je k dispozici anglicky a česky.

---

## English

| Document | What it covers |
|---|---|
| **[Tutorial](en/TUTORIAL.md)** | Step-by-step course: installation, your first simulation, fin hinge torque, sweeps, visualisation, the BMP580 sensor case, projects, the built-in AI assistant, and AI control. **Start here.** |
| **[User Guide](en/USER_GUIDE.md)** | Reference for every setting: what it does, how to choose a value, and the physics behind it — including the AI assistant and its API key. |
| **[Radiation Shield Study Guide](en/SHIELD_STUDY_GUIDE.md)** | A thermometer radiation shield from scratch, for someone who has never done CFD: this program (analytic and SU2) and Ansys Student (Fluent, Workbench, DesignXplorer) step by step, and how to get the most accurate data. |
| **[MCP and AI Guide](en/MCP_AI_GUIDE.md)** | Driving the platform from an AI assistant: setup, all fifteen tools, worked workflows, agent guidance and how the built-in assistant uses the same tools. |
| **[README](../README.md)** | Architecture, implementation notes and measured results. |

### Quick start

```
Setup.bat                 install and check
Run-Demo.bat              verify it works, no solver needed
AeroThermalStudio.bat     start the program
```

---

## Česky

| Dokument | O čem je |
|---|---|
| **[Tutoriál](cs/TUTORIAL.md)** | Návod krok za krokem: instalace, první simulace, moment na závěsu křidélka, parametrické studie, vizualizace, případ senzoru BMP580, projekty, vestavěný AI asistent a ovládání pomocí AI. **Začněte zde.** |
| **[Uživatelská příručka](cs/USER_GUIDE.md)** | Přehled všech nastavení: co dělají, jak volit hodnoty a fyzika za nimi — včetně AI asistenta a jeho API klíče. |
| **[Studie radiačního štítu](cs/SHIELD_STUDY_GUIDE.md)** | Radiační štít teploměru od nuly, pro člověka, který nikdy nedělal CFD: tento program (analyticky i SU2) a Ansys Student (Fluent, Workbench, DesignXplorer) krok za krokem, a jak získat co nejpřesnější data. |
| **[MCP a AI](cs/MCP_AI_GUIDE.md)** | Ovládání z AI asistenta: nastavení, všech patnáct nástrojů, hotové postupy, rady pro agenty a jak tytéž nástroje používá vestavěný asistent. |
| **[README](../README.md)** | Architektura, poznámky k implementaci a naměřené výsledky (anglicky). |

### Rychlý start

```
Setup.bat                 instalace a kontrola
Run-Demo.bat              ověření funkčnosti, bez řešiče
AeroThermalStudio.bat     spuštění programu
```

---

## Note on language / Poznámka k jazyku

The code, its comments and the technical README are in English, because CFD
and MCP terminology is English and the settings in the program are labelled in
English. The tutorial and guides exist in both languages, with Czech
translations giving the English term alongside so the two can be read
together.

Kód, komentáře a technické README jsou anglicky, protože terminologie CFD a
MCP je anglická a názvy nastavení v programu jsou anglicky. Tutoriál a
příručky existují v obou jazycích; české verze uvádějí anglický termín vedle
českého, aby se daly číst společně.
