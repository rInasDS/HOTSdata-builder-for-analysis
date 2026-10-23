from veterancy_layering_audit import audit_veterancy_layering


def level(i,mod,file,value=None,req=None):
    nf=[] if value is None else [{'path':['VeterancyLevelArray','Modification','VitalMaxArray'],'attrs':{'index':'Life','value':str(value)}}]
    return {'index':i,'attrs':{} if req is None else {'MinVeterancyXP':str(req)},'provenance':{'mod':mod,'file':file},'nested_facts':nf}


def main():
    a=[level(0,'a.stormmod','a.xml'),level(1,'a.stormmod','a.xml',100,1),level(2,'a.stormmod','a.xml',200,1)]
    b=[level(3,'b.stormmod','b.xml'),level(4,'b.stormmod','b.xml',100,1),level(5,'b.stormmod','b.xml',200,1)]
    db={'units':{'U':{'by_map':{'M':{'scaling_evidence':{'veterancy_behaviors':[{'id':'Scale','key':'CBehaviorVeterancy::Scale','levels':a+b}]}}}}}}
    out=audit_veterancy_layering(db)
    assert out['summary']['veterancy_behavior_contexts']==1
    assert out['summary']['contexts_with_exact_duplicate_layer_segments']==1
    r=out['records'][0]
    assert r['status']=='UNRESOLVED_LOAD_LAYERING_SEMANTICS'
    assert r['duplicate_groups'][0]['runtime_load_meaning']=='UNRESOLVED'
    print('PASS test_veterancy_layering_v016')

if __name__=='__main__': main()
