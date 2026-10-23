from pathlib import Path
import tempfile
from storm_catalog_index import StormCatalogIndex

def run():
    with tempfile.TemporaryDirectory() as td:
        td=Path(td)
        (td/'a.xml').write_text('''<Catalog>
          <CAbilEffectInstant id="RealAbility"><Cost><Cooldown id="NestedFake" TimeUse="4"/></Cost></CAbilEffectInstant>
          <CTalent id="RealTalent"><Abil value="RealAbility"/></CTalent>
        </Catalog>''')
        (td/'preload.xml').write_text('''<Preload><Effect id="PreloadFake"/><Talent id="TalentFake"/></Preload>''')
        idx=StormCatalogIndex(td); idx.build()
        assert 'RealAbility' in idx.definitions_by_id
        assert 'RealTalent' in idx.definitions_by_id
        assert 'NestedFake' not in idx.definitions_by_id
        assert 'PreloadFake' not in idx.definitions_by_id
        assert 'TalentFake' not in idx.definitions_by_id
        assert idx.non_catalog_files_skipped==1
    return True
if __name__=='__main__': print('PASS' if run() else 'FAIL')
