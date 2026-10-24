Create the following folders inside `C:\parsers_db` before running the extraction commands.

```text
C:\parsers_db\
├── raw_xml\
├── MAP-ASSETS\
├── S2MA\
├── HDP-MAP\
└── stormreps\
```


### 1. raw_xml

Extract raw game data using **heroes-data-parser**.

```powershell
dotnet heroes-data-parser casc-extract game --storage-path "C:\Program Files (x86)\Heroes of the Storm" -i ":hdp:" -o "C:\parsers_db\raw_xml"
```

**Output:** `C:\parsers_db\raw_xml`

---

### 2. MAP-ASSETS

Extract map files, terrain, pathing, and cliff assets.

```powershell
dotnet heroes-data-parser casc-extract game `
  -s "C:\Program Files (x86)\Heroes of the Storm" `
  -o "C:\parsers_db\MAP-ASSETS" `
  -i "**/*.stormmap/**" `
  -i "**/base.stormmaps/**" `
  -i "**/*terrain*" `
  -i "**/*pathing*" `
  -i "**/*cliff*"
```

**Output:** `C:\parsers_db\MAP-ASSETS`

---

### 3. S2MA

Extract `.s2ma` files from the game's depot cache.

```powershell
dotnet heroes-data-parser casc-extract game `
  -s "C:\Program Files (x86)\Heroes of the Storm" `
  -o "C:\parsers_db\S2MA" `
  -i "**/depotcache/**/*.s2ma"
```

**Output:** `C:\parsers_db\S2MA`

---

### 4. HDP-MAP

Extract map-specific game data in JSON format.

```powershell
dotnet heroes-data-parser game `
  -s "C:\Program Files (x86)\Heroes of the Storm" `
  -e map `
  --map-specific-json-output All `
  -o "C:\parsers_db\HDP-MAP"
```

**Output:** `C:\parsers_db\HDP-MAP`

---

### 5. STORM REPLAYS

Add **at least 200 `.StormReplay` files** to the `stormreps` folder.

**Output:** `C:\parsers_db\stormreps`

> [!IMPORTANT]
> **Game Version Compatibility**
>
> The Heroes of the Storm game version used for data extraction must match the version of the replay files.
>
> Make sure `raw_xml`, `MAP-ASSETS`, `S2MA`, and `HDP-MAP` are extracted from the same game version as the `.StormReplay` files to avoid compatibility issues.


**Once all steps are complete, the dataset is ready for further processing.**
