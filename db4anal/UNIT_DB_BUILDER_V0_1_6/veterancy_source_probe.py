from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from json_backend import save

_TEXT_SUFFIXES={'.xml','.galaxy','.txt','.trig','.trigger','.sc2lib','.sc2mod','.stormmod'}
_ROOT_RE=re.compile(br'<\s*([A-Za-z_][A-Za-z0-9_.:-]*)')
_CONTEXT_KEYWORDS=(
    b'Veterancy', b'VeterancyXP', b'Experience', b'XP', b'GameTime', b'Level',
    b'Periodic', b'Timer', b'Modify', b'Add', b'Set', b'Grant', b'Increment',
)
_GENERIC_PROGRESSION_PATTERNS=(
    ('VETERANCY', re.compile(br'(?i)\bveteran(?:cy|cyxp|cylevel)?\b')),
    ('EXPERIENCE', re.compile(br'(?i)\b(?:experience|xp)\b')),
    ('LEVEL', re.compile(br'(?i)\b(?:unit\s*level|level)\b')),
)
_GENERIC_DRIVER_PATTERNS=(
    ('ADD_SET_MODIFY', re.compile(br'(?i)\b(?:add|set|modify|grant|give|change|increment|increase)\w*\b')),
    ('TIME_PERIODIC', re.compile(br'(?i)\b(?:game\s*time|gametime|periodic|timer|elapsed|second|minute)\w*\b')),
    ('UNIT_CONTEXT', re.compile(br'(?i)\b(?:unit|behavior|behaviour)\w*\b')),
)


def _detect_mod(path: Path) -> str | None:
    for part in path.parts:
        if part.lower().endswith('.stormmod'):
            return part
    return None


def _root_tag(data: bytes) -> str | None:
    m=_ROOT_RE.search(data[:8192])
    if not m: return None
    try: return m.group(1).decode('utf-8','replace')
    except Exception: return None


def _snippet(data: bytes, pos: int, radius: int=240) -> str:
    a=max(0,pos-radius); b=min(len(data),pos+radius)
    return data[a:b].decode('utf-8','replace').replace('\r',' ').replace('\n',' ')[:1200]


def _reference_class(path: Path, root_tag: str | None) -> str:
    low=str(path).lower().replace('\\','/')
    name=path.name.lower()
    if name in {'gamestrings.txt','strings.txt'} or '/localizeddata/' in low or '/localization/' in low:
        return 'LOCALIZATION_REFERENCE'
    if name == 'preload.xml' or root_tag in {'Preload','PreloadList'}:
        return 'PRELOAD_REFERENCE'
    if path.suffix.lower() in {'.galaxy','.trig','.trigger','.sc2lib'} or any(x in low for x in ('/triggers/','/triggerlib/','/scripts/','/script/')):
        return 'EXECUTION_CANDIDATE'
    if root_tag and any(x in root_tag.lower() for x in ('trigger','script','library')):
        return 'EXECUTION_CANDIDATE'
    return 'OTHER_NON_EXECUTION_REFERENCE'


def _iter_text_files(root: Path):
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES:
            yield path


def scan_veterancy_source_candidates(mods_root: Path, behavior_ids: Iterable[str], *, max_hits_per_behavior: int=50) -> dict:
    """Classify exact non-Catalog literal references to replay-relevant veterancy behaviors.

    v0.1.6 separates localization/preload noise from execution candidates.  Even an
    execution candidate is only provenance: literal reference != execution != level source.
    """
    root=Path(mods_root).resolve()
    ids=sorted({str(x) for x in behavior_ids if x})
    if not ids:
        return {
            'schema':{'name':'PYHOTS_VETERANCY_SOURCE_PROBE','version':'0.1.6'},
            'summary':{'behavior_ids':0,'files_scanned':0,'non_catalog_files_scanned':0,'exact_non_catalog_hits':0,'behaviors_with_non_catalog_hits':0,'read_errors':0,'class_counts':{}},
            'behaviors':{},'read_errors':[],
        }

    enc={x:x.encode('utf-8') for x in ids}
    alt=b'|'.join(sorted((re.escape(v) for v in enc.values()),key=len,reverse=True))
    pat=re.compile(br'(?<![A-Za-z0-9_])(' + alt + br')(?![A-Za-z0-9_])')
    by_bytes={v:k for k,v in enc.items()}
    hits=defaultdict(list); errors=[]; files_scanned=0; non_catalog_scanned=0; exact_hits=0; classes=Counter()

    for path in _iter_text_files(root):
        files_scanned += 1
        try: data=path.read_bytes()
        except Exception as e:
            errors.append({'file':str(path),'error':repr(e)}); continue
        tag=_root_tag(data)
        if tag == 'Catalog': continue
        non_catalog_scanned += 1
        cls=_reference_class(path,tag)
        for m in pat.finditer(data):
            bid=by_bytes.get(m.group(1))
            if not bid or len(hits[bid]) >= max_hits_per_behavior: continue
            around=data[max(0,m.start()-500):min(len(data),m.end()+500)]
            kws=[kw.decode('ascii','replace') for kw in _CONTEXT_KEYWORDS if kw.lower() in around.lower()]
            line=data.count(b'\n',0,m.start())+1
            hits[bid].append({
                'file':str(path),'mod':_detect_mod(path),'root_tag':tag,'line':line,'literal':bid,
                'context_keywords':kws,'snippet':_snippet(data,m.start()),
                'classification':'EXACT_NON_CATALOG_REFERENCE_NOT_EXECUTION_PROOF',
                'reference_class':cls,'semantic_status':'REFERENCE_ONLY_NOT_EXECUTION_PROOF',
            })
            exact_hits += 1; classes[cls] += 1

    behaviors={}
    for bid in ids:
        h=hits.get(bid,[])
        behaviors[bid]={
            'status':'CANDIDATE_SOURCE_EVIDENCE_ONLY' if h else 'NO_EXACT_NON_CATALOG_REFERENCE_FOUND',
            'level_source':'UNRESOLVED','proof_basis':[],
            'execution_candidates':[x for x in h if x.get('reference_class')=='EXECUTION_CANDIDATE'],
            'references':h,
            'exact_non_catalog_references':h,
        }
    return {
        'schema':{'name':'PYHOTS_VETERANCY_SOURCE_PROBE','version':'0.1.6'},
        'contract':{
            'exact_literal_only':True,'reference_is_not_execution':True,'source_claims_fail_closed':True,
            'classification_only':True,
            'note':'Localization and preload are separated from execution candidates. No class resolves runtime level source automatically.',
        },
        'summary':{
            'behavior_ids':len(ids),'files_scanned':files_scanned,'non_catalog_files_scanned':non_catalog_scanned,
            'exact_non_catalog_hits':exact_hits,'behaviors_with_non_catalog_hits':sum(1 for x in ids if hits.get(x)),
            'behaviors_with_execution_candidates':sum(1 for x in ids if any(h.get('reference_class')=='EXECUTION_CANDIDATE' for h in hits.get(x,[]))),
            'execution_candidate_hits':classes.get('EXECUTION_CANDIDATE',0),
            'class_counts':dict(sorted(classes.items())),'read_errors':len(errors),
        },
        'behaviors':behaviors,'read_errors':errors[:500],
    }


def scan_generic_veterancy_runtime_candidates(mods_root: Path, *, max_execution_candidates: int=1000, max_context_hits: int=500) -> dict:
    """Probe generic non-Catalog runtime text for veterancy/XP progression primitives.

    This intentionally does not require a concrete behavior ID.  A candidate must
    contain a progression token and at least one driver/action token in the same
    local window.  It remains CANDIDATE_ONLY until a typed execution chain exists.
    """
    root=Path(mods_root).resolve(); errors=[]; class_counts=Counter(); token_counts=Counter()
    files_scanned=0; non_catalog_scanned=0; execution=[]; contexts=[]
    for path in _iter_text_files(root):
        files_scanned += 1
        try: data=path.read_bytes()
        except Exception as e:
            errors.append({'file':str(path),'error':repr(e)}); continue
        tag=_root_tag(data)
        if tag == 'Catalog': continue
        non_catalog_scanned += 1
        cls=_reference_class(path,tag); class_counts[cls] += 1
        # Search progression tokens and classify a bounded local window.
        for pname,pat in _GENERIC_PROGRESSION_PATTERNS:
            for m in pat.finditer(data):
                around=data[max(0,m.start()-420):min(len(data),m.end()+420)]
                drivers=[name for name,p in _GENERIC_DRIVER_PATTERNS if p.search(around)]
                if not drivers: continue
                token_counts[pname] += 1
                item={
                    'file':str(path),'mod':_detect_mod(path),'root_tag':tag,
                    'line':data.count(b'\n',0,m.start())+1,'progression_token':pname,
                    'driver_tokens':drivers,'reference_class':cls,'snippet':_snippet(data,m.start()),
                    'status':'CANDIDATE_ONLY_NOT_EXECUTION_PROOF',
                    'level_source':'UNRESOLVED','proof_basis':[],
                }
                if cls=='EXECUTION_CANDIDATE':
                    if len(execution)<max_execution_candidates: execution.append(item)
                elif cls not in {'LOCALIZATION_REFERENCE','PRELOAD_REFERENCE'}:
                    if len(contexts)<max_context_hits: contexts.append(item)
    return {
        'schema':{'name':'PYHOTS_GENERIC_VETERANCY_RUNTIME_PROBE','version':'0.1.6'},
        'contract':{
            'generic_not_behavior_id_bound':True,'catalog_files_excluded':True,
            'localization_and_preload_not_execution':True,'candidate_is_not_execution':True,
            'source_claims_fail_closed':True,
        },
        'summary':{
            'files_scanned':files_scanned,'non_catalog_files_scanned':non_catalog_scanned,
            'execution_candidates':len(execution),'other_context_candidates':len(contexts),
            'file_class_counts':dict(sorted(class_counts.items())),'progression_token_hits':dict(sorted(token_counts.items())),
            'read_errors':len(errors),
        },
        'execution_candidates':execution,'other_context_candidates':contexts,'read_errors':errors[:500],
    }


def save_probe(path: Path, data: dict) -> None:
    save(path,data)
