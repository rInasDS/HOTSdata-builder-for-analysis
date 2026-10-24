# HOTSdata-builder-for-analysis
parses usefull data from Heroes of the Storm for analyzing games from .StormReplay

## Requirements & Dependencies

### Environment

- **OS:** Windows 10/11
- **Language:** Python 3.12+
- **Replay Decoder:** [HeroesDecode](https://github.com/HeroesToolChest/HeroesDecode)
- **Runtime:** .NET SDK/runtime required by the selected HeroesDecode release
- **Version Control:** Git

### External Resources

- **Heroes of the Storm GameData:** https://github.com/jamiephan/HeroesOfTheStorm_Gamedata
- **HeroesDecode:** https://github.com/HeroesToolChest/HeroesDecode
- **HeroesDecode Releases:** https://github.com/HeroesToolChest/HeroesDecode/releases
- **Static Map Resources (S2MA):** https://github.com/jamiephan/HeroesOfTheStorm_S2MA
- **MPQ Archive Tool Reference:** https://github.com/HeroesToolChest/Heroes.MpqTool

### Setup

1. Install Python and Git.
2. Install Python dependencies from `requirements.txt`.
3. Install the .NET runtime or SDK required by the selected HeroesDecode release.
4. Obtain the required GameData version and S2MA map resources. [READ ME HOW](./parsers_db/README.md)
5. Configure source-data and output paths.
6. Run the database builders and check their validation reports.

### Output

Generated databases are stored in `db4anal` and are intended for use by downstream replay analysis tools.

### Compatibility

- Use GameData matching the target Heroes of the Storm build.
- Ensure that the selected HeroesDecode release supports the replay format.
- Use the project's dependency files as the source of truth for Python package requirements.
- GameData and S2MA archives are external source resources, separate from the generated databases.

### Tested
- Game versions: 97771 and 98025
- Current databases coverage: 91% of hero abilities and map data

### TO DO
- Bush/mud geometry and vision in bushes
- Shield & Heal: need fix for certain abilities
- Core abilities: maps
- Test new game version with Xal'atath

---
