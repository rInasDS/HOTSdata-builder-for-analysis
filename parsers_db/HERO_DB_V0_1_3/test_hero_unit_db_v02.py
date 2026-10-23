from hero_unit_db_builder import (
    _reconstruct_groups, _scaling_semantic_class, _scaling_level_domain,
)

def test_direct_group_attrs_retained():
    d={'descendants':[
        {'depth':1,'tag':'LevelScalingArray','path':['LevelScalingArray'],'same_tag_ordinal':1,'attrs':{'Ability':'UtherHolyLight'}},
        {'depth':2,'tag':'Modifications','path':['LevelScalingArray','Modifications'],'same_tag_ordinal':3,
         'attrs':{'Entry':'UtherHolyLight','Field':'Cost[0].Vital'}},
    ]}
    g=_reconstruct_groups(d,'LevelScalingArray')[0]
    assert g['values']['Entry']=='UtherHolyLight'
    assert g['values']['Field']=='Cost[0].Vital'

def rec(typ, chain=None):
    return {'xml_type':typ,'inheritance_chain':chain or []}

def test_scaling_semantic_classes():
    assert _scaling_semantic_class(rec('CUnit'),'LifeMax',None,'HeroX',{})=='HERO_HEALTH_POOL'
    assert _scaling_semantic_class(rec('CEffectDamage'),'Amount','Spell','SpellDamage',{})=='ABILITY_DAMAGE'
    assert _scaling_semantic_class(rec('CEffectDamage'),'Amount',None,'WeaponDamage',{'WeaponDamage':[{}]})=='BASIC_ATTACK_DAMAGE'
    assert _scaling_semantic_class(rec('CEffectCreateHealer'),'RechargeVitalRate','Heal','HealFx',{})=='ABILITY_HEAL'
    shield=rec('CBehaviorBuff',[{'id':'StormProtect'},{'id':'StormShield'}])
    assert _scaling_semantic_class(shield,'DamageResponse.ModifyLimit','Barrier','BarrierBuff',{})=='ABILITY_SHIELD'

def test_level_domain_fail_closed():
    d=_scaling_level_domain([{'catalog_slot_count':30,'explicit_level_indices':list(range(1,30))}])
    assert d['normal_1_30_catalog_evidence']=='PROVEN_30_SLOT_VETERANCY_PRESENT'
    assert d['display_level_to_slot_mapping'].startswith('UNRESOLVED')
    assert d['post_catalog_slot_behavior'].startswith('UNRESOLVED')

if __name__=='__main__':
    test_direct_group_attrs_retained(); test_scaling_semantic_classes(); test_level_domain_fail_closed()
    print('test_hero_unit_db_v02: PASS')
