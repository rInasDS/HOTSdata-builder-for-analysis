from hero_unit_db_builder import _reconstruct_groups, _owned_entities_for_root, _playable_heroes, _vision_primitives_for_record
from typed_reference_resolver import classify_reference

def edge(src,tgt,rel,own=False,exe=False,bound=None,eid='e'):
    return {'source_key':src,'target_key':tgt,'target_id':tgt.split('::')[-1] if tgt else None,'relation':rel,'edge_id':eid,'owner_boundary':bound,'propagation':{'ownership':own,'execution':exe}}

def rec(typ,oid,fields=None): return {'xml_type':typ,'object_id':oid,'fields':fields or {},'nested_facts':[]}
def node(tag,**attrs): return {'tag':tag,'attrs':attrs,'provenance':{}}

def test_grouping():
    d={'descendants':[
      {'tag':'AbilityModificationArray','path':['AbilityModificationArray'],'depth':1,'same_tag_ordinal':0,'attrs':{}},
      {'tag':'Modifications','path':['AbilityModificationArray','Modifications'],'depth':2,'same_tag_ordinal':0,'attrs':{}},
      {'tag':'Catalog','path':['AbilityModificationArray','Modifications','Catalog'],'depth':3,'attrs':{'value':'Unit'}},
      {'tag':'Entry','path':['AbilityModificationArray','Modifications','Entry'],'depth':3,'attrs':{'value':'HeroX'}},
      {'tag':'Field','path':['AbilityModificationArray','Modifications','Field'],'depth':3,'attrs':{'value':'Sight'}},
      {'tag':'Value','path':['AbilityModificationArray','Modifications','Value'],'depth':3,'attrs':{'value':'4'}},
      {'tag':'Modifications','path':['AbilityModificationArray','Modifications'],'depth':2,'same_tag_ordinal':1,'attrs':{}},
      {'tag':'Field','path':['AbilityModificationArray','Modifications','Field'],'depth':3,'attrs':{'value':'Range'}},
    ]}
    g=_reconstruct_groups(d,'AbilityModificationArray')
    assert len(g)==2 and g[0]['values']['Field']=='Sight' and g[1]['values']['Field']=='Range'

def test_direct_spawn_and_reference_not_ownership():
    records={'CUnit::HeroX':rec('CUnit','HeroX'),'CAbilEffect::A':rec('CAbilEffect','A'),'CEffectCreateUnit::E':rec('CEffectCreateUnit','E'),'CUnit::Summon':rec('CUnit','Summon')}
    edges=[edge('CUnit::HeroX','CAbilEffect::A','UNIT_HAS_ABILITY',own=True,eid='1'),edge('CAbilEffect::A','CEffectCreateUnit::E','EXECUTES',own=True,exe=True,eid='2'),edge('CEffectCreateUnit::E','CUnit::Summon','SPAWNS_UNIT',exe=True,bound='SUMMON',eid='3')]
    got=_owned_entities_for_root('CUnit::HeroX',{},records,edges)
    assert {x['unit_id'] for x in got}=={'Summon'}
    # ProducedUnitArray/reference-only edge cannot manufacture ownership.
    edges2=[edge('CUnit::HeroX','CAbilEffect::A','UNIT_HAS_ABILITY',own=True,eid='1'),edge('CAbilEffect::A','CUnit::Summon','REFERENCES_UNIT',eid='r')]
    assert _owned_entities_for_root('CUnit::HeroX',{},records,edges2)==[]

def test_launched_unit_corroboration_and_missile_exclusion():
    abil=rec('CAbilEffect','A',{'ProducedUnitArray':[node('ProducedUnitArray',value='Roach')]})
    launch=rec('CEffectLaunchMissile','L',{'AmmoUnit':[node('AmmoUnit',value='Roach')]})
    records={'CUnit::HeroX':rec('CUnit','HeroX'),'CAbilEffect::A':abil,'CEffectLaunchMissile::L':launch,'CUnit::Roach':rec('CUnit','Roach'),'CValidator::V':rec('CValidator','V')}
    edges=[edge('CUnit::HeroX','CAbilEffect::A','UNIT_HAS_ABILITY',own=True,eid='1'),edge('CAbilEffect::A','CEffectLaunchMissile::L','EXECUTES',own=True,exe=True,eid='2'),edge('CEffectLaunchMissile::L','CValidator::V','CONDITIONS',eid='3')]
    got=_owned_entities_for_root('CUnit::HeroX',{},records,edges)
    assert any(x['unit_id']=='Roach' and x['proof']=='PROVEN_ABILITY_PRODUCED_EXECUTED_AMMO_UNIT' for x in got)
    records['CUnit::Roach']=rec('CUnit','Roach',{'FlagArray':[node('FlagArray',index='Missile',value='1')]})
    assert not _owned_entities_for_root('CUnit::HeroX',{},records,edges)

def test_local_roster_not_inherited_form():
    chero={'CHero::Hero':{'fields':{'ProductId':[node('ProductId',value='1')],'AttributeId':[node('AttributeId',value='ABCD')]}},'CHero::Form':{'fields':{'ProductId':[node('ProductId',value='1')],'AttributeId':[node('AttributeId',value='ABCD')]}}}
    raw={'objects':{'Hero':[{'xml_type':'CHero','children':[node('ProductId',value='1'),node('AttributeId',value='ABCD')]}],'Form':[{'xml_type':'CHero','attrs':{'parent':'Hero'},'children':[]}]}}
    assert _playable_heroes(chero,raw)==['Hero']

def test_weaponarray_turret_typed_separately():
    r=classify_reference('CUnit::TurretUnit','WeaponArray','Turret',['WeaponArray'],'CANDIDATE_REFERENCE')
    assert r.relation=='UNIT_USES_TURRET' and r.expected_prefixes==('CTurret',) and not r.execution
    w=classify_reference('CUnit::TurretUnit','WeaponArray','Link',['WeaponArray'],'CANDIDATE_REFERENCE')
    assert w.relation=='UNIT_EQUIPS_WEAPON' and w.expected_prefixes==('CWeapon',)


def test_vision_primitives():
    b=rec('CBehaviorBuff','B',{'Modification':[node('Modification')]})
    b['nested_facts']=[{'tag':'SightBonus','path':['Modification','SightBonus'],'attrs':{'value':'18'},'provenance':{}}]
    got=_vision_primitives_for_record('CBehaviorBuff::B',b)
    assert any(x['kind']=='SIGHT_BONUS' and x['value']=='18' for x in got)
    e=rec('CEffectCreatePersistent','E',{'RevealRadius':[node('RevealRadius',value='10')]})
    got=_vision_primitives_for_record('CEffectCreatePersistent::E',e)
    assert any(x['kind']=='AREA_REVEAL_RADIUS' and x['radius']=='10' for x in got)
    r=rec('CBehaviorBuff','R',{'BehaviorCategories':[node('BehaviorCategories',index='Reveal',value='1')],'Duration':[node('Duration',value='2')]})
    got=_vision_primitives_for_record('CBehaviorBuff::R',r)
    assert any(x['kind']=='REVEAL_BEHAVIOR' and x['duration']=='2' for x in got)

def main():
    test_grouping(); test_direct_spawn_and_reference_not_ownership(); test_launched_unit_corroboration_and_missile_exclusion(); test_local_roster_not_inherited_form(); test_weaponarray_turret_typed_separately(); test_vision_primitives(); print('test_hero_unit_db_v01: PASS')
if __name__=='__main__':main()
