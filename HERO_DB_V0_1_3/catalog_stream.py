from __future__ import annotations
import json, re, collections, sys
from pathlib import Path

def iter_objects(path, chunk_size=1<<20):
    dec=json.JSONDecoder()
    with open(path,'r',encoding='utf-8') as f:
        buf=''; eof=False
        marker='"objects"'
        # find marker and opening brace
        while marker not in buf:
            c=f.read(chunk_size)
            if not c: raise RuntimeError('objects marker not found')
            buf += c
            if len(buf)>4*chunk_size and marker not in buf: buf=buf[-2*chunk_size:]
        i=buf.index(marker)+len(marker)
        colon=buf.index(':',i); brace=buf.index('{',colon)
        buf=buf[brace+1:]
        while True:
            # ensure non-whitespace
            while True:
                j=0
                while j<len(buf) and (buf[j].isspace() or buf[j]==','): j+=1
                if j < len(buf):
                    buf=buf[j:]; break
                c=f.read(chunk_size)
                if not c: return
                buf += c
            if buf.startswith('}'): return
            # key
            while True:
                try:
                    key,end=dec.raw_decode(buf)
                    break
                except json.JSONDecodeError:
                    c=f.read(chunk_size)
                    if not c: raise
                    buf += c
            buf=buf[end:]
            # colon
            while True:
                k=0
                while k<len(buf) and buf[k].isspace(): k+=1
                if k<len(buf):
                    if buf[k] != ':': raise RuntimeError(f'expected colon after {key!r}: {buf[:80]!r}')
                    buf=buf[k+1:]; break
                c=f.read(chunk_size)
                if not c: raise EOFError
                buf+=c
            # whitespace then value; accumulate until dec works
            while True:
                k=0
                while k<len(buf) and buf[k].isspace(): k+=1
                if k<len(buf): buf=buf[k:]; break
                c=f.read(chunk_size)
                if not c: raise EOFError
                buf+=c
            while True:
                try:
                    value,end=dec.raw_decode(buf)
                    break
                except json.JSONDecodeError as e:
                    c=f.read(chunk_size)
                    if not c: raise
                    buf += c
            yield key,value
            buf=buf[end:]

def main():
    path=sys.argv[1]
    units=[]; kc=collections.Counter(); candidate=[]; n=0
    for oid,defs in iter_objects(path):
        n+=1
        uds=[d for d in defs if d.get('xml_type')=='CUnit']
        if not uds: continue
        for d in uds: kc.update(d.get('attrs',{}).keys())
        # classify candidate by id or literal Hero in attrs/children/descendants only
        txt=json.dumps(uds,ensure_ascii=False)
        if 'Hero' in oid or 'hero' in oid.lower() or 'Hero' in txt:
            candidate.append((oid,uds))
        units.append((oid,uds))
    print('objects',n,'unit ids',len(units),'candidates',len(candidate))
    print('attrs',kc.most_common(80))
    for oid,uds in candidate[:800]:
        d=uds[-1]
        print('CAND',oid,'attrs=',d.get('attrs',{}),'parent=',d.get('attrs',{}).get('parent'),'file=',d.get('file'))
        # print selected immediate children tags/attrs
        sel=[]
        for c in d.get('children',[]):
            if c.get('tag') in {'Hero','FlagArray','Attributes','Attribute','BehaviorArray','AbilArray','WeaponArray','Sight','LifeMax','EnergyMax','Speed','Radius','PlaneArray','UserData'}:
                sel.append(c)
        if sel: print('  children',sel[:30])

if __name__=='__main__': main()
