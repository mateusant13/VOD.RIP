from pathlib import Path

from utils import classify_download_kind, download_kind_dir


class _Opts:
    def __init__(self, folder='D:/Videos', layout='typed'):
        self.download_folder = folder
        self.download_layout = layout


def test_flat_layout_stays_in_root():
    p = download_kind_dir(_Opts(layout='flat'), 'vods')
    assert p == Path('D:/Videos')


def test_typed_layout_uses_subfolder():
    p = download_kind_dir(_Opts(layout='typed'), 'twitch_clips')
    assert p == Path('D:/Videos') / 'Twitch clips'


def test_classify_twitch_clip_url():
    assert classify_download_kind(
        'https://clips.twitch.tv/PrettyClip', 'clip', None, None, 30,
    ) == 'twitch_clips'


def test_classify_trim_as_cut():
    assert classify_download_kind(
        'https://www.twitch.tv/videos/1', 'video', 10, 40, 3600,
    ) == 'cuts'


def test_classify_full_vod():
    assert classify_download_kind(
        'https://www.twitch.tv/videos/1', 'video', 0, 3600, 3600,
    ) == 'vods'
