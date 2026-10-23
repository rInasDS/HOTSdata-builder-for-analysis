from replay_overflow_probe import signed_i32, unsigned_i32, fixed_signed, fixed_unsigned_reconstructed


def test_known_overflow_values():
    assert signed_i32(4294524391) == -442905
    assert unsigned_i32(-2119327744) == 2175639552
    assert fixed_signed(2130092032) == 520042.0
    assert fixed_signed(-2119327744) == -517414.0
    assert fixed_unsigned_reconstructed(-2119327744) == 531162.0


if __name__ == '__main__':
    test_known_overflow_values()
    print('test_replay_overflow_probe: PASS')
