from effective_catalog import EffectiveCatalogCompiler

def D(i,t,id,children,parent=None,file=None,mod=None):
    a={"id":id};
    if parent:a["parent"]=parent
    return {"object_id":id,"xml_type":t,"file":file or f"f{i}.xml","mod":mod,"attrs":a,"children":children,"descendants":[],"order":i}
def C(tag,**attrs):return {"tag":tag,"attrs":attrs}
def vals(r,tag):return [x["attrs"].get("value") for x in r["fields"].get(tag,[]) if x["attrs"].get("__hole__")!="1"]

def run():
    raw={"objects":{
      "HearthstoneSpecialCaseSet":[D(1,"CEffectSet","HearthstoneSpecialCaseSet",[C("EffectArray",value="Chen"),C("EffectArray",value="Zeratul"),C("EffectArray",value="Rexxar")],mod="heroesdata"),D(2,"CEffectSet","HearthstoneSpecialCaseSet",[C("EffectArray",value="Wizard")],mod="wizard")],
      "RegenGlobeImpactSet":[D(3,"CEffectSet","RegenGlobeImpactSet",[C("EffectArray",value="Base")]),D(4,"CEffectSet","RegenGlobeImpactSet",[C("EffectArray",value="Jaina")])],
      "SamuroMirrorImageTransferSet":[D(5,"CEffectSet","SamuroMirrorImageTransferSet",[C("EffectArray",value=str(i)) for i in range(10)])],
      "SamuroMirrorImageTransferSetNoShake":[D(6,"CEffectSet","SamuroMirrorImageTransferSetNoShake",[C("EffectArray",index="8",removed="1")],parent="SamuroMirrorImageTransferSet")],
      "AlarakTelekinesisImpactSet":[D(7,"CEffectSet","AlarakTelekinesisImpactSet",[C("EffectArray",value="A"),C("EffectArray",value="OLD"),C("EffectArray",value="C")])],
      "AlarakTelekinesisImpactSetTier1":[D(8,"CEffectSet","AlarakTelekinesisImpactSetTier1",[C("EffectArray",index="1",value="AlarakTelekinesisKnockbackSetTier1")],parent="AlarakTelekinesisImpactSet")],
      "StableIndexBase":[D(9,"CEffectSet","StableIndexBase",[C("EffectArray",value="A"),C("EffectArray",value="B"),C("EffectArray",value="C")])],
      "StableIndexChild":[D(10,"CEffectSet","StableIndexChild",[C("EffectArray",index="1",removed="1"),C("EffectArray",index="2",value="C2")],parent="StableIndexBase")],
      "BaseAccumulator":[D(11,"CAccumulator","BaseAccumulator",[C("MinAccumulation",value="0"),C("MaxAccumulation",value="100")])],
      "TokenAccumulator":[D(12,"CAccumulatorToken","TokenAccumulator",[C("MaxAccumulation",value="15")],parent="BaseAccumulator")],
    }}
    c=EffectiveCatalogCompiler(raw); out=c.compile(); R=out["records"]
    assert vals(R["CEffectSet::HearthstoneSpecialCaseSet"],"EffectArray")==["Chen","Zeratul","Rexxar","Wizard"]
    assert vals(R["CEffectSet::RegenGlobeImpactSet"],"EffectArray")==["Base","Jaina"]
    assert vals(R["CEffectSet::SamuroMirrorImageTransferSetNoShake"],"EffectArray")==[str(i) for i in range(8)]+["9"]
    assert vals(R["CEffectSet::AlarakTelekinesisImpactSetTier1"],"EffectArray")==["A","AlarakTelekinesisKnockbackSetTier1","C"]
    assert R["CEffectSet::HearthstoneSpecialCaseSet"]["fields"]["EffectArray"][-1]["provenance"]["mod"]=="wizard"
    stable=R["CEffectSet::StableIndexChild"]["fields"]["EffectArray"]
    assert stable[1]["attrs"].get("__hole__")=="1" and stable[1]["provenance"]["operation"]=="REMOVE"
    assert stable[2]["attrs"].get("value")=="C2"
    token=R["CAccumulatorToken::TokenAccumulator"]
    assert vals(token,"MinAccumulation")==["0"]
    assert vals(token,"MaxAccumulation")==["15"]
    assert token["inheritance_chain"][-1]=={"type":"CAccumulator","id":"BaseAccumulator"}
    assert not [u for u in out["unresolved"] if u.get("id")=="TokenAccumulator"]
    return True
if __name__=="__main__":print("PASS" if run() else "FAIL")
