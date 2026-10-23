from map_context import definition_allowed, MAP_CONTEXT_DEPENDENCY_EVIDENCE


def bg(mod):
    return {
        'file': rf'C:\mods\heroesmapmods\battlegroundmapmods\{mod}\base.stormdata\gamedata\unitdata.xml',
        'mod': mod,
    }


def test():
    expected = {
        'AlteracPass': {'warcraftdata.stormmod'},
        'BattlefieldOfEternity': {'heavenhell.stormmod'},
        'BraxisHoldout': {'starcraft.stormmod'},
        'BraxisOutpost': {'braxisholdoutdata.stormmod', 'starcraft.stormmod'},
        'IndustrialDistrict': {'volskayadata.stormmod', 'overwatchdata.stormmod', 'conveyorbelts.stormmod'},
        'Shrines': {'heavenhell.stormmod'},
        'Volskaya': {'overwatchdata.stormmod', 'conveyorbelts.stormmod'},
    }

    for map_id, mods in expected.items():
        assert set(MAP_CONTEXT_DEPENDENCY_EVIDENCE[map_id]) == mods
        for mod in mods:
            ok, reason = definition_allowed(bg(mod), map_id)
            assert ok and reason == 'PROVEN_MAP_DEPENDENCY_MOD', (map_id, mod, ok, reason)
            assert MAP_CONTEXT_DEPENDENCY_EVIDENCE[map_id][mod], (map_id, mod)

    # A proven dependency for one map must remain foreign to unrelated maps.
    for mod in ('starcraft.stormmod', 'overwatchdata.stormmod', 'heavenhell.stormmod', 'warcraftdata.stormmod', 'conveyorbelts.stormmod'):
        ok, reason = definition_allowed(bg(mod), 'BlackheartsBay')
        assert not ok and reason == 'FOREIGN_BATTLEGROUND_MOD', (mod, ok, reason)

    # Embedded tutorial/Try Mode scope remains fail-closed.
    ok, reason = definition_allowed({
        'file': r'C:\mods\heroes.stormmod\base.stormmaps\maps\heroes\singleplayermaps\startingexperience\tutorial01.stormmap\base.stormdata\gamedata\unitdata.xml',
        'mod': 'heroes.stormmod',
    }, 'Volskaya')
    assert not ok and reason == 'UNPROVEN_EMBEDDED_STORMMAP'

    print('PASS test_map_context_v013')


if __name__ == '__main__':
    test()
