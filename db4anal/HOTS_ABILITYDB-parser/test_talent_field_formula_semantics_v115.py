from pathlib import Path
import tempfile

from json_backend import save, load
from combat_typed_engine import run_combat
from combat_formula_model import ability_damage_models
from replay_schema_v1 import normalize_ability


def _prov():
    return {"file":"fixture.xml","order":1}


def _edge(src,tgt,rel,field,*,ownership=False,execution=False,condition=False,modifier=False):
    return {
        "source_key":src,"target_key":tgt,"target_id":tgt.split("::",1)[1],
        "relation":rel,"field":field,"path":[field],"attr":"value",
        "propagation":{"dependency":True,"ownership":ownership,"execution":execution,
                       "condition":condition,"modifier":modifier,"formula":False},
    }


def run():
    records={
        "CAbilEffectInstant::A":{"object_id":"A","xml_type":"CAbilEffectInstant","fields":{},"nested_facts":[]},
        "CEffectSet::TalentGate":{"object_id":"TalentGate","xml_type":"CEffectSet","fields":{"Chance":[{"attrs":{"value":"0"},"provenance":_prov()}]},"nested_facts":[]},
        "CEffectDamage::D":{"object_id":"D","xml_type":"CEffectDamage","fields":{
            "Amount":[{"attrs":{"value":"100"},"provenance":_prov()}],
            "MultiplicativeModifierArray":[{"attrs":{"index":"TalentScale","Modifier":"-0.4","Validator":"VFormula"},"provenance":_prov()}],
        },"nested_facts":[]},
        "CEffectSet::DormantGate":{"object_id":"DormantGate","xml_type":"CEffectSet","fields":{"Chance":[{"attrs":{"value":"0"},"provenance":_prov()}]},"nested_facts":[]},
        "CEffectDamage::DormantDamage":{"object_id":"DormantDamage","xml_type":"CEffectDamage","fields":{"Amount":[{"attrs":{"value":"999"},"provenance":_prov()}]},"nested_facts":[]},
        "CValidatorPlayerTalent::VFormula":{"object_id":"VFormula","xml_type":"CValidatorPlayerTalent","fields":{},"nested_facts":[]},
        "CTalent::TalentEnable":{"object_id":"TalentEnable","xml_type":"CTalent","fields":{},"nested_facts":[
            {"tag":"Type","path":["AbilityModificationArray","Modifications","Type"],"attrs":{"value":"FlatModification"},"provenance":_prov()},
            {"tag":"Catalog","path":["AbilityModificationArray","Modifications","Catalog"],"attrs":{"value":"Effect"},"provenance":_prov()},
            {"tag":"Entry","path":["AbilityModificationArray","Modifications","Entry"],"attrs":{"value":"TalentGate"},"provenance":_prov()},
            {"tag":"Field","path":["AbilityModificationArray","Modifications","Field"],"attrs":{"value":"Chance"},"provenance":_prov()},
            {"tag":"Value","path":["AbilityModificationArray","Modifications","Value"],"attrs":{"value":"1.0"},"provenance":_prov()},
        ]},
        "CTalent::TalentFormula":{"object_id":"TalentFormula","xml_type":"CTalent","fields":{},"nested_facts":[]},
    }
    typed={"edges":[
        _edge("CAbilEffectInstant::A","CEffectSet::TalentGate","EXECUTES","Effect",ownership=True,execution=True),
        _edge("CEffectSet::TalentGate","CEffectDamage::D","EXECUTES","Effect",ownership=True,execution=True),
        _edge("CAbilEffectInstant::A","CEffectSet::DormantGate","EXECUTES","Effect",ownership=True,execution=True),
        _edge("CEffectSet::DormantGate","CEffectDamage::DormantDamage","EXECUTES","Effect",ownership=True,execution=True),
        _edge("CEffectDamage::D","CValidatorPlayerTalent::VFormula","CONDITIONS","MultiplicativeModifierArray",condition=True),
        _edge("CValidatorPlayerTalent::VFormula","CTalent::TalentFormula","CHECKS_TALENT","Value",condition=True),
        _edge("CTalent::TalentEnable","CEffectSet::TalentGate","TALENT_MODIFICATION_TARGET","Entry",modifier=True),
    ],"unresolved":[]}
    hd={"heroes":{"Hero":{"abilities":[{"id":"A","ability_id":"A","record_key":"Hero|A","name":"A"}],
                             "talents":[{"id":"TalentEnable","talent_id":"TalentEnable"},{"id":"TalentFormula","talent_id":"TalentFormula"}]}}}
    with tempfile.TemporaryDirectory() as td:
        td=Path(td)
        save(td/"cat.json",{"records":records}); save(td/"typed.json",typed); save(td/"hd.json",hd)
        report=run_combat(td/"cat.json",td/"typed.json",td/"hd.json",td/"combat.json",td/"report.json")
        x=load(td/"combat.json")["heroes"]["Hero"][0]

        # M1: Chance=0 + explicit CTalent field mutation is conditional, not unconditional.
        assert not x["damage"]
        assert [d["effect_id"] for d in x["conditional_damage"]]==["D"]
        d=x["conditional_damage"][0]
        gates=[c for c in d["conditions"] if c.get("relation")=="ENABLED_BY_TALENT_MODIFICATION"]
        assert len(gates)==1 and gates[0]["talent_ids"]==["TalentEnable"]
        assert any(m.get("target_id")=="TalentGate" and m.get("field")=="Chance" and m.get("value")==1 for m in x["talent_field_mutations"])

        # Fail closed: structurally reachable Chance=0 branch with no proven enabler is suppressed.
        assert all(d.get("effect_id")!="DormantDamage" for d in x["damage"]+x["conditional_damage"])
        assert any(q.get("object_id")=="DormantDamage" for q in x["dormant_combat"])

        # M2: formula validator constrains the arithmetic only; candidate value is 60.
        fm=d["formula_modifiers"]
        assert len(fm)==1 and fm[0]["operation"]=="MULTIPLY_BASE_DELTA"
        assert fm[0]["value"]==-0.4 and abs(fm[0]["candidate_value_if_active"]-60.0)<1e-9
        assert fm[0]["conditions"][0]["talent_ids"]==["TalentFormula"]
        assert all(c.get("target_id")!="VFormula" for c in d["conditions"])

        # Base/direct bridge uses the same Replay Schema formula.modifiers shape.
        direct_models=ability_damage_models({"short_text":"","full_text":""},{"damage":[d]})
        assert direct_models[0]["modifiers"][0]["candidate_value_if_active"]==60.0

        # Formula modifiers and talent field mutations survive the Replay Schema bridge.
        ability={"ability_id":"A","id":"A","record_key":"Hero|A","name":"A","combat":x,
                 "resolver_semantics":{"damage_models":ability_damage_models({"short_text":"","full_text":""},x),
                                       "heal_models":[],"shield_models":[],"has_damage":True,"has_heal":False,"has_shield":False,
                                       "status_effects":[],"protection":[],"interrupt_capable":[],"talent_combat_overlays":[],"talent_status_overlays":[]}}
        rv=normalize_ability("Hero",ability)
        cdm=[m for m in rv["mechanics"] if m["kind"]=="DAMAGE" and m.get("execution_relation")=="CONDITIONAL_TYPED_OWNERSHIP"]
        assert cdm and cdm[0]["formula"]["modifiers"][0]["candidate_value_if_active"]==60.0
        tfm=[m for m in rv["modifiers"] if m.get("kind")=="TALENT_FIELD_MUTATION"]
        assert tfm and tfm[0]["talent_id"]=="TalentEnable" and tfm[0]["field"]=="Chance"

        assert report["summary"]["dormant_combat_suppressed"]>=1
        assert report["summary"]["talent_field_mutations_materialized"]>=1
        assert report["summary"]["damage_formula_modifiers_materialized"]>=1
    return True


if __name__=="__main__":
    print("PASS" if run() else "FAIL")
