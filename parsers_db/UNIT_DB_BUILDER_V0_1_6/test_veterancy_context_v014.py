from pathlib import Path
from tempfile import TemporaryDirectory

from json_backend import load
from storm_catalog_index import build_catalog
from unit_effective_catalog import build_unit_effective_catalog_data
from typed_reference_resolver import build_typed_reference_graph_data
from unit_semantics import build_unit_entries_from_data

XML='''<Catalog>
<CUnit id="TestUnit">
  <BehaviorArray Link="ChildScaling"/>
  <BehaviorArray Link="ImplicitScaling"/>
</CUnit>
<CBehaviorVeterancy id="BaseScaling">
  <VeterancyLevelArray index="0"><Modification><VitalMaxArray index="Life" value="10"/></Modification></VeterancyLevelArray>
  <VeterancyLevelArray index="1"><Modification><VitalMaxArray index="Life" value="20"/></Modification></VeterancyLevelArray>
  <VeterancyLevelArray index="2"><Modification><VitalMaxArray index="Life" value="30"/></Modification></VeterancyLevelArray>
</CBehaviorVeterancy>
<CBehaviorVeterancy id="ChildScaling" parent="BaseScaling">
  <VeterancyLevelArray index="0" removed="1"/>
  <VeterancyLevelArray index="1"><Modification><VitalMaxArray index="Life" value="200"/></Modification></VeterancyLevelArray>
  <VeterancyLevelArray index="3"><Modification><VitalMaxArray index="Life" value="400"/></Modification></VeterancyLevelArray>
</CBehaviorVeterancy>
<CBehaviorVeterancy id="ImplicitScaling">
  <VeterancyLevelArray><Modification><DamageDealtScaled index="Basic" value="1"/></Modification></VeterancyLevelArray>
  <VeterancyLevelArray MinVeterancyXP="1"><Modification><DamageDealtScaled index="Basic" value="2"/></Modification></VeterancyLevelArray>
</CBehaviorVeterancy>
</Catalog>'''


def mapdb():
    return {"maps":{"TestMap":{"unit_types":[{"unit_type":"TestUnit","replay_support":1,"spawn_count":1,"owner_classes":{"NON_ROSTER_CONTROL_ID":1}}],"semantic_landmarks":[]}}}


def by_id(items, object_id):
    return next(x for x in items if x['id']==object_id)


def facts_by_level(vet):
    return {str(level['index']): level['nested_facts'] for level in vet['levels']}


def test():
    with TemporaryDirectory() as td:
        root=Path(td)/'mods'; p=root/'heroesdata.stormmod/base.stormdata/gamedata/data.xml'; p.parent.mkdir(parents=True); p.write_text(XML)
        idx=Path(td)/'catalog.json'; build_catalog(root,idx,Path(td)/'report.json'); raw=load(idx)
        assert raw['metadata']['ancestor_context_retained'] is True

        implicit_def=raw['objects']['ImplicitScaling'][0]
        roots=[x for x in implicit_def['descendants'] if x['depth']==1 and x['tag']=='VeterancyLevelArray']
        assert [x['same_tag_ordinal'] for x in roots]==[0,1]
        leaf=[x for x in implicit_def['descendants'] if x['tag']=='DamageDealtScaled'][1]
        assert leaf['ancestor_context'][0]['tag']=='VeterancyLevelArray'
        assert leaf['ancestor_context'][0]['same_tag_ordinal']==1

        eff,_=build_unit_effective_catalog_data(raw,['TestUnit'])
        graph,_=build_typed_reference_graph_data(eff)
        units,unr,counts=build_unit_entries_from_data(mapdb(),eff,graph)
        assert not unr
        vets=units['TestUnit']['scaling_evidence']['veterancy_behaviors']

        child=by_id(vets,'ChildScaling')
        assert [x['index'] for x in child['levels']]==[1,2,3]
        grouped=facts_by_level(child)
        values={k:[f['attrs']['value'] for f in v if f['path'][-1]=='VitalMaxArray'] for k,v in grouped.items()}
        assert values=={'1':['200'],'2':['30'],'3':['400']}, values
        assert all(f['veterancy_level_context']['status']=='RESOLVED' for f in child['nested_facts'])
        assert child['context_integrity']['orphan_nested_facts']==0
        assert child['context_integrity']['slot_mismatch_nested_facts']==0

        implicit=by_id(vets,'ImplicitScaling')
        assert [x['index'] for x in implicit['levels']]==[0,1]
        grouped=facts_by_level(implicit)
        values={k:[f['attrs']['value'] for f in v if f['path'][-1]=='DamageDealtScaled'] for k,v in grouped.items()}
        assert values=={'0':['1'],'1':['2']}, values
        assert {f['veterancy_level_context']['slot_source'] for f in implicit['nested_facts']}=={'IMPLICIT_ORDINAL'}

        assert counts['veterancy_nested_source_facts']==counts['veterancy_nested_contextualized_facts']
        assert counts['veterancy_nested_orphan_facts']==0
        assert counts['veterancy_nested_slot_mismatch_facts']==0
        print('PASS test_veterancy_context_v014')


if __name__=='__main__': test()
