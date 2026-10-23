from pathlib import Path
from tempfile import TemporaryDirectory
from json_backend import load
from storm_catalog_index import build_catalog
from unit_effective_catalog import build_unit_effective_catalog_data
from typed_reference_resolver import build_typed_reference_graph_data
from unit_semantics import build_unit_entries_from_data

XML='''<Catalog>
<CUnit id="KingsCore">
  <BehaviorArray Link="TownRampingArmorShred"/>
  <BehaviorArray Link="CoreScaling"/>
</CUnit>
<CBehaviorBuff id="TownRampingArmorShred">
  <Duration value="3"/>
  <MaxStackCount value="2"/>
  <ArmorModification StackCount="8"><AllArmorBonus value="-20"/></ArmorModification>
</CBehaviorBuff>
<CBehaviorVeterancy id="CoreScaling">
  <VeterancyLevelArray index="0">
    <Modification><VitalMaxArray index="Life" value="1.0"/></Modification>
  </VeterancyLevelArray>
  <VeterancyLevelArray index="1">
    <Modification><VitalMaxArray index="Life" value="1.1"/></Modification>
  </VeterancyLevelArray>
</CBehaviorVeterancy>
</Catalog>'''


def mapdb():
    return {"maps":{"TestMap":{"unit_types":[{"unit_type":"KingsCore","replay_support":1,"spawn_count":1,"owner_classes":{"NON_ROSTER_CONTROL_ID":1}}],"semantic_landmarks":[]}}}


def test():
    with TemporaryDirectory() as td:
        root=Path(td)/'mods'; p=root/'heroesdata.stormmod/base.stormdata/gamedata/data.xml'; p.parent.mkdir(parents=True); p.write_text(XML)
        idx=Path(td)/'catalog.json'; build_catalog(root,idx,Path(td)/'report.json'); raw=load(idx)
        eff,_=build_unit_effective_catalog_data(raw,['KingsCore'])
        graph,_=build_typed_reference_graph_data(eff)
        units,unr,counts=build_unit_entries_from_data(mapdb(),eff,graph)
        assert not unr
        u=units['KingsCore']
        armor=next(x for x in u['behaviors'] if x['id']=='TownRampingArmorShred')
        assert armor['fields']['MaxStackCount']['numeric_value']==2
        assert armor['fields']['ArmorModification']['attrs']['StackCount']=='8'
        bonus=[x for x in armor['nested_mechanics'] if x['path']==['ArmorModification','AllArmorBonus']]
        assert bonus and bonus[0]['attrs']['value']=='-20'

        vets=u['scaling_evidence']['veterancy_behaviors']
        assert len(vets)==1 and vets[0]['id']=='CoreScaling'
        assert 'VeterancyLevelArray' in vets[0]['fields']
        vals=[x for x in vets[0]['nested_facts'] if x['path'][-1]=='VitalMaxArray']
        assert {x['attrs']['value'] for x in vals}=={'1.0','1.1'}
        assert u['scaling_evidence']['status']=='EVIDENCE_ONLY_NOT_EVALUATED'

        assert counts['armor_semantic_source_records']==counts['armor_semantic_retained_records']==1
        assert counts['veterancy_source_records']==counts['veterancy_retained_records']==1
        print('PASS test_semantics_retention_v012')


if __name__=='__main__': test()
