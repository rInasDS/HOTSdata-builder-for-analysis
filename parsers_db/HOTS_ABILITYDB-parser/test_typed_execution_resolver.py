from typed_execution_resolver import resolve_execution

def D(oid,typ,desc=None): return {"object_id":oid,"xml_type":typ,"descendants":desc or [],"attrs":{},"file":"fixture"}
def N(tag,**attrs): return {"tag":tag,"attrs":attrs,"path":[tag],"depth":1}
def run():
    a={"id":"A"}
    # Direct effect.
    o={"A":[D("A","CAbilEffectInstant",[N("Effect",value="D")])],"D":[D("D","CEffectDamage")]}
    r=resolve_execution(o,a); assert r["classes"]==["DIRECT_EFFECT"] and len(r["terminals"])==1
    # Xul/Fenix structural pattern: behavior enables weapon, weapon impact owns damage.
    o={"A":[D("A","CAbilEffectInstant",[N("Behavior",value="B")])],"B":[D("B","CBehaviorBuff",[N("WeaponEnableArray",value="W")])],"W":[D("W","CWeapon",[N("ImpactEffect",value="D")])],"D":[D("D","CEffectDamage")]}
    r=resolve_execution(o,a); assert r["classes"]==["WEAPON_MODE"] and r["terminals"][0]["chain"]==["A","B","W","D"]
    # Zagara structural separation: cast damage and spawned unit are distinct terminals.
    o={"A":[D("A","CAbilEffectInstant",[N("EffectArray",value="D"),N("EffectArray",value="U")])],"D":[D("D","CEffectDamage")],"U":[D("U","CEffectCreateUnit")]}
    r=resolve_execution(o,a); assert {x["execution_class"] for x in r["terminals"]}=={"DIRECT_EFFECT","SUMMON"}
    # Negative trigger text / unrelated object: no explicit edge => no damage.
    o={"A":[D("A","CAbilEffectInstant")],"ForeignDamage":[D("ForeignDamage","CEffectDamage")]}
    r=resolve_execution(o,a); assert not r["terminals"] and r["classes"]==["UNRESOLVED_EXECUTION_PATTERN"]
    # Foreign namespace is legal when XML explicitly references it: provenance, not names, decides.
    o={"A":[D("A","CAbilEffectInstant",[N("Effect",value="TotallyForeign")])],"TotallyForeign":[D("TotallyForeign","CEffectDamage")]}
    r=resolve_execution(o,a); assert r["classes"]==["DIRECT_EFFECT"]
    # Duplicate XML definitions must not duplicate semantic terminal.
    o={"A":[D("A","CAbilEffectInstant",[N("Effect",value="D")])],"D":[D("D","CEffectDamage"),D("D","CEffectDamage")]}
    r=resolve_execution(o,a); assert len(r["terminals"])==1
    return True
if __name__=='__main__': print('PASS' if run() else 'FAIL')
