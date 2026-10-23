from pathlib import Path
from tempfile import TemporaryDirectory
from json_backend import save
from static_map_probe import run

XML='''<Catalog>
<CValidatorLocationShrub id="TargetInShrub" />
<CBehaviorBuff id="ShrubStatusController"><DamageResponse Chance="1" Handled="ShrubEffect"/></CBehaviorBuff>
<CFootprint id="BrushFootprint"><Shape Radius="2"/></CFootprint>
</Catalog>'''
NON='''<MapData><BrushRegion x="10" y="20" radius="3"/></MapData>'''

def test():
  with TemporaryDirectory() as td:
    root=Path(td); mods=root/'mods'/'x.stormmod'; mods.mkdir(parents=True)
    (mods/'a.xml').write_text(XML); (mods/'map.xml').write_text(NON)
    mapdb=root/'map.json'; save(mapdb,{"maps":{"TestMap":{"map_name_observations":{"Test Map":1}}}})
    r=run(root/'mods',mapdb,root/'out')
    assert r['shrub_runtime_mechanics']['status']=='PROVEN'
    assert r['shrub_geometry']['status']=='CANDIDATE_SOURCE_FOUND'
    print('PASS test_static_map_probe_v01')
if __name__=='__main__': test()
