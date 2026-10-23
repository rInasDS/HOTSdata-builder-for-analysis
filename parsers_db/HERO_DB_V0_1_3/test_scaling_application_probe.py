from engine_scaling_probe import galaxy_semantic_anchors

def test_compat_import_and_fail_closed_without_scripts():
    r = galaxy_semantic_anchors(None, None)
    assert r['status'] == 'MISSING_BUILD_MATCHED_GALAXY_SNAPSHOT'
    assert r['application_semantics'] == 'UNRESOLVED_BUILD_MATCHED_GALAXY_APPLICATION_MISSING'
