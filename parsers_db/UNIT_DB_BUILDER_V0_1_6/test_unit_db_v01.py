from pathlib import Path
from tempfile import TemporaryDirectory
from json_backend import save, load
from storm_catalog_index import build_catalog
from unit_effective_catalog import build_unit_effective_catalog
from typed_reference_resolver import build_typed_reference_graph
from unit_semantics import collect_observed_units, build_unit_db

XML='''<Catalog>
<CUnit id="TestBoss">
  <LifeMax value="1000"/><LifeStart value="1000"/><Radius value="1"/><Sight value="9"/>
  <BehaviorArray Link="ReactiveArmor"/><WeaponArray Link="BossWeapon"/><ArmorLink value="BossArmor"/>
</CUnit>
<CArmor id="BossArmor"><Amount value="20"/></CArmor>
<CWeaponLegacy id="BossWeapon"><Period value="1.25"/><Range value="5"/><Effect value="BossAttackDamage"/></CWeaponLegacy>
<CEffectDamage id="BossAttackDamage"><Amount value="100"/></CEffectDamage>
<CBehaviorBuff id="ReactiveArmor">
  <DamageResponse Chance="1" Handled="OnHitSet"/>
  <Modification><Armor value="10"/></Modification>
</CBehaviorBuff>
<CEffectSet id="OnHitSet"><EffectArray value="ApplyDebuff"/></CEffectSet>
<CEffectApplyBehavior id="ApplyDebuff"><Behavior value="ArmorDebuff"/></CEffectApplyBehavior>
<CBehaviorBuff id="ArmorDebuff"><Duration value="3"/><FinalEffect value="CleanupEffect"/><Modification><Armor value="-10"/></Modification></CBehaviorBuff>
<CEffectSet id="CleanupEffect"/>
</Catalog>'''

def make_map(path):
    save(path,{"schema":{"name":"PYHOTS_MAP_DB","version":"0.2"},"maps":{"TestMap":{
        "unit_types":[{"unit_type":"TestBoss","replay_support":3,"spawn_count":3,"owner_classes":{"NON_ROSTER_CONTROL_ID":3}}],
        "semantic_landmarks":[{"semantic_class":"JUNGLE_CAMP","nearby_spawn_clusters":[{"unit_type":"TestBoss","owner_class":"NON_ROSTER_CONTROL_ID"}]}]
    }}})

def test():
  with TemporaryDirectory() as td:
    root=Path(td); mods=root/'mods'/'test.stormmod'/'base.stormdata'/'gamedata'; mods.mkdir(parents=True)
    (mods/'test.xml').write_text(XML)
    mapdb=root/'map_db.json'; make_map(mapdb)
    idx=root/'catalog.json'; build_catalog(root/'mods',idx,root/'catalog_report.json')
    obs=collect_observed_units(load(mapdb)); assert [x['unit_id'] for x in obs]==['TestBoss']
    eff=root/'eff.json'; build_unit_effective_catalog(idx,['TestBoss'],eff,root/'eff_report.json')
    typed=root/'typed.json'; build_typed_reference_graph(eff,typed,root/'typed_report.json')
    result,integ=build_unit_db(mapdb,eff,typed,root/'unit_db.json',root/'summary.json',root/'integrity.json',root/'unresolved.json')
    u=result['units']['TestBoss']
    assert u['base_stats']['LifeMax']['numeric_value']==1000
    assert u['weapons'] and u['weapons'][0]['id']=='BossWeapon'
    assert u['armor'] and u['armor'][0]['id']=='BossArmor'
    assert u['reactive_mechanics'] and u['reactive_mechanics'][0]['trigger']=='ON_DAMAGE_TAKEN'
    edges=u['reactive_mechanics'][0]['execution_edges']
    assert any(e['target_key']=='CEffectSet::OnHitSet' for e in edges)
    assert any(x['id']=='BossAttackDamage' for x in u['effect_summaries'])
    # FinalEffect is an event mechanic, not an ON_DAMAGE_TAKEN mechanic by itself.
    assert len(u['reactive_mechanics'])==1
    assert any(e.get('target_key')=='CEffectSet::CleanupEffect' for e in u['event_mechanics'])
    assert integ['overall']=='PASS'
    print('PASS test_unit_db_v01')
if __name__=='__main__': test()
