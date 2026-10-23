from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess, sys

from json_backend import save, load

CAT='''<Catalog>
<CUnit id="TestUnit"><LifeMax value="1000"/><BehaviorArray Link="ProbeScaling"/></CUnit>
<CBehaviorVeterancy id="ProbeScaling">
  <XPFraction index="Kills" value="0"/>
  <VeterancyLevelArray/>
  <VeterancyLevelArray MinVeterancyXP="1"><Modification><VitalMaxArray index="Life" value="100"/></Modification></VeterancyLevelArray>
  <VeterancyLevelArray MinVeterancyXP="1"/>
</CBehaviorVeterancy>
</Catalog>'''


def test():
    with TemporaryDirectory() as td:
        td=Path(td); mods=td/'mods'; out=td/'out'
        p=mods/'heroesdata.stormmod/base.stormdata/gamedata/data.xml'; p.parent.mkdir(parents=True); p.write_text(CAT)
        t=mods/'heroesdata.stormmod/base.stormdata/triggers/test.xml'; t.parent.mkdir(parents=True); t.write_text('<TriggerData><Action value="ProbeScaling VeterancyXP Periodic"/></TriggerData>')
        mapdb={
            'schema':{'name':'PYHOTS_MAP_DB','version':'0.2'},
            'maps':{'TowersOfDoom':{
                'unit_types':[{'unit_type':'TestUnit','replay_support':1,'spawn_count':1,'owner_classes':{'NON_ROSTER_CONTROL_ID':1}}],
                'semantic_landmarks':[]
            }}
        }
        mp=td/'map_db.json'; save(mp,mapdb)
        subprocess.run([sys.executable,str(Path(__file__).with_name('unit_db_builder.py')),
                        '--map-db',str(mp),'--mods-root',str(mods),'--output-dir',str(out)],check=True)
        integ=load(out/'unit_integrity_report.json')
        assert integ['overall']=='PASS', integ
        for gate in ('VETERANCY_SEMANTIC_PROFILE_RETENTION','VETERANCY_OPERATION_FAIL_CLOSED',
                     'VETERANCY_CUMULATIVE_FAIL_CLOSED','VETERANCY_EMPTY_SLOT_FAIL_CLOSED',
                     'VETERANCY_LEVEL_SOURCE_FAIL_CLOSED','VETERANCY_SOURCE_PROBE_COVERAGE',
                     'VETERANCY_SOURCE_PROBE_FAIL_CLOSED'):
            assert integ['gates'][gate]=='PASS', (gate,integ['gates'][gate])
        db=load(out/'unit_db.json')
        ve=db['units']['TestUnit']['by_map']['TowersOfDoom']['scaling_evidence']['veterancy_behaviors'][0]
        sp=ve['semantic_profile']
        assert sp['level_source']['source']=='UNRESOLVED'
        assert sp['application_semantics']['field_operations'][0]['operation']=='UNRESOLVED'
        assert sp['empty_slot_semantics']['runtime_meaning']=='UNRESOLVED'
        probe=load(out/'veterancy_source_probe.json')
        assert probe['summary']['exact_non_catalog_hits']==1
        assert probe['behaviors']['ProbeScaling']['level_source']=='UNRESOLVED'
        print('PASS test_builder_v015')

if __name__=='__main__': test()
