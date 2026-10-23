from reverse_provenance_resolver import resolve_reverse_provenance

def D(oid,typ,children=None):
    return {'object_id':oid,'xml_type':typ,'descendants':children or []}
def N(tag,value):
    return {'tag':tag,'attrs':{'value':value},'path':[tag],'depth':1}

def run():
    # Local owner must beat a longer unrelated path (Hearthstone-style contamination).
    objects={
      'A':[D('A','CAbilEffectInstant',[N('Effect','ASet')])],
      'ASet':[D('ASet','CEffectSet',[N('EffectArray','Dmg')])],
      'Dmg':[D('Dmg','CEffectDamage')],
      'Foreign':[D('Foreign','CAbilEffectInstant',[N('Effect','Bridge')])],
      'Bridge':[D('Bridge','CEffectSet',[N('EffectArray','ASet')])],
    }
    a={'id':'A','record_key':'A'}; f={'id':'Foreign','record_key':'Foreign'}
    ra=resolve_reverse_provenance(objects,a); rf=resolve_reverse_provenance(objects,f)
    assert [x['object_id'] for x in ra['terminals']]==['Dmg'],ra
    assert rf['terminals']==[],rf

    # Weapon ownership pattern.
    objects={
      'Xul':[D('Xul','CAbilEffectInstant',[N('WeaponEnableArray','W')])],
      'W':[D('W','CWeapon',[N('ImpactEffect','Impact')])],
      'Impact':[D('Impact','CEffectSet',[N('EffectArray','XDmg')])],
      'XDmg':[D('XDmg','CEffectDamage')],
    }
    r=resolve_reverse_provenance(objects,{'id':'Xul','record_key':'Xul'})
    assert len(r['terminals'])==1 and r['terminals'][0]['execution_class']=='WEAPON_MODE',r
    assert r['terminals'][0]['chain']==['Xul','W','Impact','XDmg'],r
    return True
if __name__=='__main__': print('PASS' if run() else 'FAIL')
