from hero_unit_db_builder import (
    _vision_center_for_record,
    _scope_to_vision_center,
    _effect_target_scope,
)

def node(tag, **attrs):
    return {'tag': tag, 'attrs': attrs, 'provenance': {}}

def rec(typ, oid, fields=None):
    return {'xml_type': typ, 'object_id': oid, 'fields': fields or {}, 'nested_facts': []}

def test_explicit_location_selectors():
    r=rec('CEffectCreatePersistent','S',{'WhichLocation':[node('WhichLocation',Value='SourceUnit')]})
    assert _vision_center_for_record(r)[0]=='SOURCE_POINT'
    r=rec('CEffectCreatePersistent','T',{'WhichLocation':[node('WhichLocation',Value='TargetUnitOrPoint')]})
    assert _vision_center_for_record(r)[0]=='TARGET_POINT'

def test_ability_type_location_proof():
    assert _vision_center_for_record(rec('CAbilEffectTarget','A'))[0]=='TARGET_POINT'
    assert _vision_center_for_record(rec('CAbilEffectInstant','B'))[0]=='SOURCE_POINT'
    assert _vision_center_for_record(rec('CBehaviorBuff','C'))[0]=='UNRESOLVED'

def test_recipient_scope_centers():
    assert _scope_to_vision_center('HERO_SELF')=='SELF'
    assert _scope_to_vision_center('HERO_FORM')=='SELF'
    assert _scope_to_vision_center('OWNED_ENTITY')=='OWNED_ENTITY'
    assert _scope_to_vision_center('OWNED_ENTITY_RUNTIME_FORM')=='OWNED_ENTITY'
    assert _scope_to_vision_center('TARGET_POINT')=='TARGET_POINT'
    assert _scope_to_vision_center('VISION_OBJECT_RECIPIENT_UNRESOLVED') is None

def test_effect_target_scope():
    assert _effect_target_scope('TARGET_POINT','AREA_REVEAL_RADIUS')=='TARGET_POINT'
    assert _effect_target_scope('SOURCE_POINT','AREA_REVEAL_RADIUS')=='AREA'
    assert _effect_target_scope('AREA_EFFECT','SEARCH_REVEAL')=='AREA'
    assert _effect_target_scope('UNRESOLVED','SIGHT_BONUS')=='EFFECT_RUNTIME_SCOPE_UNRESOLVED'

def main():
    test_explicit_location_selectors()
    test_ability_type_location_proof()
    test_recipient_scope_centers()
    test_effect_target_scope()
    print('test_hero_unit_db_v011: PASS')

if __name__=='__main__':
    main()
