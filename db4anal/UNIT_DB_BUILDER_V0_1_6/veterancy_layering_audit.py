from __future__ import annotations

import json
from collections import defaultdict


def _source(level):
    p=level.get('provenance') or {}
    return (p.get('mod'),p.get('file'))


def _clean_fact(f):
    return {
        'path':f.get('path') or [],
        'attrs':f.get('attrs') or {},
    }


def _level_signature(level, local_index):
    attrs=dict(level.get('attrs') or {})
    # Effective index belongs to the merged array, not the source segment identity.
    attrs.pop('__effective_index__',None)
    return {
        'local_index':local_index,
        'attrs':attrs,
        'nested_facts':[_clean_fact(x) for x in (level.get('nested_facts') or [])],
    }


def _segments(levels):
    segs=[]; cur=[]; src=None
    for lv in levels:
        s=_source(lv)
        if cur and s!=src:
            segs.append((src,cur)); cur=[]
        src=s; cur.append(lv)
    if cur: segs.append((src,cur))
    return segs


def _fingerprint(levels):
    return json.dumps([_level_signature(lv,i) for i,lv in enumerate(levels)],sort_keys=True,separators=(',',':'),ensure_ascii=False)


def audit_veterancy_layering(unit_db: dict) -> dict:
    """Find exact repeated source-layer level tables without resolving load semantics."""
    records=[]; behavior_contexts=0; duplicated=0
    for uid,u in (unit_db.get('units') or {}).items():
        for map_id,e in (u.get('by_map') or {}).items():
            for ve in ((e.get('scaling_evidence') or {}).get('veterancy_behaviors') or []):
                behavior_contexts += 1
                levels=ve.get('levels') or []
                segs=_segments(levels)
                groups=defaultdict(list)
                for si,(src,lvs) in enumerate(segs):
                    groups[_fingerprint(lvs)].append((si,src,lvs))
                dup_groups=[]
                for xs in groups.values():
                    if len(xs)<2: continue
                    # Require provenance-distinct segments; repeated identical blocks from the
                    # same source file are retained as ordinary source structure.
                    distinct={x[1] for x in xs}
                    if len(distinct)<2: continue
                    dup_groups.append({
                        'segment_indices':[x[0] for x in xs],
                        'segment_length':len(xs[0][2]),
                        'sources':[{'mod':x[1][0],'file':x[1][1]} for x in xs],
                        'classification':'EXACT_DUPLICATE_LEVEL_TABLE_ACROSS_PROVENANCE_SEGMENTS',
                        'runtime_load_meaning':'UNRESOLVED',
                        'proof_basis':[],
                    })
                if dup_groups:
                    duplicated += 1
                    records.append({
                        'unit_id':uid,'map_id':map_id,'behavior_id':ve.get('id'),'behavior_key':ve.get('key'),
                        'effective_level_count':len(levels),'source_segment_count':len(segs),
                        'duplicate_groups':dup_groups,
                        'status':'UNRESOLVED_LOAD_LAYERING_SEMANTICS',
                        'note':'Exact repeated source-layer tables are reported, not deduplicated. APPEND/override/load semantics must be proven separately.',
                    })
    return {
        'schema':{'name':'PYHOTS_VETERANCY_LAYERING_AUDIT','version':'0.1.6'},
        'contract':{'detect_exact_duplicates_only':True,'no_automatic_deduplication':True,'layering_semantics_fail_closed':True},
        'summary':{'veterancy_behavior_contexts':behavior_contexts,'contexts_with_exact_duplicate_layer_segments':duplicated,'audit_records':len(records)},
        'records':records,
    }
