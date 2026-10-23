from pathlib import Path
from tempfile import TemporaryDirectory

from json_backend import load
from storm_catalog_index import build_catalog
from unit_effective_catalog import build_unit_effective_catalog_data
from typed_reference_resolver import build_typed_reference_graph_data
from unit_semantics import build_unit_entries_from_data

XML='''<Catalog>
<CUnit id="TestUnit"><BehaviorArray Link="ProbeScaling"/></CUnit>
<CBehaviorVeterancy id="ProbeScaling">
  <BehaviorFlags index="Permanent" value="1"/>
  <XPFraction index="Kills" value="0"/>
  <VeterancyLevelArray/>
  <VeterancyLevelArray MinVeterancyXP="1">
    <Modification KillXPBonus="10">
      <DamageDealtScaled index="Basic" value="2.6"/>
    </Modification>
  </VeterancyLevelArray>
  <VeterancyLevelArray MinVeterancyXP="1">
    <Modification><VitalMaxArray index="Life" value="180"/></Modification>
  </VeterancyLevelArray>
  <VeterancyLevelArray MinVeterancyXP="1"/>
</CBehaviorVeterancy>
</Catalog>'''


def mapdb():
    return {"maps":{"TestMap":{"unit_types":[{"unit_type":"TestUnit","replay_support":1,"spawn_count":1,"owner_classes":{"NON_ROSTER_CONTROL_ID":1}}],"semantic_landmarks":[]}}}


def test():
    with TemporaryDirectory() as td:
        root=Path(td)/'mods'; p=root/'heroesdata.stormmod/base.stormdata/gamedata/data.xml'; p.parent.mkdir(parents=True); p.write_text(XML)
        idx=Path(td)/'catalog.json'; build_catalog(root,idx,Path(td)/'report.json'); raw=load(idx)
        eff,_=build_unit_effective_catalog_data(raw,['TestUnit'])
        graph,_=build_typed_reference_graph_data(eff)
        units,unr,counts=build_unit_entries_from_data(mapdb(),eff,graph)
        assert not unr
        vet=units['TestUnit']['scaling_evidence']['veterancy_behaviors'][0]
        assert 'XPFraction' in vet['fields']
        sp=vet['semantic_profile']

        st=sp['structure']
        assert st['level_count']==4, st
        assert st['empty_levels']==[0,3], st
        assert st['trailing_empty_levels']==[3], st
        assert st['first_level_with_nested_facts']==1
        assert st['last_level_with_nested_facts']==2

        req=sp['level_requirement_evidence']
        assert req['status']=='GAMEDATA_FIELD_EVIDENCE_ONLY'
        assert req['semantic_interpretation']=='UNRESOLVED'
        assert [x['level_index'] for x in req['requirements']]==[1,2,3]
        assert [x['numeric_value'] for x in req['requirements']]==[1,1,1]

        xp=sp['xp_field_evidence']
        assert xp['XPFraction'][0]['attrs']=={'index':'Kills','value':'0'}
        # v0.1.5 must not turn the field name/value into an execution/source claim.
        source=sp['level_source']
        assert source['status']=='UNRESOLVED'
        assert source['source']=='UNRESOLVED'
        assert not source['proof_basis']
        assert any(x['relation']=='UNIT_HAS_BEHAVIOR' for x in source['incoming_reference_evidence'])
        assert all(x['classification']=='REFERENCE_OR_ATTACHMENT_ONLY_NOT_LEVEL_SOURCE_PROOF' for x in source['incoming_reference_evidence'])

        app=sp['application_semantics']
        assert app['cumulative']['value']=='UNRESOLVED'
        assert not app['cumulative']['proof_basis']
        ops={x['field_signature']:x for x in app['field_operations']}
        assert set(ops)=={
            'Modification.KillXPBonus',
            'Modification.DamageDealtScaled[Basic]',
            'Modification.VitalMaxArray[Life]',
        }, ops.keys()
        assert all(x['operation']=='UNRESOLVED' and not x['proof_basis'] for x in ops.values())
        assert ops['Modification.KillXPBonus']['distinct_raw_values']==['10']
        assert ops['Modification.DamageDealtScaled[Basic]']['distinct_raw_values']==['2.6']
        assert ops['Modification.VitalMaxArray[Life]']['distinct_raw_values']==['180']
        assert ops['Modification.KillXPBonus']['observation_count']==1

        empty=sp['empty_slot_semantics']
        assert empty['runtime_meaning']=='UNRESOLVED'
        assert empty['trailing_empty_levels']==[3]
        assert not empty['proof_basis']

        assert counts['veterancy_semantic_profile_records']==1
        assert counts['veterancy_semantic_profile_mismatches']==0
        assert counts['veterancy_level_requirement_field_records']==1
        assert counts['veterancy_xp_field_evidence_records']==1
        assert counts['veterancy_level_source_resolved_records']==0
        assert counts['veterancy_operation_fields']==3
        assert counts['veterancy_operation_resolved_fields']==0
        assert counts['veterancy_cumulative_resolved_records']==0
        assert counts['veterancy_empty_slot_runtime_resolved_records']==0
        assert counts['veterancy_unsupported_operation_claims']==0
        assert counts['veterancy_unsupported_source_claims']==0
        assert counts['veterancy_unsupported_cumulative_claims']==0
        assert counts['veterancy_unsupported_empty_slot_claims']==0
        assert counts['veterancy_trailing_empty_records']==1
        print('PASS test_veterancy_semantics_v015')


if __name__=='__main__': test()
