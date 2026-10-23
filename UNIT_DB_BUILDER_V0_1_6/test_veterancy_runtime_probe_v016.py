from pathlib import Path
from tempfile import TemporaryDirectory
from veterancy_source_probe import scan_veterancy_source_candidates, scan_generic_veterancy_runtime_candidates


def main():
    with TemporaryDirectory() as td:
        root=Path(td)
        loc=root/'x.stormmod'/'enUS.stormdata'/'LocalizedData'; loc.mkdir(parents=True)
        (loc/'GameStrings.txt').write_text('HorsemanDefenderScaling Veterancy XP Level',encoding='utf-8')
        pre=root/'x.stormmod'/'base.stormdata'; pre.mkdir(parents=True)
        (pre/'preload.xml').write_text('<Preload><Behavior value="HorsemanDefenderScaling"/></Preload>',encoding='utf-8')
        trg=root/'x.stormmod'/'base.stormdata'/'Triggers'; trg.mkdir(parents=True)
        (trg/'runtime.galaxy').write_text('void f(){ AddVeterancyXP(unit, 1); SetUnitLevel(unit, UnitLevel(unit)+1); }',encoding='utf-8')
        exact=scan_veterancy_source_candidates(root,['HorsemanDefenderScaling'])
        cc=exact['summary']['class_counts']
        assert cc['LOCALIZATION_REFERENCE']==1 and cc['PRELOAD_REFERENCE']==1
        assert exact['summary']['execution_candidate_hits']==0
        generic=scan_generic_veterancy_runtime_candidates(root)
        assert generic['summary']['execution_candidates']>0
        assert all(x['status']=='CANDIDATE_ONLY_NOT_EXECUTION_PROOF' for x in generic['execution_candidates'])
        assert all(x['level_source']=='UNRESOLVED' for x in generic['execution_candidates'])
    print('PASS test_veterancy_runtime_probe_v016')

if __name__=='__main__': main()
