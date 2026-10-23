from pathlib import Path
import tempfile
from json_backend import save, load
from typed_reference_resolver import build_typed_reference_graph, resolve_ambiguous_candidate, has_propagating_path


def E(src,tgt,field,attr='value',order=1,relation='CANDIDATE_REFERENCE'):
    return {'source_key':src,'target_key':tgt,'target_id':tgt.split('::',1)[1],
            'field':field,'path':[field] if field else [],'attr':attr,'depth':1 if field else 0,
            'relation':relation,'resolution':'UNIQUE_CATALOG_ID','provenance':{'file':'x.xml','mod':'m.stormmod','order':order}}

def R(op='DEFINE',order=1):
    return {'definitions':[{'file':'x.xml','mod':'m.stormmod','operation':op,'order':order}]}

def run():
    records={
      'CAbilEffectInstant::FenixRepeaterCannon':R(),
      'CEffectSet::FenixRepeaterCannonInitialSet':R(),
      'CEffectRemoveBehavior::FenixPhaseBombRemoveBehavior':R(),
      'CBehaviorBuff::FenixPhaseBombBehavior':R(),
      'CWeaponLegacy::FenixHeroWeapon':R(),
      'CEffectDamage::FenixHeroWeaponDamage':R(),
      'CBehaviorBuff::XulCursedStrikesActive':R(),
      'CWeaponLegacy::NecromancerCursedStrikesWeapon':R(),
      'CEffectDamage::NecromancerCursedStrikesDamage':R(),
      'CEffectCreateUnit::ZagaraSpawnRoach':R(),
      'CUnit::ZagaraRoach':R(),
      'CWeaponLegacy::ZagaraRoachWeapon':R(),
      'CEffectDamage::ZagaraRoachAttackDamage':R(),
      'CEffectSet::HearthstoneSpecialCaseSet':{'definitions':[{'file':'base.xml','mod':'heroesdata.stormmod','operation':'DEFINE','order':10},{'file':'samuro.xml','mod':'samuro.stormmod','operation':'CONTRIBUTE','order':11}]},
      'CEffectEnumTrackedUnits::SamuroRemoveTrackedClones':R(),
      'CEffectApplyBehavior::PolymorphApplyBehavior':R(),
      'CBehaviorBuff::BrightwingPolymorph':R(),
      'CValidatorPlayerTalent::HasBrightwingUnstableAnomaly':R(),
      'CBehaviorBuff::ContextCarrier':R(),
      'CEffectDamage::ContextOnlyDamage':R(),
      'CEffectDamage::FilterOnlyDamage':R(),
      'CEffectLaunchMissile::ProjectileLauncher':{**R(),'fields':{'AmmoUnit':[{'attrs':{'value':'ProjectileUnit'}}]}},
      'CUnit::ProjectileUnit':{**R(),'object_id':'ProjectileUnit','xml_type':'CUnit','fields':{'FlagArray':[{'attrs':{'index':'Missile','value':'1'}}]}},
      'CEffectSet::ProjectileLaunchSet':R(),
      'CEffectDamage::ProjectileDamage':R(),
      'CEffectLaunchMissile::SummonLauncher':{**R(),'fields':{'AmmoUnit':[{'attrs':{'value':'SummonUnit'}}]}},
      'CUnit::SummonUnit':{**R(),'object_id':'SummonUnit','xml_type':'CUnit','fields':{'WeaponArray':[{'attrs':{'Link':'SummonWeapon'}}]}},
      'CEffectSet::SummonLaunchSet':R(),
      'CEffectDamage::SummonDamage':R(),
    }
    edges=[
      E('CAbilEffectInstant::FenixRepeaterCannon','CEffectSet::FenixRepeaterCannonInitialSet','Effect'),
      E('CEffectSet::FenixRepeaterCannonInitialSet','CEffectRemoveBehavior::FenixPhaseBombRemoveBehavior','EffectArray'),
      E('CEffectRemoveBehavior::FenixPhaseBombRemoveBehavior','CBehaviorBuff::FenixPhaseBombBehavior','BehaviorLink'),
      E('CBehaviorBuff::FenixPhaseBombBehavior','CWeaponLegacy::FenixHeroWeapon','WeaponDisableArray'),
      E('CWeaponLegacy::FenixHeroWeapon','CEffectDamage::FenixHeroWeaponDamage','Effect'),
      E('CBehaviorBuff::XulCursedStrikesActive','CWeaponLegacy::NecromancerCursedStrikesWeapon','WeaponEnableArray'),
      E('CWeaponLegacy::NecromancerCursedStrikesWeapon','CEffectDamage::NecromancerCursedStrikesDamage','Effect'),
      E('CEffectCreateUnit::ZagaraSpawnRoach','CUnit::ZagaraRoach','SpawnUnit'),
      E('CUnit::ZagaraRoach','CWeaponLegacy::ZagaraRoachWeapon','WeaponArray','Link'),
      E('CWeaponLegacy::ZagaraRoachWeapon','CEffectDamage::ZagaraRoachAttackDamage','Effect'),
      E('CEffectSet::HearthstoneSpecialCaseSet','CEffectEnumTrackedUnits::SamuroRemoveTrackedClones','EffectArray',order=11),
      E('CEffectApplyBehavior::PolymorphApplyBehavior','CBehaviorBuff::BrightwingPolymorph','Behavior'),
      E('CBehaviorBuff::BrightwingPolymorph','CValidatorPlayerTalent::HasBrightwingUnstableAnomaly','ValidatorArray'),
      E('CBehaviorBuff::ContextCarrier','CEffectDamage::ContextOnlyDamage','WhichUnit','Effect'),
      E('CBehaviorBuff::ContextCarrier','CEffectDamage::ContextOnlyDamage','AINotifySettings','AreaEffect',order=2),
      E('CBehaviorBuff::ContextCarrier','CEffectDamage::FilterOnlyDamage','ExcludeArray','Effect',order=3),
      E('CEffectLaunchMissile::ProjectileLauncher','CEffectSet::ProjectileLaunchSet','LaunchEffect'),
      E('CEffectSet::ProjectileLaunchSet','CEffectDamage::ProjectileDamage','EffectArray'),
      E('CEffectLaunchMissile::SummonLauncher','CEffectSet::SummonLaunchSet','LaunchEffect'),
      E('CEffectSet::SummonLaunchSet','CEffectDamage::SummonDamage','EffectArray'),
    ]
    amb=[{'source_key':'CEffectApplyBehavior::PolymorphApplyBehavior','target_id':'BrightwingPolymorph','field':'Behavior','path':['Behavior'],'attr':'value','depth':1,'relation':'CANDIDATE_REFERENCE','resolution':'AMBIGUOUS_TARGET_TYPE','candidate_keys':['CBehaviorBuff::BrightwingPolymorph','CButton::BrightwingPolymorph'],'replay_candidate_keys':['CBehaviorBuff::BrightwingPolymorph'],'provenance':{'file':'x.xml','mod':'m.stormmod','order':1}}]
    cat={'records':records,'reference_edges':edges,'ambiguous_reference_candidates':amb}
    with tempfile.TemporaryDirectory() as td:
      td=Path(td); save(td/'cat.json',cat)
      report=build_typed_reference_graph(td/'cat.json',td/'typed.json',td/'report.json')
      out=load(td/'typed.json'); es=out['edges']
      by={(e['source_key'],e['target_key']):e for e in es}
      assert by[('CBehaviorBuff::FenixPhaseBombBehavior','CWeaponLegacy::FenixHeroWeapon')]['relation']=='DEACTIVATES_WEAPON'
      assert not by[('CBehaviorBuff::FenixPhaseBombBehavior','CWeaponLegacy::FenixHeroWeapon')]['propagation']['ownership']
      assert by[('CBehaviorBuff::XulCursedStrikesActive','CWeaponLegacy::NecromancerCursedStrikesWeapon')]['relation']=='ACTIVATES_WEAPON'
      assert by[('CBehaviorBuff::XulCursedStrikesActive','CWeaponLegacy::NecromancerCursedStrikesWeapon')]['propagation']['ownership']
      assert by[('CEffectCreateUnit::ZagaraSpawnRoach','CUnit::ZagaraRoach')]['owner_boundary']=='SUMMON'
      assert not by[('CEffectCreateUnit::ZagaraSpawnRoach','CUnit::ZagaraRoach')]['propagation']['ownership']
      hearth=by[('CEffectSet::HearthstoneSpecialCaseSet','CEffectEnumTrackedUnits::SamuroRemoveTrackedClones')]
      assert hearth['source_definition_operation']=='CONTRIBUTE' and hearth['owner_boundary']=='CONTRIBUTION'
      assert not hearth['propagation']['ownership']
      assert by[('CEffectApplyBehavior::PolymorphApplyBehavior','CBehaviorBuff::BrightwingPolymorph')]['relation']=='APPLIES_BEHAVIOR'
      assert by[('CBehaviorBuff::BrightwingPolymorph','CValidatorPlayerTalent::HasBrightwingUnstableAnomaly')]['propagation']['condition']
      assert not has_propagating_path(es,'CAbilEffectInstant::FenixRepeaterCannon','CEffectDamage::FenixHeroWeaponDamage','ownership')
      assert has_propagating_path(es,'CBehaviorBuff::XulCursedStrikesActive','CEffectDamage::NecromancerCursedStrikesDamage','ownership')
      assert not has_propagating_path(es,'CEffectCreateUnit::ZagaraSpawnRoach','CEffectDamage::ZagaraRoachAttackDamage','ownership')
      assert not has_propagating_path(es,'CBehaviorBuff::ContextCarrier','CEffectDamage::ContextOnlyDamage','ownership')
      assert not has_propagating_path(es,'CBehaviorBuff::ContextCarrier','CEffectDamage::FilterOnlyDamage','ownership')
      proj=by[('CEffectLaunchMissile::ProjectileLauncher','CEffectSet::ProjectileLaunchSet')]
      assert proj['relation']=='PROJECTILE_LAUNCH_EXECUTES' and proj['propagation']['ownership'] and proj['owner_boundary'] is None
      assert has_propagating_path(es,'CEffectLaunchMissile::ProjectileLauncher','CEffectDamage::ProjectileDamage','ownership')
      summon=by[('CEffectLaunchMissile::SummonLauncher','CEffectSet::SummonLaunchSet')]
      assert summon['relation']=='LAUNCHED_UNIT_INITIALIZER' and not summon['propagation']['ownership'] and summon['owner_boundary']=='LAUNCHED_UNIT'
      assert not has_propagating_path(es,'CEffectLaunchMissile::SummonLauncher','CEffectDamage::SummonDamage','ownership')
      ctx=[e for e in es if e['source_key']=='CBehaviorBuff::ContextCarrier']
      assert any(e['relation']=='EFFECT_CONTEXT_REFERENCE' and not e['propagation']['execution'] for e in ctx)
      assert any(e['relation']=='AI_EFFECT_REFERENCE' and not e['propagation']['dependency'] for e in ctx)
      assert any(e['relation']=='EFFECT_FILTER_CONDITION' and e['propagation']['condition'] for e in ctx)
      assert report['summary']['contribution_boundaries']>=1
    # direct ambiguity resolution: Behavior wins over same-id button
    rr=resolve_ambiguous_candidate(amb[0])
    assert rr['target_key']=='CBehaviorBuff::BrightwingPolymorph'
    return True

if __name__=='__main__': print('PASS' if run() else 'FAIL')
