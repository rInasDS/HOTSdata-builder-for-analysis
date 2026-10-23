from pathlib import Path
from tempfile import TemporaryDirectory

from veterancy_source_probe import scan_veterancy_source_candidates


def test():
    with TemporaryDirectory() as td:
        root=Path(td)/'mods'
        c=root/'base.stormmod/base.stormdata/gamedata/data.xml'; c.parent.mkdir(parents=True)
        c.write_text('<Catalog><CBehaviorVeterancy id="ProbeScaling"/></Catalog>')
        t=root/'map.stormmod/base.stormdata/triggers/source.xml'; t.parent.mkdir(parents=True)
        t.write_text('<TriggerData><Action value="ProbeScaling VeterancyXP Periodic"/></TriggerData>')
        r=scan_veterancy_source_candidates(root,['ProbeScaling'])
        s=r['summary']
        assert s['behavior_ids']==1
        assert s['exact_non_catalog_hits']==1
        b=r['behaviors']['ProbeScaling']
        assert b['level_source']=='UNRESOLVED'
        assert not b['proof_basis']
        h=b['exact_non_catalog_references'][0]
        assert h['classification']=='EXACT_NON_CATALOG_REFERENCE_NOT_EXECUTION_PROOF'
        assert h['root_tag']=='TriggerData'
        assert 'VeterancyXP' in h['context_keywords']
        print('PASS test_veterancy_source_probe_v015')


if __name__=='__main__': test()
