#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

VERSION = "0.2"

def load_json_bytes(b: bytes) -> Any:
    return json.loads(b.decode("utf-8"))

class Source:
    def __init__(self, path: str):
        self.path = Path(path)
        self.z = zipfile.ZipFile(self.path) if self.path.is_file() and self.path.suffix.lower()==".zip" else None
    def names(self):
        if self.z: return self.z.namelist()
        return [str(p.relative_to(self.path)).replace('\\','/') for p in self.path.rglob('*') if p.is_file()]
    def read(self, basename: str):
        names=self.names()
        matches=[n for n in names if n==basename or n.endswith('/'+basename)]
        if not matches: return None
        n=min(matches,key=len)
        if self.z: return load_json_bytes(self.z.read(n))
        return json.loads((self.path/n).read_text(encoding='utf-8'))
    def read_all_suffix(self, suffix: str):
        out=[]
        for n in self.names():
            if n.endswith(suffix):
                try:
                    v=load_json_bytes(self.z.read(n)) if self.z else json.loads((self.path/n).read_text(encoding='utf-8'))
                    out.append((n,v))
                except Exception:
                    pass
        return out
    def close(self):
        if self.z: self.z.close()

def gate(ok: bool, detail: Any=None):
    return {"status":"PASS" if ok else "FAIL", "detail":detail}

def validate_protocol(path: str) -> dict:
    s=Source(path)
    try:
        summary=s.read('summary.json'); db=s.read('replay_protocol_db.json'); rv=s.read('runtime_variant_diagnostics.json')
        if not isinstance(summary,dict) or not isinstance(db,dict):
            return {"status":"FAIL","error":"missing summary.json or replay_protocol_db.json"}
        core=db.get('core_bindings',[]) or []
        seen=Counter((r.get('replay_build'),r.get('hero'),r.get('slot'),r.get('abil_link'),r.get('cmd_index'),r.get('runtime_context'),r.get('ability_id')) for r in core)
        exact_dups=sum(v-1 for v in seen.values() if v>1)
        confirmed=[]
        if isinstance(rv,dict):
            confirmed=[r for r in rv.get('rows',[]) if r.get('classification')=='PARENT_SUBABILITY_VARIANT_CONFIRMED_CROSS_REPLAY']
        claims=defaultdict(set)
        for r in confirmed:
            fam=(r.get('hero'),r.get('slot'),r.get('base_ability_id'),r.get('subability_id'))
            for k in ('base_numeric','subability_numeric'):
                x=r.get(k)
                if isinstance(x,dict): claims[(r.get('replay_build'),x.get('abil_link'),x.get('cmd_index'))].add(fam)
        promiscuous_confirmed={str(k):sorted(map(str,v)) for k,v in claims.items() if len(v)>1}
        manifest=summary.get('manifest_status_counts',{}) or {}
        source_replays=int(summary.get('source_replays',0) or 0)
        processed=int(manifest.get('PROCESSED',0) or 0)
        confirmed_bad_support=sum(int(r.get('cross_replay_distinct_replays',0) or 0)<2 for r in confirmed)
        active=db.get('active_talent_bindings',[]) or []
        confirmed_active=[r for r in active if r.get('status')=='CONFIRMED_ACTIVE_BINDING']
        active_bad=sum(len(r.get('numeric_identity_claimed_by',[]) or [])!=1 or int(r.get('distinct_replays',0) or 0)<2 for r in confirmed_active)
        gates={
            'PROCESSING':gate(source_replays>0 and processed==source_replays, {'source_replays':source_replays,'processed':processed}),
            'TRUE_CONFLICTS':gate(int(summary.get('true_conflicts',0) or 0)==0, summary.get('true_conflicts')),
            'BUILD_UNKNOWN_COLLISIONS':gate(int(summary.get('build_unknown_collisions',0) or 0)==0, summary.get('build_unknown_collisions')),
            'CORE_EXACT_DUPLICATES':gate(exact_dups==0, exact_dups),
            'RUNTIME_CROSS_REPLAY_SUPPORT':gate(confirmed_bad_support==0, {'confirmed_groups':len(confirmed),'bad_support':confirmed_bad_support}),
            'RUNTIME_NUMERIC_DISCRIMINATION':gate(not promiscuous_confirmed, {'promiscuous_confirmed_count':len(promiscuous_confirmed)}),
            'ACTIVE_TALENT_CONFIRMED_UNIQUENESS':gate(active_bad==0, {'confirmed':len(confirmed_active),'bad':active_bad}),
        }
        return {'status':'PASS' if all(x['status']=='PASS' for x in gates.values()) else 'FAIL','version':summary.get('protocol_db_version'),'gates':gates,'counts':{'core_bindings':len(core),'confirmed_runtime_groups':len(confirmed),'confirmed_active_talents':len(confirmed_active),'unresolved':summary.get('unresolved')}}
    finally: s.close()

def validate_world(path: str) -> dict:
    s=Source(path)
    try:
        integ=s.read('integrity_report.json')
        summary=s.read('summary.json')
        states=s.read_all_suffix('.world_state.json')
        if isinstance(integ,dict):
            return {'status':integ.get('world_state_integrity','UNKNOWN'),'gates':integ.get('gates',{}),'scope':integ.get('scope',{}),'position_scale_status_counts':integ.get('position_scale_status_counts',{}),'position_scale_best_counts':integ.get('position_scale_best_counts',{}),'source':'integrity_report.json'}
        if states:
            totals=Counter(); scale=Counter(); builds=Counter(); maps=Counter(); versions=Counter()
            max_tal=0
            for _,st in states:
                versions[str(st.get('schema',{}).get('version'))]+=1
                meta=st.get('metadata',{}); builds[str(meta.get('build'))]+=1; maps[str(meta.get('map_id') or 'UNKNOWN')]+=1
                players=st.get('players',{}) or {}; totals['players']+=len(players)
                totals['hero']+=sum(bool(p.get('hero_units')) for p in players.values()); totals['pos']+=sum(bool(p.get('position_anchors')) for p in players.values())
                integ1=st.get('integrity',{}) or {}; ps=integ1.get('position_scale',{}) or {}; scale[str(ps.get('status') or 'UNKNOWN')]+=1
                totals['hero_unresolved']+=int((integ1.get('player_identity',{}) or {}).get('hero_candidate_unresolved',0) or 0)
                for p in players.values():
                    t=p.get('talents',{})
                    if isinstance(t,dict): max_tal=max(max_tal,int(t.get('selected_count',0) or 0))
            gates={
                'HERO_OWNERSHIP':gate(totals['players']>0 and totals['hero']==totals['players'] and totals['hero_unresolved']==0,dict(totals)),
                'POSITION_COVERAGE':gate(totals['pos']==totals['players'],{'players':totals['players'],'with_positions':totals['pos']}),
                'POSITION_SCALE':gate(scale.get('FAIL_SCALE_MISMATCH',0)==0 and scale.get('UNKNOWN',0)==0,dict(scale)),
                'MAP_ID':gate(maps.get('UNKNOWN',0)==0,dict(maps)),
                'BUILD_PROVENANCE':gate(builds.get('None',0)==0 and builds.get('UNKNOWN',0)==0,dict(builds)),
                'TALENT_CANONICALIZATION':gate(max_tal<=7,{'max_selected_per_player':max_tal}),
            }
            return {'status':'PASS' if all(x['status']=='PASS' for x in gates.values()) else 'FAIL','gates':gates,'versions':dict(versions),'source':'world_state files'}
        # summary-only can validate coverage, but cannot validate coordinate scale.
        rs=s.read('replay_summaries.json')
        if isinstance(rs,list):
            # v0.1.4+ summaries explicitly scope non-ten-player replays. For older
            # summaries, derive the same quarantine only from observed player count.
            core=[]; quarantined=[]
            for x in rs:
                scope=x.get('replay_scope')
                if scope=='CORE_10_PLAYER' or (scope is None and int(x.get('players',0) or 0)==10): core.append(x)
                else: quarantined.append(x)
            players=sum(int(x.get('players',0) or 0) for x in core); hero=sum(int(x.get('players_with_hero_unit',0) or 0) for x in core); pos=sum(int(x.get('players_with_position_anchors',0) or 0) for x in core)
            unknown_maps=sum(not x.get('map_id') for x in core); unknown_builds=sum(x.get('build') is None for x in rs)
            gates={
                'QUARANTINE_ACCOUNTING':gate(len(core)+len(quarantined)==len(rs),{'core_replays':len(core),'quarantined_replays':len(quarantined)}),
                'HERO_OWNERSHIP':gate(players>0 and hero==players,{'players':players,'with_hero_unit':hero}),
                'POSITION_COVERAGE':gate(pos==players,{'players':players,'with_positions':pos}),
                'MAP_ID':gate(unknown_maps==0,unknown_maps),
                'BUILD_PROVENANCE':gate(unknown_builds==0,unknown_builds),
                'POSITION_SCALE':{'status':'UNVERIFIED','detail':'requires v0.1.3+ integrity_report.json or per-replay world_state files'},
                'TALENT_CANONICALIZATION':{'status':'UNVERIFIED','detail':'requires v0.1.3+ integrity_report.json for canonical talent gate'},
            }
            return {'status':'UNVERIFIED','gates':gates,'scope':{'core_replays':len(core),'quarantined_replays':len(quarantined)},'source':'replay_summaries.json only'}
        return {'status':'FAIL','error':'no recognizable World State output'}
    finally: s.close()

def main():
    ap=argparse.ArgumentParser(description='Pyhots Protocol/World-State integrity gate')
    ap.add_argument('--protocol-output')
    ap.add_argument('--world-state-output')
    ap.add_argument('--output',default='integrity_gate_report.json')
    a=ap.parse_args()
    if not a.protocol_output and not a.world_state_output: ap.error('provide --protocol-output and/or --world-state-output')
    out={'schema':{'name':'PYHOTS_DB_INTEGRITY_GATE','version':VERSION}}
    if a.protocol_output: out['protocol']=validate_protocol(a.protocol_output)
    if a.world_state_output: out['world_state']=validate_world(a.world_state_output)
    statuses=[v.get('status') for k,v in out.items() if k!='schema' and isinstance(v,dict)]
    out['overall']='PASS' if statuses and all(x=='PASS' for x in statuses) else ('FAIL' if 'FAIL' in statuses else 'UNVERIFIED')
    Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out['overall']=='PASS' else 2
if __name__=='__main__': raise SystemExit(main())
