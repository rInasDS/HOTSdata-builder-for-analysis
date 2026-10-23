from scaling_runtime_promoter import _runtime_application


def test_explicit_percent_runtime_application():
    r = _runtime_application({"field":"LifeMax", "value":"0.040000", "affected_by_overdrive":"1", "field_is_integer":None}, "2.55.17.97771")
    assert r["status"] == "PROVEN_BUILD_MATCHED"
    assert r["operation"] == "CURRENT_PLAYER_CATALOG_PERCENT_ADD"
    assert r["formula"] == "NEXT = CURRENT + CURRENT*VALUE"


def test_missing_value_stays_partial():
    r = _runtime_application({"field":"Amount", "value":None, "affected_by_overdrive":"1", "field_is_integer":None}, "2.55.17.97771")
    assert r["status"] == "PROVEN_OPERATION_VALUE_DEFAULT_UNRESOLVED"


def test_missing_percent_flag_stays_fail_closed():
    r = _runtime_application({"field":"Duration", "value":"0.500000", "affected_by_overdrive":None, "field_is_integer":None}, "2.55.17.97771")
    assert r["status"] == "UNRESOLVED_AFFECTED_BY_OVERDRIVE_DEFAULT"
    assert r["operation"] == "UNRESOLVED_PERCENT_OR_FLAT_ADD"
