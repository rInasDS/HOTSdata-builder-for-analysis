from pathlib import Path
import tempfile
from json_backend import save, load
from replay_effective_catalog import build_replay_effective_catalog, _looks_like_catalog_id

def D(order, typ, oid, children=None, descendants=None, parent=None):
    attrs={"id":oid}
    if parent: attrs["parent"]=parent
    return {"object_id":oid,"xml_type":typ,"file":f"f{order}.xml","mod":"m.stormmod","attrs":attrs,
            "children":children or [],"descendants":descendants or [],"order":order}

def run():
    raw={"objects":{
      "A":[D(1,"CAbilEffectInstant","A",children=[{"tag":"Cost","attrs":{}}],descendants=[
          {"tag":"Cost","attrs":{},"path":["Cost"],"depth":1},
          {"tag":"Cooldown","attrs":{"TimeUse":"4","Link":"E","Face":"Amb"},"path":["Cost","Cooldown"],"depth":2},
      ])],
      "E":[D(2,"CEffectDamage","E",children=[{"tag":"Amount","attrs":{"value":"$DamageConst"}},{"tag":"Accumulator","attrs":{"value":"Acc"}}],descendants=[{"tag":"Amount","attrs":{"value":"$DamageConst"},"path":["Amount"],"depth":1},{"tag":"Accumulator","attrs":{"value":"Acc"},"path":["Accumulator"],"depth":1}])],
      "$DamageConst":[D(9,"const","$DamageConst")],
      "4":[D(3,"CEffectDamage","4")],
      "Amb":[D(4,"CEffectDamage","Amb"),D(5,"CButton","Amb")],
      "Acc":[D(6,"CAccumulatorToken","Acc",parent="BaseAccumulator")],
      "BaseAccumulator":[D(10,"CAccumulator","BaseAccumulator",children=[{"tag":"MinAccumulation","attrs":{"value":"0"}}],descendants=[{"tag":"MinAccumulation","attrs":{"value":"0"},"path":["MinAccumulation"],"depth":1}])],
      "T":[D(8,"CTalent","T",children=[{"tag":"Abil","attrs":{"value":"A"}}],descendants=[{"tag":"Abil","attrs":{"value":"A"},"path":["Abil"],"depth":1}])],
      "Visual":[D(7,"CActorModel","Visual")],
    }}
    hd={"heroes":{"H":{"unit_id":None,"summoned_unit_ids":[],"weapons":[],
        "abilities":[{"id":"A","ability_id":"A","record_key":"H|A"}],"talents":[{"talent_id":"T","ability_id":"A","tooltip_ability_link_ids":[]}]}}}
    with tempfile.TemporaryDirectory() as td:
        td=Path(td); save(td/'raw.json',raw); save(td/'hd.json',hd)
        r=build_replay_effective_catalog(td/'raw.json',td/'hd.json',td/'out.json',td/'report.json',td/'dup.json')
        out=load(td/'out.json')
        assert set(out['records'])=={'CAbilEffectInstant::A','CEffectDamage::E','CTalent::T','const::$DamageConst','CAccumulatorToken::Acc','CAccumulator::BaseAccumulator'}, set(out['records'])
        assert r['summary']['nested_reference_edges']>=1
        assert r['summary']['ambiguous_reference_candidates']>=1
        assert 'CEffectDamage::4' not in out['records']
        assert 'CEffectDamage::Amb' not in out['records']
        assert not out['unresolved_replay_blocking']
        assert any(e.get('relation')=='INHERITS' and e.get('resolution')=='BASE_CLASS_PREFIX' and e.get('target_key')=='CAccumulator::BaseAccumulator' for e in out['reference_edges'])
        assert _looks_like_catalog_id('4') is False
    return True

if __name__=='__main__': print('PASS' if run() else 'FAIL')
