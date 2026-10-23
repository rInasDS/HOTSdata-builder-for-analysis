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
</CBehaviorVeterancy>
</Catalog>'''

def test():
    with TemporaryDirectory() as td:
        td=Path(td); mods=td/'mods'; out=td/'out'
        p=mods/'heroesdata.stormmod/base.stormdata/gamedata/data.xml'; p.parent.mkdir(parents=True); p.write_text(CAT)
        trg=mods/'heroesdata.stormmod/base.stormdata/triggers/runtime.galaxy'; trg.parent.mkdir(parents=True)
        trg.write_text('void f(){ AddVeterancyXP(unit, 1); SetUnitLevel(unit, UnitLevel(unit)+1); }')
        mapdb={'schema':{'name':'PYHOTS_MAP_DB','version':'0.2'},'maps':{'TowersOfDoom':{
            'unit_types':[{'unit_type':'TestUnit','replay_support':1,'spawn_count':1,'owner_classes':{'NON_ROSTER_CONTROL_ID':1}}],
            'semantic_landmarks':[]}}}
        mp=td/'map_db.json'; save(mp,mapdb)
        subprocess.run([sys.executable,str(Path(__file__).with_name('unit_db_builder.py')),
                        '--map-db',str(mp),'--mods-root',str(mods),'--output-dir',str(out)],check=True)
        integ=load(out/'unit_integrity_report.json')
        assert integ['overall']=='PASS',integ
        for gate in ('VETERANCY_RUNTIME_PROBE_COVERAGE','VETERANCY_RUNTIME_PROBE_FAIL_CLOSED',
                     'VETERANCY_LAYERING_AUDIT_COVERAGE','VETERANCY_LAYERING_AUDIT_FAIL_CLOSED'):
            assert integ['gates'][gate]=='PASS',(gate,integ['gates'][gate])
        rt=load(out/'veterancy_runtime_probe.json')
        assert rt['summary']['execution_candidates']>0
        assert all(x['level_source']=='UNRESOLVED' for x in rt['execution_candidates'])
        la=load(out/'veterancy_layering_audit.json')
        assert la['summary']['veterancy_behavior_contexts']==1
        assert la['summary']['contexts_with_exact_duplicate_layer_segments']==0
        print('PASS test_builder_v016')

if __name__=='__main__': test()
