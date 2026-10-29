"""python3 -m unittest discover -s bridge/tests  (보드·시리얼 포트 없이 실행)"""
import base64
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import cat_usb_bridge as cb  # noqa: E402
import make_catevt  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "tone-16k-60ms.opus")


def fixture_packets():
    with open(FIXTURE, "rb") as handle:
        return cb.read_ogg_packets(handle.read())[2:]


def feed(bridge, lines):
    for line in lines:
        bridge.feed_line(line)


class FramingTest(unittest.TestCase):
    def setUp(self):
        self.stats = cb.Stats()
        self.assembler = cb.FrameAssembler(self.stats)

    def assemble(self, lines, now=0.0):
        result = None
        for line in lines:
            result = self.assembler.feed_line(line, now) or result
        return result

    def test_round_trip_with_log_prefix_and_long_korean_text(self):
        event = {"id": "e1", "type": "message", "role": "user", "text": "꽃" * 2000}
        lines = cb.encode_event_lines(event, "ab12cd34-000001")
        self.assertGreater(len(lines), 64)                      # 64조각으로는 모자란 크기
        self.assertLessEqual(len(lines), cb.MAX_PARTS)
        prefixed = [b"[10-25 05:11:11 ty W][cat_network.c:9] " + line + b"\r" for line in lines]
        self.assertEqual(self.assemble(prefixed), event)
        self.assertNotIn(b"%", b"".join(lines))                 # bk_printf 형식 문자 위험 없음

    def test_ordinary_log_lines_are_ignored_and_not_counted_as_frames(self):
        self.assertIsNone(self.assembler.feed_line(b"[ty W][portal.c:1] Wi-Fi ssid=secret", 0.0))
        self.assertEqual(self.stats.snapshot(), {})

    def test_interleaved_events_and_duplicates(self):
        first = cb.encode_event_lines({"id": "a", "type": "state", "state": "idle", "pad": "x" * 200}, "f-1")
        second = cb.encode_event_lines({"id": "b", "type": "state", "state": "listening", "pad": "y" * 200}, "f-2")
        results = []
        for a, b in zip(first, second):
            results += [self.assembler.feed_line(a, 0.0), self.assembler.feed_line(b, 0.0), self.assembler.feed_line(a, 0.0)]
        self.assertEqual([item["id"] for item in results if item], ["a", "b"])
        self.assertIsNone(self.assemble(first))                 # 완료된 id는 다시 받지 않음

    def test_incomplete_event_times_out(self):
        lines = cb.encode_event_lines({"id": "a", "type": "state", "state": "idle", "pad": "x" * 200}, "f-3")
        self.assertIsNone(self.assemble(lines[:-1], now=0.0))
        self.assertIsNone(self.assembler.feed_line(lines[-1], now=cb.ASSEMBLY_TIMEOUT_S + 1))
        self.assertEqual(self.stats.snapshot().get("events_timeout"), 1)

    def test_malformed_frames_are_rejected(self):
        good = json.loads(cb.encode_event_lines({"id": "a", "type": "state", "state": "idle"}, "f-4")[0][len(cb.MARKER):])
        bad = [dict(good, v=2), dict(good, v=True), dict(good, id="bad id"), dict(good, part=1),
               dict(good, total=cb.MAX_PARTS + 1), dict(good, data="***"), dict(good, data="QQ"),
               dict(good, total=2, part=0)]                     # 마지막이 아닌 조각은 정확히 72바이트
        for frame in bad:
            self.assertIsNone(self.assembler.feed_line(cb.MARKER + json.dumps(frame).encode(), 0.0))
        self.assertIsNone(self.assembler.feed_line(cb.MARKER + "{\"v\":1,\"id\":\"한\"}".encode("utf-8"), 0.0))
        self.assertEqual(self.stats.snapshot()["frames_invalid"], len(bad) + 1)


class EventTest(unittest.TestCase):
    def test_normalize_keeps_known_fields_only(self):
        cleaned = cb.normalize_device_event({"id": "m1", "type": "message", "role": "assistant",
                                             "text": "  안녕  ", "turn_id": "t1", "debug": "x"})
        self.assertEqual(cleaned, {"id": "m1", "type": "message", "role": "assistant", "text": "안녕", "turn_id": "t1"})

    def test_invalid_events(self):
        for event in [{"id": "x", "type": "action", "action": "release"},
                      {"id": "x", "type": "state", "state": "completed"},
                      {"id": "x", "type": "message", "role": "system", "text": "a"},
                      {"id": "x", "type": "message", "role": "user", "text": "a" * 2001},
                      {"id": "x y", "type": "state", "state": "idle"},
                      {"id": "x", "type": "state", "state": "idle", "turn_id": "../t"},
                      {"id": "x", "type": "audio_start", "stream_id": "s", "role": "user", "turn_id": "t",
                       "sample_rate": 44100, "channels": 1, "frame_ms": 60},
                      {"id": "x", "type": "audio_chunk", "stream_id": "s", "seq": 0, "opus": ""},
                      {"id": "x", "type": "audio_end", "stream_id": "s", "frames": -1}]:
            with self.assertRaises(ValueError):
                cb.normalize_device_event(event)

    def test_opus_toc_durations(self):
        self.assertEqual(cb.opus_packet_samples_48k(bytes([(3 << 3) | 0])), 2880)      # SILK 60ms
        self.assertEqual(cb.opus_packet_samples_48k(bytes([(19 << 3) | 0])), 960)      # CELT 20ms
        self.assertEqual(cb.opus_packet_samples_48k(bytes([(19 << 3) | 3, 3])), 2880)  # code 3, 3 frames
        with self.assertRaises(ValueError):
            cb.opus_packet_samples_48k(bytes([(3 << 3) | 3, 3]))                         # 180ms > 120ms


class OggTest(unittest.TestCase):
    def test_ogg_round_trip_and_duration(self):
        packets = fixture_packets()
        ogg, duration_ms = cb.build_ogg_opus(packets, 16000)
        self.assertEqual(cb.read_ogg_packets(ogg)[2:], packets)
        self.assertTrue(1150 <= duration_ms <= 1250, duration_ms)

    @unittest.skipUnless(shutil.which("opusdec"), "opusdec 없음")
    def test_ogg_decodes_with_reference_decoder(self):
        ogg, _ = cb.build_ogg_opus(fixture_packets(), 16000)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.opus")
            with open(path, "wb") as handle:
                handle.write(ogg)
            result = subprocess.run(["opusdec", "--quiet", path, os.path.join(folder, "out.wav")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreater(os.path.getsize(os.path.join(folder, "out.wav")), 16000)


def demo_lines():
    out = io.BytesIO()
    make_catevt.demo(make_catevt.Writer(out, boot="d3m0d3m0"), FIXTURE)
    return out.getvalue().splitlines()


class BridgeTest(unittest.TestCase):
    def published(self, bridge):
        client, backlog = bridge.hub.subscribe()
        bridge.hub.unsubscribe(client)
        return [json.loads(text) for _, text in backlog]

    def test_demo_turn_publishes_text_state_and_audio(self):
        bridge = cb.Bridge()
        feed(bridge, demo_lines())
        events = self.published(bridge)
        self.assertEqual([event["type"] for event in events],
                         ["state", "message", "audio", "state", "audio", "message", "state", "state"])
        audio = [event for event in events if event["type"] == "audio"]
        self.assertEqual([item["role"] for item in audio], ["user", "assistant"])
        for item in audio:
            self.assertRegex(item["url"], r"^/audio/[0-9a-f]{24}\.ogg$")
            self.assertEqual(item["turn_id"], "d3m0d3m0-t1")
            self.assertTrue(1150 <= item["duration_ms"] <= 1250)
            self.assertEqual(cb.read_ogg_packets(bridge.store.get(item["url"][7:]))[2:], fixture_packets())

    def audio_lines(self, frames, drop=()):
        writer_out = io.BytesIO()
        writer = make_catevt.Writer(writer_out, boot="aaaa0000")
        packets = fixture_packets()
        writer.event({"type": "audio_start", "stream_id": "s1", "turn_id": "t1", "role": "assistant",
                      "sample_rate": 24000, "channels": 1, "frame_ms": 60})
        for seq, packet in enumerate(packets[:frames]):
            if seq not in drop:
                writer.event({"type": "audio_chunk", "stream_id": "s1", "seq": seq,
                              "opus": base64.b64encode(packet).decode()})
        writer.event({"type": "audio_end", "stream_id": "s1", "frames": frames})
        return writer_out.getvalue().splitlines()

    def test_missing_packets_policy(self):
        bridge = cb.Bridge(max_audio_loss=0.05)
        feed(bridge, self.audio_lines(20, drop={3, 4}))             # 10% 누락 → 버림
        self.assertEqual(self.published(bridge), [])
        self.assertEqual(bridge.stats.snapshot()["audio_dropped_missing"], 1)
        bridge = cb.Bridge(max_audio_loss=0.05)
        feed(bridge, self.audio_lines(20, drop={3}))                # 5% 누락 → 허용
        self.assertEqual([event["type"] for event in self.published(bridge)], ["audio"])

    def test_zero_frame_turn_has_no_audio_and_no_audio_mode(self):
        bridge = cb.Bridge()
        feed(bridge, self.audio_lines(0))
        self.assertEqual(self.published(bridge), [])
        self.assertEqual(bridge.stats.snapshot()["audio_empty"], 1)
        bridge = cb.Bridge(audio_enabled=False)
        feed(bridge, demo_lines())
        self.assertNotIn("audio", [event["type"] for event in self.published(bridge)])

    def test_audio_store_is_bounded(self):
        store = cb.AudioStore(max_files=2, max_bytes=10, ttl_s=60)
        first = store.put(b"12345", now=0)
        store.put(b"12345", now=1)
        store.put(b"1", now=2)
        self.assertIsNone(store.get(first, now=3))
        self.assertIsNone(cb.AudioStore(ttl_s=5).get(cb.AudioStore(ttl_s=5).put(b"x", now=0), now=10))


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = cb.Bridge()
        feed(cls.bridge, demo_lines())
        cls.server, cls.port = cb.start_http(cls.bridge, "127.0.0.1", 0,
                                             ["http://localhost:4317", "http://127.0.0.1:4317"])

    @classmethod
    def tearDownClass(cls):
        cls.server.closing = True
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return connection, response

    def read_events(self, response, count):
        events = []
        while len(events) < count:
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        return events

    def test_sse_cors_and_resume(self):
        connection, response = self.request("/events", {"Origin": "http://localhost:4317"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "http://localhost:4317")
        events = self.read_events(response, 8)
        connection.close()
        self.assertEqual(events[0]["type"], "state")
        connection, response = self.request("/events", {"Last-Event-ID": events[5]["id"]})
        self.assertEqual([item["id"] for item in self.read_events(response, 2)], [events[6]["id"], events[7]["id"]])
        connection.close()

    def test_rejects_other_origins_and_hosts(self):
        connection, response = self.request("/events", {"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        connection.close()
        connection, response = self.request("/health", {"Host": "evil.example:%d" % self.port})
        self.assertEqual(response.status, 421)
        connection.close()

    def test_health_has_counters_only(self):
        connection, response = self.request("/health")
        body = json.loads(response.read())
        connection.close()
        self.assertTrue(body["ok"])
        self.assertNotIn("꽃", json.dumps(body, ensure_ascii=False))

    def test_audio_full_and_range(self):
        _, backlog = self.bridge.hub.subscribe()
        url = next(json.loads(text)["url"] for _, text in backlog if json.loads(text)["type"] == "audio")
        connection, response = self.request(url)
        data = response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "audio/ogg")
        connection, response = self.request(url, {"Range": "bytes=4-15"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.read(), data[4:16])
        connection.close()
        connection, response = self.request("/audio/%s.ogg" % ("0" * 24))
        self.assertEqual(response.status, 404)
        connection.close()


def port(device, location="", serial="5AAE166934", vid=0x1A86, pid=0x55D2, interface=None):
    return SimpleNamespace(device=device, location=location, serial_number=serial, vid=vid, pid=pid,
                           interface=interface, hwid="", description="USB Dual_Serial")


class PortPickTest(unittest.TestCase):
    def test_log_interface_is_found_from_metadata_on_each_os(self):
        mac = [port("/dev/cu.usbmodem5AAE1669341", "0-1"), port("/dev/cu.usbmodem5AAE1669343", "0-1")]
        win = [port("COM7", "1-2:x.0"), port("COM8", "1-2:x.2")]
        linux = [port("/dev/ttyACM0", "1-1:1.0"), port("/dev/ttyACM1", "1-1:1.2")]
        self.assertEqual(cb.pick_board_port(mac)[0], "/dev/cu.usbmodem5AAE1669343")
        self.assertEqual(cb.pick_board_port(win)[0], "COM8")
        self.assertEqual(cb.pick_board_port(linux)[0], "/dev/ttyACM1")

    def test_ambiguous_cases_return_guidance_instead_of_guessing(self):
        self.assertIsNone(cb.pick_board_port([])[0])
        self.assertIsNone(cb.pick_board_port([port("COM3", vid=0x10C4, pid=0xEA60)])[0])
        unknown = [port("COM7"), port("COM8")]                       # 인터페이스 정보 없음
        self.assertIsNone(cb.pick_board_port(unknown)[0])
        self.assertIn("COM7", cb.pick_board_port(unknown)[1])
        two_boards = [port("COM8", "1-2:x.2"), port("COM10", "1-3:x.2", serial="OTHER")]
        self.assertIsNone(cb.pick_board_port(two_boards)[0])


class LineSplitterTest(unittest.TestCase):
    def test_partial_and_overlong_lines(self):
        stats = cb.Stats()
        splitter = cb.LineSplitter(stats)
        self.assertEqual(splitter.push(b"ab"), [])
        self.assertEqual(splitter.push(b"c\r\nde\n"), [b"abc", b"de"])
        self.assertEqual(splitter.push(b"x" * (cb.MAX_LINE_BYTES + 1)), [])
        self.assertEqual(splitter.push(b"tail\nok\n"), [b"ok"])
        self.assertEqual(stats.snapshot()["lines_too_long"], 1)


if __name__ == "__main__":
    unittest.main()
