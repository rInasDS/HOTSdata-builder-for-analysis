from pathlib import Path
import tempfile
from json_backend import save, load
from effective_catalog import EffectiveCatalogCompiler
from typed_reference_resolver import classify_reference, resolve_ambiguous_candidate
from combat_typed_engine import run_combat
from replay_schema_v1 import normalize_ability


def run():
    # 1) Effective Catalog must preserve nested literal combat facts.
    raw={"objects":{"Buff":[{
        "object_id":"Buff","xml_type":"CBehaviorBuff","file":"x.xml","mod":"h.stormmod","order":1,
        "attrs":{"id":"Buff"},
        "children":[{"tag":"Modification","attrs":{}}],
        "descendants":[
            {"tag":"Modification","attrs":{},"path":["Modification"],"depth":1},
            {"tag":"DamageDealtFraction","attrs":{"value":"0.2"},"path":["Modification","DamageDealtFraction"],"depth":2},
        ],
    }]}}
    eff=EffectiveCatalogCompiler(raw).compile()['records']['CBehaviorBuff::Buff']
    assert any(f['tag']=='DamageDealtFraction' and f['attrs'].get('value')=='0.2' for f in eff['nested_facts'])

    # 2) CTalent AbilityModificationArray/Modifications@Entry is a typed modifier target.
    rule=classify_reference('CTalent::T','Modifications','Entry',['AbilityModificationArray','Modifications'])
    assert rule.relation=='TALENT_MODIFICATION_TARGET' and rule.modifier and not rule.ownership

    # 3) TokenId with multiple token-family records is a shared identity, not guessed ownership.
    amb={
        'source_key':'CBehaviorBuff::B','field':'TokenId','attr':'value','path':['TokenId'],
        'candidate_keys':['CAccumulatorToken::Shared','CBehaviorTokenCounter::Shared','CButton::Shared'],
        'replay_candidate_keys':['CAccumulatorToken::Shared','CBehaviorTokenCounter::Shared','CButton::Shared'],
    }
    rr=resolve_ambiguous_candidate(amb)
    assert rr['status']=='RESOLVED_SHARED_TOKEN_IDENTITY' and rr.get('identity_only')
    assert set(rr['matches'])=={'CAccumulatorToken::Shared','CBehaviorTokenCounter::Shared'}

    # 4) Synthetic typed combat bridge: direct damage, nested modifier/shield and
    # branch-specific talent-gated percent-health damage.
    records={
        'CAbilEffectInstant::A':{'object_id':'A','xml_type':'CAbilEffectInstant','fields':{},'nested_facts':[]},
        'CEffectDamage::D':{'object_id':'D','xml_type':'CEffectDamage','fields':{'Amount':[{'attrs':{'value':'100'},'provenance':{'file':'x.xml'}}]},'nested_facts':[]},
        'CBehaviorBuff::M':{'object_id':'M','xml_type':'CBehaviorBuff','fields':{},'nested_facts':[
            {'tag':'DamageDealtFraction','path':['Modification','DamageDealtFraction'],'attrs':{'value':'-0.4'},'provenance':{'file':'x.xml'}}]},
        'CBehaviorBuff::S':{'object_id':'S','xml_type':'CBehaviorBuff','fields':{},'nested_facts':[
            {'tag':'ModifyLimit','path':['DamageResponse','ModifyLimit'],'attrs':{'value':'500'},'provenance':{'file':'x.xml'}}]},
        'CEffectSwitch::SW':{'object_id':'SW','xml_type':'CEffectSwitch','fields':{
            'CaseArray':[{'attrs':{'Effect':'DT','Validator':'VT'},'provenance':{'file':'x.xml','index':'0'}}],
            'CaseDefault':[{'attrs':{'value':'D'},'provenance':{'file':'x.xml'}}]},'nested_facts':[]},
        'CEffectDamage::DT':{'object_id':'DT','xml_type':'CEffectDamage','fields':{'VitalFractionMax':[{'attrs':{'value':'0.02'},'provenance':{'file':'x.xml'}}]},'nested_facts':[]},
        'CValidatorPlayerTalent::VT':{'object_id':'VT','xml_type':'CValidatorPlayerTalent','fields':{},'nested_facts':[]},
        'CTalent::TalentT':{'object_id':'TalentT','xml_type':'CTalent','fields':{},'nested_facts':[]},
    }
    def edge(src,tgt,rel,field,*,ownership=False,execution=False,condition=False):
        return {'source_key':src,'target_key':tgt,'target_id':tgt.split('::',1)[1],
                'relation':rel,'field':field,'path':[field], 'attr':'value',
                'propagation':{'dependency':True,'ownership':ownership,'execution':execution,'condition':condition,'modifier':False,'formula':False}}
    typed={'edges':[
        edge('CAbilEffectInstant::A','CEffectDamage::D','EXECUTES','Effect',ownership=True,execution=True),
        edge('CAbilEffectInstant::A','CBehaviorBuff::M','APPLIES_BEHAVIOR','Behavior',ownership=True,execution=True),
        edge('CAbilEffectInstant::A','CBehaviorBuff::S','APPLIES_BEHAVIOR','Behavior',ownership=True,execution=True),
        edge('CAbilEffectInstant::A','CEffectSwitch::SW','EXECUTES','Effect',ownership=True,execution=True),
        edge('CEffectSwitch::SW','CEffectDamage::DT','CONDITIONAL_EXECUTES','CaseArray',ownership=True,execution=True),
        edge('CEffectSwitch::SW','CValidatorPlayerTalent::VT','CONDITIONS','CaseArray',condition=True),
        edge('CValidatorPlayerTalent::VT','CTalent::TalentT','CHECKS_TALENT','Value',condition=True),
    ],'unresolved':[]}
    cat={'records':records}
    hd={'heroes':{'Hero':{'abilities':[{'id':'A','ability_id':'A','record_key':'Hero|A'}]}}}
    with tempfile.TemporaryDirectory() as td:
        td=Path(td)
        save(td/'cat.json',cat); save(td/'typed.json',typed); save(td/'hd.json',hd)
        report=run_combat(td/'cat.json',td/'typed.json',td/'hd.json',td/'combat.json',td/'report.json')
        x=load(td/'combat.json')['heroes']['Hero'][0]
        assert [d['effect_id'] for d in x['damage']]==['D']
        assert x['damage'][0]['value']==100
        assert [d['effect_id'] for d in x['conditional_damage']]==['DT']
        assert x['conditional_damage'][0]['formula']['model']=='PERCENT_MAX_HEALTH'
        assert x['conditional_damage'][0]['formula']['value_percent']==2.0
        assert x['conditional_damage'][0]['conditions'][0]['talent_ids']==['TalentT']
        assert any(m['modifier_kind']=='DAMAGE_DEALT_FRACTION' and m['value']==-0.4 for m in x['modifiers'])
        assert any(s['limit']==500 for s in x['shield_candidates'])
        assert report['summary']['with_damage']==1 and report['summary']['with_talent_gated_damage']==1

        # 5) Replay Schema must consume typed direct/conditional combat instead of
        # dropping it behind legacy resolver_semantics.
        ability={'ability_id':'A','id':'A','record_key':'Hero|A','name':'A','combat':x,
                 'resolver_semantics':{'damage_models':[{'model':'FLAT','value':100}],
                                       'heal_models':[],'shield_models':[],
                                       'has_damage':True,'has_heal':False,'has_shield':True,
                                       'status_effects':[],'protection':[],'interrupt_capable':[],
                                       'talent_combat_overlays':[],'talent_status_overlays':[]}}
        rv=normalize_ability('Hero',ability)
        assert any(m['kind']=='SHIELD' and m['formula'].get('value')==500 for m in rv['mechanics'])
        cd=[m for m in rv['mechanics'] if m['kind']=='DAMAGE' and m.get('execution_relation')=='CONDITIONAL_TYPED_OWNERSHIP']
        assert len(cd)==1 and cd[0]['formula']['value_percent']==2.0
        assert any(mod.get('modifier_kind')=='DAMAGE_DEALT_FRACTION' and mod.get('value')==-0.4 for mod in rv['modifiers'])
    return True

if __name__=='__main__':
    print('PASS' if run() else 'FAIL')
