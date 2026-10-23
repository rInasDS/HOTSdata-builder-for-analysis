from combat_effect_engine import extract

def D(oid,t,desc=None): return {"object_id":oid,"xml_type":t,"descendants":desc or [],"attrs":{},"file":"fixture.xml"}
def N(tag,**attrs): return {"tag":tag,"attrs":attrs,"path":[tag],"depth":1}
def run():
    objects={
      "A":[D("A","CAbilEffectInstant",[N("Effect",value="OwnedSet")])],
      "OwnedSet":[D("OwnedSet","CEffectSet",[N("EffectArray",value="OwnedDamage")])],
      "OwnedDamage":[D("OwnedDamage","CEffectDamage",[N("Amount",value="100")])],
      "ForeignDamage":[D("ForeignDamage","CEffectDamage",[N("Amount",value="999")])],
    }
    a={"id":"A","record_key":"Hero:A","name":"A"}
    x=extract(objects,a)
    assert [d["effect_id"] for d in x["damage"]]==["OwnedDamage"],x["damage"]
    assert x["damage"][0]["evidence"]=="REVERSE_PROVENANCE_TERMINAL"
    assert "ForeignDamage" not in str(x)
    assert x["graph_nodes"]==3,x["graph_nodes"]
    return True
if __name__=="__main__": print("PASS" if run() else "FAIL")
