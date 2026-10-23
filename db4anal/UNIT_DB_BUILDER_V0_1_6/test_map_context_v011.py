from pathlib import Path
from tempfile import TemporaryDirectory
from json_backend import save, load
from storm_catalog_index import build_catalog
from map_context import filter_catalog_for_map
from unit_effective_catalog import build_unit_effective_catalog_data
from typed_reference_resolver import build_typed_reference_graph_data
from unit_semantics import build_unit_entries_from_data

COMMON='''<Catalog>
<CUnit id="KingsCore"><LifeMax value="1000"/><BehaviorArray Link="BaseCore"/></CUnit>
<CBehaviorBuff id="BaseCore"/>
<CBehaviorBuff id="BlackheartCore"/>
<CBehaviorBuff id="TowerCore"/>
<CBehaviorBuff id="BrawlOnly"/>
</Catalog>'''
BLACK='''<Catalog><CUnit id="KingsCore"><LifeMax value="1200"/><BehaviorArray Link="BlackheartCore"/></CUnit></Catalog>'''
TOWERS='''<Catalog><CUnit id="KingsCore"><LifeMax value="40"/><BehaviorArray Link="TowerCore"/></CUnit></Catalog>'''
BRAWL='''<Catalog><CUnit id="KingsCore"><BehaviorArray Link="BrawlOnly"/></CUnit></Catalog>'''

def mapdb(mid):
    return {"maps":{mid:{"unit_types":[{"unit_type":"KingsCore","replay_support":3,"spawn_count":6,"owner_classes":{"NON_ROSTER_CONTROL_ID":6}}],"semantic_landmarks":[]}}}

def write(root,rel,text):
    p=root/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(text)

def entry(raw,mid):
    filt,rep=filter_catalog_for_map(raw,mid)
    eff,_=build_unit_effective_catalog_data(filt,["KingsCore"])
    graph,_=build_typed_reference_graph_data(eff)
    units,unr,_=build_unit_entries_from_data(mapdb(mid),eff,graph)
    assert not unr
    return units["KingsCore"],rep

def test():
  with TemporaryDirectory() as td:
    root=Path(td)/'mods'
    write(root,'heroesdata.stormmod/base.stormdata/gamedata/unitdata.xml',COMMON)
    write(root,'heroesmapmods/battlegroundmapmods/blackheartsbay.stormmod/base.stormdata/gamedata/unitdata.xml',BLACK)
    write(root,'heroesmapmods/battlegroundmapmods/towersofdoom.stormmod/base.stormdata/gamedata/unitdata.xml',TOWERS)
    write(root,'heroesbrawlmods/arenamodemods/attackdefend.stormmod/base.stormdata/gamedata/unitdata.xml',BRAWL)
    idx=Path(td)/'catalog.json'; build_catalog(root,idx,Path(td)/'report.json'); raw=load(idx)
    b,br=entry(raw,'BlackheartsBay'); t,tr=entry(raw,'TowersOfDoom'); c,cr=entry(raw,'CursedHollow')
    assert b['base_stats']['LifeMax']['numeric_value']==1200
    assert t['base_stats']['LifeMax']['numeric_value']==40
    assert c['base_stats']['LifeMax']['numeric_value']==1000
    b_ids={x['id'] for x in b['behaviors']}; t_ids={x['id'] for x in t['behaviors']}; c_ids={x['id'] for x in c['behaviors']}
    assert 'BlackheartCore' in b_ids and 'TowerCore' not in b_ids and 'BrawlOnly' not in b_ids
    assert 'TowerCore' in t_ids and 'BlackheartCore' not in t_ids and 'BrawlOnly' not in t_ids
    assert 'BlackheartCore' not in c_ids and 'TowerCore' not in c_ids and 'BrawlOnly' not in c_ids
    assert br['dropped_definitions']>=2 and tr['dropped_definitions']>=2
    print('PASS test_map_context_v011')
if __name__=='__main__': test()
