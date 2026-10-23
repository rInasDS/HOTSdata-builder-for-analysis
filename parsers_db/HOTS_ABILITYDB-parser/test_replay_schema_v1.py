from replay_schema_v1 import normalize_ability, attach_schema_v1, validate_schema_v1

def run():
    a={"ability_id":"TestAbility","name":"Test","resolver_semantics":{"has_damage":True,"damage_models":[{"model":"FLAT","value":100}],"has_heal":True,"heal_models":[],"has_shield":False,"shield_models":[],"status_effects":["STUN"],"protection":[],"interrupt_capable":["STUN"],"talent_combat_overlays":[],"talent_status_overlays":[]}}
    v=normalize_ability("Hero",a)
    assert any(x["kind"]=="DAMAGE" and x["formula"] for x in v["mechanics"])
    assert any(x["kind"]=="HEAL" and x["data"].get("unresolved_formula") for x in v["mechanics"])
    assert any(x["kind"]=="STATUS" for x in v["mechanics"])
    db={"metadata":{},"heroes":{"Hero":{"abilities":[a],"talents":[]}}}
    db=attach_schema_v1(db); r=validate_schema_v1(db); assert r["passed"],r
    return True
if __name__=='__main__': print('PASS' if run() else 'FAIL')
