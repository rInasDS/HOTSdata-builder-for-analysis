from pathlib import Path
from runtime_scaling_probe import decode_message_events
from replay_overflow_probe import MPQArchive
from game_events_91093 import decode as decode_game_events


def test_build_91093_message_surface(replay_path: Path):
    rows = decode_message_events(MPQArchive(replay_path).extract('replay.message.events'))
    assert len(rows) == 1038
    anns = [x for x in rows if x['event_name'] == 'SPlayerAnnounceMessage']
    assert len(anns) == 11
    vit = [x for x in anns if 'Vitals' in x['payload']['m_announcement']]
    assert len(vit) == 3
    assert {x['payload']['m_announcement']['Vitals'] for x in vit} == {2}


def test_build_91093_game_surface(replay_path: Path):
    rows = decode_game_events(MPQArchive(replay_path).extract('replay.game.events'))
    assert len(rows) == 771396
    assert sum(1 for x in rows if x['event_name'] == 'SCatalogModifyEvent') == 0
    assert sum(1 for x in rows if x['event_name'] == 'SHeroTalentTreeSelectedEvent') == 70


if __name__ == '__main__':
    rp = Path('/mnt/data/2023-11-12_21.11.07_Towers_of_Doom(1).StormReplay')
    test_build_91093_message_surface(rp)
    test_build_91093_game_surface(rp)
    print('test_runtime_scaling_probe: PASS')
