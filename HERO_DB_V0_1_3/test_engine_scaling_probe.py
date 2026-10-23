import os
from pathlib import Path
import pytest
from engine_scaling_probe import galaxy_semantic_anchors, probe


def test_galaxy_semantic_parser_synthetic():
    heroes = '''
    lv_numberOfLevelUpAbils = CatalogFieldValueCount(c_gameCatalogHero, h, "LevelScalingArray", c_playerAny);
    lv_levelUpAbilitiesCount = lv_numberOfLevelUpAbils;
    lv_numberOfFieldChanges[x] = CatalogFieldValueCount(c_gameCatalogHero, h, "LevelScalingArray[0].Modifications", c_playerAny);
    lv_percentScaled[x][y] = CatalogFieldValueGetAsInt(c_gameCatalogHero, h, "LevelScalingArray[0].Modifications[0].AffectedByOverdrive", c_playerAny) == 1;
    lv_valueAddedPerLevel[x][y] = CatalogFieldValueGetAsFixed(c_gameCatalogHero, h, "LevelScalingArray[0].Modifications[0].Value", c_playerAny);
    lv_catalog[x][y] = 0; lv_entry[x][y] = ""; lv_field[x][y] = "";
    lv_fieldIsInteger[x][y] = CatalogFieldValueGetAsInt(c_gameCatalogHero,h,"FieldIsInteger",c_playerAny)==1;
    '''
    game = '''
    bool libGame_gv_dEBUG_PercentScalingEnabled = true;
    void libGame_gf_TogglePercentScaling(bool x) { libGame_gv_dEBUG_PercentScalingEnabled = true; }
    void libGame_gf_ApplyAbilityModificationsForPlayerAtTalent() {
      if (c_talentModificationFlatModification) { x=CatalogFieldValueGetAsFixed(lv_catalog, lv_entry, lv_field, lp_player); }
      if (c_talentModificationMultiplyLevelModification) { x=CatalogFieldValueGetAsFixed(lv_catalog, lv_entry, lv_field, lp_player); }
    }
    void libGame_gf_ApplyLevelUpHeroStatsForHeroSingleLevel(unit lp_hero) {
      if (lv_field == "InitialOffset" || lv_field == "ExpireOffset" || lv_field == "FinalOffset" || lv_field == "PeriodicOffsetArray[0]") {
        lv_yOffsetReal += lv_valueAddedPerLevel[x][y];
      } else {
        if (lv_percentScaled[x][y]) {
          lv_newValue = CatalogFieldValueGetAsFixed(c,e,f,p) * lv_valueAddedPerLevel[x][y];
          if (lv_valueAddedPerLevel[x][y] > 1.0) { lv_newValue = 0.0; }
        } else { lv_newValue = lv_valueAddedPerLevel[x][y]; }
        CatalogFieldValueModifyInt(c,e,f,p,FixedToInt(lv_newValue),c_upgradeOperationAdd);
        CatalogFieldValueModifyFixed(c,e,f,p,lv_newValue,c_upgradeOperationAdd);
      }
    }
    void libGame_gf_RemoveLevelUpHeroStatsForPlayer() {}
    void libGame_gf_LevelUpHero(unit lp_hero) {}
    bool auto_libGame_gf_LevelUpHero_TriggerFunc(bool a, bool b) { libGame_gf_ApplyLevelUpHeroStatsForHeroSingleLevel(lp_hero); return true; }
    trigger auto_libGame_gf_SpawnHero_Trigger = null;
    void spawn() { SetTalentTreeHeroLevel(lp_player, 0); libGame_gf_LevelUpHero(libGame_gv_players[lp_player].lv_heroUnit); }
    if (EventUnitBehavior() == "HeroDummyVeterancy") {}
    TriggerAddEventUnitGainLevel(t, null);
    '''
    r = galaxy_semantic_anchors(heroes, game)
    assert r['status'] == 'SCRIPT_SEMANTICS_MATCH'
    assert r['application_semantics'] == 'PROVEN_PERCENT_CURRENT_VALUE_ADD_OR_FLAT_ADD_PER_FLAG'
    assert r['heroes_lib']['loads_level_scaling_array_as_group_list']
    assert r['game_lib']['initial_spawn_calls_levelup']
    assert r['game_lib']['no_explicit_level_cap_in_scaling_application_function']


def test_real_corpus_if_supplied():
    hero = os.environ.get('PYHOTS_HERO_UNIT_DB')
    compact = os.environ.get('PYHOTS_HERO_GLOBAL_COMPACT')
    if not hero or not compact:
        pytest.skip('Set PYHOTS_HERO_UNIT_DB and PYHOTS_HERO_GLOBAL_COMPACT for real-corpus v0.2.4 test')
    r = probe(Path(hero), Path(compact))
    assert r['integrity']['overall'] == 'PASS'
    local = r['local_catalog_evidence']
    assert local['record_count'] == 1641
    assert local['declaration_mode_counts']['PERCENT_FLAG_DECLARED'] == 1635
    assert local['declaration_mode_counts']['PERCENT_FLAG_DECLARED_VALUE_DEFAULT_DEPENDENT'] == 2
    assert local['declaration_mode_counts']['OVERDRIVE_FLAG_ABSENT_VALUE_DECLARED'] == 2
    assert local['declaration_mode_counts']['DEFAULT_DEPENDENT_INCOMPLETE_RECORD'] == 2
    assert len(local['non_percent_declared_records']) == 6
    assert r['conclusions']['local_runtime_arithmetic'].startswith('UNRESOLVED_')
