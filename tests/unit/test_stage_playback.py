"""Run the normal playback loop with fake devices, without importing audio dependencies.

AST extraction uses the production methods verbatim. These regression tests cover
ordering and exactly-once publication, not physical audio hardware or remote apps.
"""
import ast
import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class StopPlayback(BaseException):
    pass


class PlaybackTimingTests(unittest.TestCase):
    def exercise_player(self, player):
        events = []
        tree = ast.parse((ROOT / 'utils/audio.py').read_text())
        audio_class = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == 'Audio')
        names = {'only_play_audio', '_publish_playback_caption'}
        methods = [item for item in audio_class.body if getattr(item, 'name', '') in names]
        namespace = {'asyncio': asyncio, 'threading': threading}

        class FakeSignals:
            metadata = {'id': 'clip', 'callback_url': 'http://127.0.0.1:5500/playback', 'protocol': 1}
            external_callback = None

            def __init__(self, speech):
                pass

            def send(self, phase):
                events.append(phase)

            def start(self):
                events.append('speaking-start')

            def end(self):
                events.append('speaking-end')

            def watch_external(self, on_start):
                FakeSignals.external_callback = on_start

        class Condition:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def wait(self):
                raise StopPlayback()

        class Music:
            busy = False

            def load(self, path):
                events.append('load')

            def play(self):
                events.append('actual-start')
                self.busy = True

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False
                events.append('audio-end')

        music = Music()

        class ExternalPlayer:
            def play(self, payload):
                events.append('enqueue')
                self_payload = payload['stage_playback']
                assert self_payload['protocol'] == 1
                assert 'caption' not in events
                events.append('actual-start')
                # External callbacks arrive on a different process/thread.
                thread = threading.Thread(target=FakeSignals.external_callback)
                thread.start()
                thread.join()
                events.append('audio-end')
                return True

        class Config:
            def get(self, *keys):
                values = {
                    ('play_audio', 'player'): player,
                    ('play_audio', 'interval_num_min'): 1,
                    ('play_audio', 'interval_num_max'): 1,
                    ('captions',): {'enable': True, 'file_path': 'unused'},
                    ('web_captions_printer', 'enable'): True,
                    ('choose_song',): {},
                }
                return values.get(keys, False)

        async def caption(*args, **kwargs):
            events.append('caption')

        async def sleep(*args):
            events.append('pre-play-delay')

        def tick(*args):
            music.busy = False

        namespace.update({
            'PlaybackSignals': FakeSignals,
            'logger': SimpleNamespace(debug=lambda *a: None, error=lambda *a: events.append('error')),
            'traceback': SimpleNamespace(format_exc=lambda: ''),
            'time': SimpleNamespace(monotonic=lambda: 0),
        })
        exec(compile(ast.Module(body=methods, type_ignores=[]), 'audio-production-methods', 'exec'), namespace)
        Audio = type('Audio', (), {name: namespace[name] for name in names})
        namespace['Audio'] = Audio
        Audio.copywriting_play_flag = -1
        Audio.voice_tmp_path_queue_lock = Condition()
        Audio.voice_tmp_path_queue_not_empty = Condition()
        Audio.voice_tmp_path_queue = [{'type': 'comment', 'voice_path': 'fake.wav', 'content': 'reply'}]
        Audio.message_queue = []
        Audio.mixer_normal = SimpleNamespace(music=music, init=lambda: None)
        Audio.audio_player = ExternalPlayer()
        instance = Audio()
        instance.config = Config()
        instance.common = SimpleNamespace(
            write_content_to_file=lambda *a, **k: events.append('file-caption'),
            send_to_web_captions_printer=caption,
            get_random_value=lambda a, b: 1,
        )
        instance._song_overlap_wanted = lambda data: False
        async def callback():
            pass
        instance.send_audio_play_info_to_callback = callback
        fake_pygame = SimpleNamespace(time=SimpleNamespace(get_ticks=lambda: 0, Clock=lambda: SimpleNamespace(tick=tick)))
        with patch.dict(sys.modules, {'pygame': fake_pygame}), patch('asyncio.sleep', sleep):
            with self.assertRaises(StopPlayback):
                asyncio.run(instance.only_play_audio())
        self.assertNotIn('error', events)
        self.assertEqual(events.count('caption'), 1)
        self.assertEqual(events.count('file-caption'), 1)
        self.assertLess(events.index('pre-play-delay'), events.index('actual-start'))
        self.assertLess(events.index('actual-start'), events.index('caption'))
        if player == 'pygame':
            self.assertLess(events.index('actual-start'), events.index('speaking-start'))
            self.assertLess(events.index('audio-end'), events.index('speaking-end'))

    def test_pygame(self):
        self.exercise_player('pygame')

    def test_audio_player(self):
        self.exercise_player('audio_player')

    def test_audio_player_v2(self):
        self.exercise_player('audio_player_v2')


if __name__ == '__main__':
    unittest.main(verbosity=2)
