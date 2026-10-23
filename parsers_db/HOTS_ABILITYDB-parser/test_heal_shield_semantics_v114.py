from combat_typed_engine import _behavior_heal_instances, _shield_instances
from heal_model import infer_heal_models
from shield_model import infer_shield_models
from combat_semantics_audit import _heal_text_role, _shield_text_role


def _prov():
    return {"file": "fixture.xml", "order": 1}


def run():
    # Text-derived conversion/triggered heals that matter to replay reconstruction.
    zeal = infer_heal_models(
        "Allies with Zeal are healed for 100% of the damage Whitemane deals to Heroes.",
        "FIXTURE",
    )
    assert len(zeal) == 1 and zeal[0]["amount_model"] == "DAMAGE_DEALT_PERCENT" and zeal[0]["value"] == 100.0
    assert zeal[0]["source_event"] == "DAMAGE_DEALT"

    absorbed = infer_heal_models(
        "When Force of Will expires, the target is healed for 20% of the damage it absorbed.",
        "FIXTURE",
    )
    assert len(absorbed) == 1 and absorbed[0]["amount_model"] == "DAMAGE_ABSORBED_PERCENT" and absorbed[0]["value"] == 20.0

    # Explicit flat heals without a trailing 'Health' token are valid.
    flat = infer_heal_models("Each attack deals 26 damage and heals the ally for 20.", "FIXTURE")
    assert [(x["amount_model"], x["value"]) for x in flat] == [("FLAT", 20.0)]

    # Charge counts / resource regeneration are not healing amounts.
    assert infer_heal_models("Activate to instantly gain 3 charges of Healing Dart.", "FIXTURE") == []
    morales = infer_heal_models(
        "Heal target allied Hero for 172 Health per second. After not channeling for 2 seconds, regenerate 6 Energy per second.",
        "FIXTURE",
    )
    assert [(x["amount_model"], x["value"]) for x in morales] == [("FLAT", 172.0)]

    # XML behavior leech: VitalDamageLeechArray is derived healing from damage dealt.
    leech = {
        "object_id": "BloodlustLeech",
        "xml_type": "CBehaviorBuff",
        "fields": {},
        "nested_facts": [
            {"path": ["Modification"], "tag": "VitalDamageLeechArray", "attrs": {"index": "Life", "value": "0.30"}, "provenance": _prov()},
            {"path": ["Modification", "VitalDamageLeechArray"], "tag": "KindArray", "attrs": {"index": "Basic", "value": "0.30"}, "provenance": _prov()},
        ],
    }
    rows = _behavior_heal_instances({"CBehaviorBuff::BloodlustLeech": leech}, "CBehaviorBuff::BloodlustLeech", leech, {}, {"CBehaviorBuff::BloodlustLeech": 0}, {})
    assert len(rows) == 1
    assert rows[0]["formula"]["model"] == "DAMAGE_DEALT_FRACTION"
    assert rows[0]["formula"]["fraction"] == 0.30
    assert rows[0]["formula"]["value_percent"] == 30.0
    assert rows[0]["source_damage_type"] == "BASIC_ATTACK"

    # XML shield capacity encoded as DamageResponse max-vital fraction.
    shield = {
        "object_id": "BoneArmorShield",
        "xml_type": "CBehaviorBuff",
        "fields": {},
        "nested_facts": [
            {"path": ["DamageResponse"], "tag": "ModifyLimitVitalMaxFractionArray", "attrs": {"index": "Life", "value": "0.25"}, "provenance": _prov()},
        ],
    }
    srows = _shield_instances({"CBehaviorBuff::BoneArmorShield": shield}, "CBehaviorBuff::BoneArmorShield", shield, {}, {"CBehaviorBuff::BoneArmorShield": 0}, {})
    assert len(srows) == 1
    assert srows[0]["kind"] == "DAMAGE_ABSORB_MAX_VITAL_FRACTION"
    assert srows[0]["value_percent"] == 25.0

    # Text shield fallback remains available for replay semantics.
    sms = infer_shield_models("Gain a Shield equal to 25% of Xul's maximum Health for 3 seconds.", "FIXTURE")
    assert sms and sms[0]["amount_model"] == "MAX_HEALTH_PERCENT" and sms[0]["value"] == 25.0

    # Audit role separation: control/modifier language must not be treated as direct heal/shield creation.
    assert _heal_text_role("Activate to instantly gain 3 charges of Healing Dart.")[0] == "HEAL_CONTROL_OR_REFERENCE"
    assert _heal_text_role("Enemies receive 50% less healing for 4 seconds.")[0] == "HEAL_MODIFIER"
    assert _shield_text_role("Reset the cooldown of Shield Ally and Personal Barrier.")[0] == "SHIELD_COOLDOWN_MODIFIER"
    assert _shield_text_role("Deal 200% bonus damage to enemy Shields.")[0] == "SHIELD_DAMAGE_OR_REMOVE"
    return True


if __name__ == "__main__":
    print("PASS" if run() else "FAIL")
