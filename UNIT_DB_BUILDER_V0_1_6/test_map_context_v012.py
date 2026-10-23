from pathlib import Path
from tempfile import TemporaryDirectory
from json_backend import load
from storm_catalog_index import build_catalog
from map_context import filter_catalog_for_map, definition_allowed
from unit_effective_catalog import build_unit_effective_catalog_data
from typed_reference_resolver import build_typed_reference_graph_data
from unit_semantics import build_unit_entries_from_data
from unit_db_builder import _append_provenance_leaks

COMMON='''<Catalog>
<CUnit id="KingsCore"><LifeMax value="1000"/><BehaviorArray Link="BaseCore"/></CUnit>
<CBehaviorBuff id="BaseCore"/>
<CBehaviorBuff id="ForeignCore"/>
<CBehaviorBuff id="TryModeLeak"/>
</Catalog>'''
BLACK='''<Catalog><CUnit id="KingsCore"><LifeMax value="1200"/></CUnit></Catalog>'''
FOREIGN='''<Catalog><CUnit id="KingsCore"><BehaviorArray Link="ForeignCore"/></CUnit></Catalog>'''
TRYMODE='''<Catalog><CUnit id="KingsCore"><BehaviorArray Link="TryModeLeak"/></CUnit></Catalog>'''
BRAXIS_DATA='''<Catalog><CUnit id="BraxisHoldoutSCV"><LifeMax value="777"/></CUnit></Catalog>'''
VOLSKAYA_DATA='''<Catalog><CUnit id="VolskayaMercCaptureSlab"><LifeMax value="888"/></CUnit></Catalog>'''


def mapdb(mid, uid):
    return {"maps":{mid:{"unit_types":[{"unit_type":uid,"replay_support":1,"spawn_count":1,"owner_classes":{"NON_ROSTER_CONTROL_ID":1}}],"semantic_landmarks":[]}}}


def write(root, rel, text):
    p=root/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(text)


def resolve_entry(raw, mid, uid):
    filt, rep=filter_catalog_for_map(raw,mid)
    eff,_=build_unit_effective_catalog_data(filt,[uid])
    graph,_=build_typed_reference_graph_data(eff)
    units,unr,_=build_unit_entries_from_data(mapdb(mid,uid),eff,graph)
    return units[uid],unr,rep


def test():
    with TemporaryDirectory() as td:
        root=Path(td)/'mods'
        write(root,'heroesdata.stormmod/base.stormdata/gamedata/unitdata.xml',COMMON)
        write(root,'heroesmapmods/battlegroundmapmods/blackheartsbay.stormmod/base.stormdata/gamedata/unitdata.xml',BLACK)
        # Not present in the map table on purpose: v0.1.1 treated unknown battleground
        # mods as shared and leaked them. v0.1.2 must reject by scope.
        write(root,'heroesmapmods/battlegroundmapmods/heavenhell.stormmod/base.stormdata/gamedata/unitdata.xml',FOREIGN)
        # Embedded single-player/test map inside a common outer stormmod.
        write(root,'heroes.stormmod/base.stormmaps/maps/heroes/singleplayermaps/(10)trymemode.stormmap/base.stormdata/gamedata/unitdata.xml',TRYMODE)
        # Proven ARAM dependencies from replay-observed exact unit identities.
        write(root,'heroesmapmods/battlegroundmapmods/braxisholdoutdata.stormmod/base.stormdata/gamedata/unitdata.xml',BRAXIS_DATA)
        write(root,'heroesmapmods/battlegroundmapmods/volskayadata.stormmod/base.stormdata/gamedata/unitdata.xml',VOLSKAYA_DATA)

        idx=Path(td)/'catalog.json'; build_catalog(root,idx,Path(td)/'report.json'); raw=load(idx)

        core,unr,rep=resolve_entry(raw,'BlackheartsBay','KingsCore')
        assert not unr
        assert core['base_stats']['LifeMax']['numeric_value']==1200
        ids={x['id'] for x in core['behaviors']}
        assert 'BaseCore' in ids
        assert 'ForeignCore' not in ids
        assert 'TryModeLeak' not in ids
        assert rep['drop_reasons'].get('FOREIGN_BATTLEGROUND_MOD',0)>=1
        assert rep['drop_reasons'].get('UNPROVEN_EMBEDDED_STORMMAP',0)>=1

        scv,unr,rep=resolve_entry(raw,'BraxisOutpost','BraxisHoldoutSCV')
        assert not unr and scv['semantics_status']=='RESOLVED_TYPED_CLOSURE'
        assert scv['base_stats']['LifeMax']['numeric_value']==777
        assert 'braxisholdoutdata.stormmod' in rep['dependency_map_mods']

        slab,unr,rep=resolve_entry(raw,'IndustrialDistrict','VolskayaMercCaptureSlab')
        assert not unr and slab['semantics_status']=='RESOLVED_TYPED_CLOSURE'
        assert slab['base_stats']['LifeMax']['numeric_value']==888
        assert 'volskayadata.stormmod' in rep['dependency_map_mods']

        # Direct policy checks for the two contamination classes.
        ok,_=definition_allowed({'file':r'C:\mods\heroesmapmods\battlegroundmapmods\overwatchdata.stormmod\x.xml','mod':'overwatchdata.stormmod'},'BlackheartsBay')
        assert not ok
        ok,_=definition_allowed({'file':r'C:\mods\heroes.stormmod\base.stormmaps\maps\heroes\singleplayermaps\startingexperience\tutorial01.stormmap\x.xml','mod':'heroes.stormmod'},'BlackheartsBay')
        assert not ok

        # Integrity must inspect effective-catalog definition provenance even when
        # no compact output field retained the foreign contribution.
        leaks=[]; seen=set()
        hidden={'records':{'CBehaviorBuff::X':{'definitions':[{'file':r'C:\mods\heroesmapmods\battlegroundmapmods\heavenhell.stormmod\x.xml','mod':'heavenhell.stormmod','order':9,'operation':'CONTRIBUTE'}]}}}
        _append_provenance_leaks(leaks,seen,hidden,'BlackheartsBay','effective_catalog')
        assert len(leaks)==1 and leaks[0]['source']=='effective_catalog'
        print('PASS test_map_context_v012')


if __name__=='__main__': test()
