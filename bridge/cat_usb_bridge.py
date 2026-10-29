#!/usr/bin/env python3
"""고양이 보드(Tuya T5AI) USB 로그 → CoFlorist 외부 대화(SSE) 중계기.

보드는 로그 UART(460800)에 `CAT_EVT {...}` 줄을 섞어 보낸다. 이 프로그램은 그 줄만
골라 조각을 다시 합치고, 대화 상태·문장은 SSE로 그대로 내보내며, Opus 음성 패킷은
Ogg Opus 파일로 묶어 이 컴퓨터 안(loopback)에서만 재생 주소를 알려 준다.

- 그 밖의 로그 줄은 읽자마자 버린다. 화면·파일 어디에도 남기지 않는다.
- 음성 파일은 메모리에만 두고 개수·용량 상한을 넘으면 오래된 것부터 지운다.
- 기본 바인딩은 127.0.0.1, 브라우저 출처는 localhost:4317 / 127.0.0.1:4317만 허용한다.

필요한 것: Python 3.8+ 와 pyserial (시리얼 포트를 읽을 때만).
사용법은 bridge/README.md 참고.
"""
import argparse
import base64
import binascii
import json
import os
import queue
import re
import secrets
import struct
import sys
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1"

# ---------------------------------------------------------------- 프레이밍 규칙
MARKER = b"CAT_EVT "
MAX_LINE_BYTES = 2048          # 이보다 긴 줄은 읽지 않고 버린다
CHUNK_BYTES = 72               # 원본 JSON(UTF-8)을 72바이트씩 잘라 base64
MAX_PARTS = 128                # 이벤트 하나 최대 128조각(9,216B, 펌웨어 상한 8,192B)
ASSEMBLY_TIMEOUT_S = 2.0       # 조각이 다 오기까지 기다리는 시간
MAX_ASSEMBLIES = 32            # 동시에 조립 중인 이벤트 수
COMPLETED_MEMORY = 1024        # 이미 받은 조각 묶음 id를 기억하는 개수

FRAME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
STATES = ("idle", "listening", "thinking", "speaking", "error")
ROLES = ("user", "assistant")

# ---------------------------------------------------------------- 음성 상한
MAX_OPUS_PACKET = 1275
MAX_STREAM_PACKETS = 2000      # 60ms 기준 120초
MAX_STREAM_BYTES = 600_000
MAX_ACTIVE_STREAMS = 8
STREAM_IDLE_TIMEOUT_S = 30.0
SAMPLE_RATES = (8000, 12000, 16000, 24000, 48000)
FRAME_MS = (10, 20, 40, 60)
OPUS_PRE_SKIP = 312            # libopus 기본 lookahead(48kHz 기준)

MAX_AUDIO_FILES = 24
MAX_AUDIO_TOTAL_BYTES = 16 * 1024 * 1024
AUDIO_TTL_S = 3600.0
SSE_HISTORY = 500
SSE_CLIENT_QUEUE = 256
SSE_KEEPALIVE_S = 15.0


def log(message):
    """중계기 자신의 상태만 출력한다. 보드 로그 내용은 절대 넘기지 않는다."""
    print("[cat-bridge] %s" % message, file=sys.stderr, flush=True)


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.values = {}

    def add(self, name, amount=1):
        with self._lock:
            self.values[name] = self.values.get(name, 0) + amount

    def snapshot(self):
        with self._lock:
            return dict(self.values)


# ================================================================ 조각 조립
class FrameAssembler:
    """`CAT_EVT {"v":1,"id","part","total","data"}` 줄을 모아 원본 JSON 이벤트로 복원."""

    def __init__(self, stats, timeout_s=ASSEMBLY_TIMEOUT_S):
        self.stats = stats
        self.timeout_s = timeout_s
        self._pending = OrderedDict()      # frame id -> {"total", "parts", "created"}
        self._completed = OrderedDict()    # frame id -> None (최근 완료 기억)

    def feed_line(self, raw, now=None):
        """줄 하나를 넣는다. 이벤트가 완성되면 dict, 아니면 None."""
        now = time.monotonic() if now is None else now
        self.expire(now)
        index = raw.find(MARKER)
        if index < 0:
            return None                    # 일반 로그: 보관·출력하지 않음
        self.stats.add("frames_seen")
        try:
            frame = self._parse_frame(raw[index + len(MARKER):])
        except ValueError:
            self.stats.add("frames_invalid")
            return None
        frame_id, part, total, chunk = frame
        if frame_id in self._completed:
            self.stats.add("frames_duplicate")
            return None
        entry = self._pending.get(frame_id)
        if entry is None:
            if len(self._pending) >= MAX_ASSEMBLIES:
                self._pending.popitem(last=False)
                self.stats.add("events_evicted")
            entry = {"total": total, "parts": {}, "created": now}
            self._pending[frame_id] = entry
        if entry["total"] != total:
            del self._pending[frame_id]
            self.stats.add("events_invalid")
            return None
        previous = entry["parts"].get(part)
        if previous is not None:
            if previous != chunk:
                del self._pending[frame_id]
                self.stats.add("events_invalid")
            else:
                self.stats.add("frames_duplicate")
            return None
        entry["parts"][part] = chunk
        if len(entry["parts"]) < total:
            return None
        del self._pending[frame_id]
        self._remember(frame_id)
        payload = b"".join(entry["parts"][i] for i in range(total))
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.stats.add("events_invalid")
            return None
        if not isinstance(event, dict):
            self.stats.add("events_invalid")
            return None
        self.stats.add("events_assembled")
        return event

    def expire(self, now):
        while self._pending:
            frame_id, entry = next(iter(self._pending.items()))
            if now - entry["created"] <= self.timeout_s:
                break
            del self._pending[frame_id]
            self.stats.add("events_timeout")

    def _remember(self, frame_id):
        self._completed[frame_id] = None
        while len(self._completed) > COMPLETED_MEMORY:
            self._completed.popitem(last=False)

    @staticmethod
    def _parse_frame(body):
        text = body.strip().decode("ascii")         # 원본 문장은 base64 안에만 있다
        frame = json.loads(text)
        if not isinstance(frame, dict):
            raise ValueError("frame")
        version, frame_id = frame.get("v"), frame.get("id")
        part, total, data = frame.get("part"), frame.get("total"), frame.get("data")
        if type(version) is not int or version != 1:
            raise ValueError("version")
        if not isinstance(frame_id, str) or not FRAME_ID_RE.match(frame_id):
            raise ValueError("id")
        if type(part) is not int or type(total) is not int or not (1 <= total <= MAX_PARTS) or not (0 <= part < total):
            raise ValueError("part")
        if not isinstance(data, str) or not data or len(data) > 4 * CHUNK_BYTES // 3:
            raise ValueError("data")
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("base64")
        if part < total - 1 and len(chunk) != CHUNK_BYTES:
            raise ValueError("chunk size")
        if not (1 <= len(chunk) <= CHUNK_BYTES):
            raise ValueError("chunk size")
        return frame_id, part, total, chunk


def encode_event_lines(event, frame_id):
    """참고 구현(펌웨어와 같은 규칙): 이벤트 dict를 CAT_EVT 줄 목록으로 만든다."""
    if not FRAME_ID_RE.match(frame_id):
        raise ValueError("frame id")
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    chunks = [payload[i:i + CHUNK_BYTES] for i in range(0, len(payload), CHUNK_BYTES)] or [b""]
    if len(chunks) > MAX_PARTS or not chunks[0]:
        raise ValueError("event size")
    lines = []
    for part, chunk in enumerate(chunks):
        frame = {"v": 1, "id": frame_id, "part": part, "total": len(chunks),
                 "data": base64.b64encode(chunk).decode("ascii")}
        lines.append(MARKER + json.dumps(frame, separators=(",", ":")).encode("ascii"))
    return lines


# ================================================================ 이벤트 검사
def _event_id(event, key="id"):
    value = event.get(key)
    if not isinstance(value, str) or not EVENT_ID_RE.match(value):
        raise ValueError(key)
    return value


def _optional_turn(event, cleaned, required=False):
    if "turn_id" in event or required:
        cleaned["turn_id"] = _event_id(event, "turn_id")


def _int_field(event, key, low, high):
    value = event.get(key)
    if type(value) is not int or not (low <= value <= high):
        raise ValueError(key)
    return value


def normalize_device_event(event):
    """보드 이벤트를 검사하고 알려진 필드만 남긴 사본을 돌려준다. 틀리면 ValueError."""
    kind = event.get("type")
    cleaned = {"id": _event_id(event), "type": kind}
    if kind == "state":
        if event.get("state") not in STATES:
            raise ValueError("state")
        cleaned["state"] = event["state"]
        _optional_turn(event, cleaned)
    elif kind == "message":
        text = event.get("text")
        if event.get("role") not in ROLES or not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("message")
        cleaned["role"] = event["role"]
        cleaned["text"] = text.strip()
        _optional_turn(event, cleaned)
    elif kind == "audio_start":
        cleaned["stream_id"] = _event_id(event, "stream_id")
        if event.get("role") not in ROLES:
            raise ValueError("role")
        cleaned["role"] = event["role"]
        _optional_turn(event, cleaned, required=True)
        if event.get("sample_rate") not in SAMPLE_RATES or type(event.get("sample_rate")) is not int:
            raise ValueError("sample_rate")
        cleaned["sample_rate"] = event["sample_rate"]
        cleaned["channels"] = _int_field(event, "channels", 1, 1)
        if event.get("frame_ms") not in FRAME_MS or type(event.get("frame_ms")) is not int:
            raise ValueError("frame_ms")
        cleaned["frame_ms"] = event["frame_ms"]
    elif kind == "audio_chunk":
        cleaned["stream_id"] = _event_id(event, "stream_id")
        cleaned["seq"] = _int_field(event, "seq", 0, MAX_STREAM_PACKETS - 1)
        opus = event.get("opus")
        if not isinstance(opus, str) or not opus or len(opus) > 4 * ((MAX_OPUS_PACKET + 2) // 3):
            raise ValueError("opus")
        try:
            packet = base64.b64decode(opus, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("opus")
        if not (1 <= len(packet) <= MAX_OPUS_PACKET):
            raise ValueError("opus")
        opus_packet_samples_48k(packet)          # TOC가 틀리면 여기서 거절
        cleaned["packet"] = packet
    elif kind == "audio_end":
        cleaned["stream_id"] = _event_id(event, "stream_id")
        cleaned["frames"] = _int_field(event, "frames", 0, MAX_STREAM_PACKETS)   # 보드가 보내려 한 패킷 수
    else:
        raise ValueError("type")
    return cleaned


# ================================================================ Ogg Opus
def opus_packet_samples_48k(packet):
    """Opus 패킷 TOC로 48kHz 기준 샘플 수를 구한다 (RFC 6716 3.1)."""
    if not packet:
        raise ValueError("empty packet")
    toc = packet[0]
    config = toc >> 3
    if config < 12:
        frame_10us = (1000, 2000, 4000, 6000)[config & 3]
    elif config < 16:
        frame_10us = (1000, 2000)[config & 1]
    else:
        frame_10us = (250, 500, 1000, 2000)[config & 3]
    code = toc & 3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        if len(packet) < 2:
            raise ValueError("code 3 packet")
        frames = packet[1] & 0x3F
        if frames == 0:
            raise ValueError("frame count")
    samples = frame_10us * frames * 48 // 100
    if samples > 5760:                       # 120ms 초과는 잘못된 패킷
        raise ValueError("packet duration")
    return samples


def _ogg_crc_table():
    table = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = ((value << 1) ^ 0x04C11DB7) if value & 0x80000000 else (value << 1)
            value &= 0xFFFFFFFF
        table.append(value)
    return table


_OGG_CRC_TABLE = _ogg_crc_table()


def ogg_crc(data):
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _ogg_page(serial, sequence, granule, flags, packets):
    lacing = bytearray()
    for packet in packets:
        size = len(packet)
        lacing.extend([255] * (size // 255))
        lacing.append(size % 255)
    if len(lacing) > 255:
        raise ValueError("too many segments")
    header = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, serial, sequence, 0, len(lacing))
    page = bytearray(header + bytes(lacing) + b"".join(packets))
    struct.pack_into("<I", page, 22, ogg_crc(page))
    return bytes(page)


def build_ogg_opus(packets, input_sample_rate, channels=1, serial=None):
    """Opus 패킷 목록 → (Ogg Opus 파일 bytes, 재생 길이 ms)."""
    if not packets:
        raise ValueError("no packets")
    serial = secrets.randbits(32) if serial is None else serial
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, channels, OPUS_PRE_SKIP, input_sample_rate, 0, 0)
    tags_vendor = b"coflorist-cat-bridge"
    tags = b"OpusTags" + struct.pack("<I", len(tags_vendor)) + tags_vendor + struct.pack("<I", 0)
    pages = [_ogg_page(serial, 0, 0, 0x02, [head]), _ogg_page(serial, 1, 0, 0x00, [tags])]
    sequence, granule, group, segments = 2, 0, [], 0
    for index, packet in enumerate(packets):
        need = len(packet) // 255 + 1
        if group and (segments + need > 255 or len(group) >= 50):
            pages.append(_ogg_page(serial, sequence, granule, 0x00, group))
            sequence, group, segments = sequence + 1, [], 0
        granule += opus_packet_samples_48k(packet)
        group.append(packet)
        segments += need
    pages.append(_ogg_page(serial, sequence, granule, 0x04, group))
    duration_ms = max(0, granule - OPUS_PRE_SKIP) * 1000 // 48000
    return b"".join(pages), duration_ms


def read_ogg_packets(data):
    """Ogg 파일에서 패킷을 꺼낸다(테스트·도구용). 첫 두 패킷은 OpusHead/OpusTags."""
    packets, current, offset = [], bytearray(), 0
    while offset < len(data):
        if data[offset:offset + 4] != b"OggS":
            raise ValueError("bad page")
        segments = data[offset + 26]
        lacing = data[offset + 27:offset + 27 + segments]
        body = offset + 27 + segments
        page = bytearray(data[offset:body + sum(lacing)])
        expected = struct.unpack_from("<I", page, 22)[0]
        struct.pack_into("<I", page, 22, 0)
        if ogg_crc(page) != expected:
            raise ValueError("bad crc")
        for size in lacing:
            current.extend(data[body:body + size])
            body += size
            if size < 255:
                packets.append(bytes(current))
                current = bytearray()
        offset = body
    return packets


# ================================================================ 음성 묶음
class AudioStreams:
    def __init__(self, stats, max_loss=0.05):
        self.stats = stats
        self.max_loss = max_loss
        self._streams = OrderedDict()

    def expire(self, now):
        for stream_id in [key for key, item in self._streams.items() if now - item["updated"] > STREAM_IDLE_TIMEOUT_S]:
            del self._streams[stream_id]
            self.stats.add("audio_timeout")

    def start(self, event, now):
        self.expire(now)
        stream_id = event["stream_id"]
        if stream_id in self._streams:
            self.stats.add("audio_invalid")
            return
        if len(self._streams) >= MAX_ACTIVE_STREAMS:
            self._streams.popitem(last=False)
            self.stats.add("audio_evicted")
        self._streams[stream_id] = {"meta": event, "packets": {}, "bytes": 0, "updated": now}

    def chunk(self, event, now):
        stream = self._streams.get(event["stream_id"])
        if stream is None:
            self.stats.add("audio_orphan_chunk")
            return
        if event["seq"] in stream["packets"]:
            self.stats.add("audio_duplicate_chunk")
            return
        stream["bytes"] += len(event["packet"])
        if stream["bytes"] > MAX_STREAM_BYTES or len(stream["packets"]) >= MAX_STREAM_PACKETS:
            del self._streams[event["stream_id"]]
            self.stats.add("audio_oversize")
            return
        stream["packets"][event["seq"]] = event["packet"]
        stream["updated"] = now

    def end(self, event):
        """완성되면 (meta, Ogg bytes, duration_ms), 버려야 하면 None."""
        stream = self._streams.pop(event["stream_id"], None)
        if stream is None:
            self.stats.add("audio_orphan_end")
            return None
        frames = event["frames"]
        if frames == 0:                        # 소리 없는 턴: 파일을 만들지 않는다
            self.stats.add("audio_empty")
            return None
        present = [stream["packets"][seq] for seq in range(frames) if seq in stream["packets"]]
        missing = frames - len(present)
        if not present or missing > frames * self.max_loss:
            self.stats.add("audio_dropped_missing")
            return None
        if missing:
            self.stats.add("audio_frames_missing", missing)
        meta = stream["meta"]
        try:
            ogg, duration_ms = build_ogg_opus(present, meta["sample_rate"], meta["channels"])
        except ValueError:
            self.stats.add("audio_invalid")
            return None
        return meta, ogg, duration_ms


class AudioStore:
    def __init__(self, max_files=MAX_AUDIO_FILES, max_bytes=MAX_AUDIO_TOTAL_BYTES, ttl_s=AUDIO_TTL_S):
        self._lock = threading.Lock()
        self._files = OrderedDict()            # name -> (bytes, created)
        self._bytes = 0
        self.max_files, self.max_bytes, self.ttl_s = max_files, max_bytes, ttl_s

    def put(self, data, now=None):
        now = time.monotonic() if now is None else now
        name = secrets.token_hex(12) + ".ogg"
        with self._lock:
            self._files[name] = (data, now)
            self._bytes += len(data)
            self._trim(now)
        return name

    def get(self, name, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(now)
            item = self._files.get(name)
            return item[0] if item else None

    def _trim(self, now):
        while self._files:
            name, (data, created) = next(iter(self._files.items()))
            if len(self._files) <= self.max_files and self._bytes <= self.max_bytes and now - created <= self.ttl_s:
                break
            del self._files[name]
            self._bytes -= len(data)


# ================================================================ SSE 허브
class SseClient:
    def __init__(self):
        self.queue = queue.Queue(maxsize=SSE_CLIENT_QUEUE)
        self.dropped = threading.Event()       # 너무 느려 끊긴 화면 (재접속 후 Last-Event-ID로 이어 받음)


class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._history = deque(maxlen=SSE_HISTORY)   # (id, json text)
        self._seen = OrderedDict()
        self._clients = set()

    def publish(self, event):
        text = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if event["id"] in self._seen:
                return False
            self._seen[event["id"]] = None
            while len(self._seen) > SSE_HISTORY * 2:
                self._seen.popitem(last=False)
            self._history.append((event["id"], text))
            for client in list(self._clients):
                try:
                    client.queue.put_nowait((event["id"], text))
                except queue.Full:
                    self._clients.discard(client)
                    client.dropped.set()
        return True

    def subscribe(self, last_event_id=None):
        client = SseClient()
        with self._lock:
            history = list(self._history)
            ids = [item[0] for item in history]
            start = ids.index(last_event_id) + 1 if last_event_id in ids else 0
            backlog = history[start:]
            self._clients.add(client)
        return client, backlog

    def unsubscribe(self, client):
        with self._lock:
            self._clients.discard(client)

    def client_count(self):
        with self._lock:
            return len(self._clients)


# ================================================================ 중계기 본체
class Bridge:
    def __init__(self, max_audio_loss=0.05, audio_enabled=True):
        self.stats = Stats()
        self.assembler = FrameAssembler(self.stats)
        self.streams = AudioStreams(self.stats, max_audio_loss)
        self.store = AudioStore()
        self.hub = EventHub()
        self.audio_enabled = audio_enabled
        self.serial_status = "not started"
        self._lock = threading.Lock()

    def feed_line(self, raw, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self.streams.expire(now)
            event = self.assembler.feed_line(raw, now)
            if event is None:
                return
            try:
                cleaned = normalize_device_event(event)
            except ValueError:
                self.stats.add("events_rejected")
                return
            kind = cleaned["type"]
            if kind in ("state", "message"):
                if self.hub.publish(cleaned):
                    self.stats.add("published_" + kind)
                return
            if not self.audio_enabled:
                return
            if kind == "audio_start":
                self.streams.start(cleaned, now)
            elif kind == "audio_chunk":
                self.streams.chunk(cleaned, now)
            elif kind == "audio_end":
                result = self.streams.end(cleaned)
                if result is None:
                    return
                meta, ogg, duration_ms = result
                name = self.store.put(ogg, now)
                audio_event = {"id": (cleaned["id"] + "-audio")[:160], "type": "audio", "role": meta["role"],
                               "turn_id": meta["turn_id"], "url": "/audio/" + name, "duration_ms": duration_ms}
                if self.hub.publish(audio_event):
                    self.stats.add("published_audio")

    def health(self):
        return {"ok": True, "version": VERSION, "serial": self.serial_status,
                "clients": self.hub.client_count(), "counters": self.stats.snapshot()}


# ================================================================ HTTP
AUDIO_PATH_RE = re.compile(r"^/audio/([0-9a-f]{24}\.ogg)$")


def make_handler(bridge, allowed_origins, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CatBridge/" + VERSION
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):          # 요청 기록도 남기지 않는다
            return

        def _guard(self):
            """(허용 여부, CORS로 돌려줄 Origin). 거절이면 응답까지 보낸다."""
            host = (self.headers.get("Host") or "").lower()
            if host not in allowed_hosts:      # DNS rebinding 차단
                self._plain(421, "misdirected host")
                return False, None
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                self._plain(403, "origin not allowed")
                return False, None
            return True, origin

        def _common_headers(self, origin):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)

        def _plain(self, status, text, origin=None):
            body = (text + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._common_headers(origin)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_OPTIONS(self):
            allowed, origin = self._guard()
            if not allowed:
                return
            self.send_response(204)
            self._common_headers(origin)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Last-Event-ID, Range")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            allowed, origin = self._guard()
            if not allowed:
                return
            path = self.path.split("?", 1)[0]
            if path == "/events":
                return self._events(origin)
            if path == "/health":
                body = json.dumps(bridge.health()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._common_headers(origin)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            match = AUDIO_PATH_RE.match(path)
            if match:
                return self._audio(match.group(1), origin)
            self._plain(404, "not found", origin)

        def _audio(self, name, origin):
            data = bridge.store.get(name)
            if data is None:
                return self._plain(404, "audio expired", origin)
            start, end, status = 0, len(data) - 1, 200
            byte_range = self.headers.get("Range")
            if byte_range:
                match = re.match(r"^bytes=(\d*)-(\d*)$", byte_range.strip())
                if not match or (not match.group(1) and not match.group(2)):
                    return self._range_error(len(data), origin)
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2)), len(data) - 1) if match.group(2) else len(data) - 1
                else:
                    start = max(0, len(data) - int(match.group(2)))
                if start > end or start >= len(data):
                    return self._range_error(len(data), origin)
                status = 206
            body = data[start:end + 1]
            self.send_response(status)
            self.send_header("Content-Type", "audio/ogg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(data)))
            self._common_headers(origin)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _range_error(self, size, origin):
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self._common_headers(origin)
            self.end_headers()

        def _events(self, origin):
            last_id = self.headers.get("Last-Event-ID")
            client, backlog = bridge.hub.subscribe(last_id)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self._common_headers(origin)
                self.end_headers()
                if self.command == "HEAD":
                    return
                self.wfile.write(b"retry: 2000\n\n")
                for event_id, text in backlog:
                    self._write_event(event_id, text)
                self.wfile.flush()
                while not getattr(self.server, "closing", False) and not client.dropped.is_set():
                    try:
                        item = client.queue.get(timeout=SSE_KEEPALIVE_S)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self._write_event(*item)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                bridge.hub.unsubscribe(client)
                self.close_connection = True

        def _write_event(self, event_id, text):
            self.wfile.write(("id: %s\ndata: %s\n\n" % (event_id, text)).encode("utf-8"))

    return Handler


def allowed_host_names(bind_host, port, extra_hosts=()):
    hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
    if bind_host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        hosts.add("%s:%d" % (bind_host.lower(), port))
    for host in extra_hosts:
        hosts.add(host.lower() if ":" in host else "%s:%d" % (host.lower(), port))
    return hosts


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return                             # 화면이 연결을 끊은 것뿐
        log("HTTP 요청 처리 오류: %s" % type(sys.exc_info()[1]).__name__)


def start_http(bridge, host, port, allowed_origins, extra_hosts=()):
    server = QuietServer((host, port), None)
    server.daemon_threads = True
    server.closing = False
    actual_port = server.server_address[1]
    server.RequestHandlerClass = make_handler(bridge, set(allowed_origins),
                                              allowed_host_names(host, actual_port, extra_hosts))
    thread = threading.Thread(target=server.serve_forever, name="cat-bridge-http", daemon=True)
    thread.start()
    return server, actual_port


# ================================================================ 입력(시리얼·재생)
class LineSplitter:
    def __init__(self, stats):
        self.stats = stats
        self._buffer = bytearray()
        self._skipping = False

    def push(self, data):
        lines = []
        start = 0
        while True:
            index = data.find(b"\n", start)
            if index < 0:
                break
            piece = data[start:index]
            if self._skipping:
                self._skipping = False
            elif len(self._buffer) + len(piece) <= MAX_LINE_BYTES:
                self._buffer.extend(piece)
                lines.append(bytes(self._buffer).rstrip(b"\r"))
            else:
                self.stats.add("lines_too_long")
            self._buffer = bytearray()
            start = index + 1
        rest = data[start:]
        if not self._skipping:
            if len(self._buffer) + len(rest) > MAX_LINE_BYTES:
                self._buffer = bytearray()
                self._skipping = True
                self.stats.add("lines_too_long")
            else:
                self._buffer.extend(rest)
        return lines


def _import_serial():
    """pyserial을 불러온다. 휴대용 zip이면 옆의 vendor/ 폴더에 넣어 둔 것도 쓴다."""
    vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.insert(0, vendor)
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        raise SystemExit("pyserial이 없어요: python3 -m pip install -r bridge/requirements.txt")
    return serial, list_ports


def open_serial(port, baud):
    serial, _ = _import_serial()
    handle = serial.Serial()
    handle.port = port
    handle.baudrate = baud
    handle.timeout = 0.5
    handle.dtr = False                         # CH342 자동 리셋/다운로드 진입 방지
    handle.rts = False
    handle.open()
    return handle


CH342_VID, CH342_PID = 0x1A86, 0x55D2
CH342_LOG_INTERFACE = 2        # 인터페이스 0 = UART0(펌웨어 굽기), 2 = UART1(로그, CAT_EVT)


def list_serial_ports():
    _, list_ports = _import_serial()
    return sorted(list_ports.comports(), key=lambda item: (getattr(item, "vid", None) != CH342_VID, item.device))


def usb_interface(port):
    """USB 인터페이스 번호를 장치 정보에서만 읽는다. 포트를 열지 않는다(리셋 방지)."""
    location = getattr(port, "location", None) or ""
    match = re.search(r":(?:x|\d+)\.(\d+)$", location)        # Windows "1-2:x.2", Linux "1-1:1.2"
    if match:
        return int(match.group(1))
    interface = getattr(port, "interface", None)
    if isinstance(interface, str) and interface.isdigit():
        return int(interface)
    device = getattr(port, "device", "") or ""
    match = re.search(r"usbmodem\w*?(\d)$", device)           # macOS: 끝자리 = 인터페이스 + 1
    serial_number = getattr(port, "serial_number", None) or ""
    if match and serial_number and serial_number in device:
        return int(match.group(1)) - 1
    return None


def pick_board_port(ports):
    """(포트, None) 또는 (None, 안내문). CH342 보드가 하나이고 로그 인터페이스가 분명할 때만 고른다."""
    boards = [item for item in ports if getattr(item, "vid", None) == CH342_VID and getattr(item, "pid", None) == CH342_PID]
    if not boards:
        return None, "고양이 보드(CH342, VID 1a86 PID 55d2)를 찾지 못했어요. USB 선(데이터용)과 드라이버를 확인해 주세요."
    chips = {}
    for item in boards:
        chips.setdefault(getattr(item, "serial_number", None) or getattr(item, "hwid", ""), []).append(item)
    if len(chips) > 1:
        return None, "보드가 %d개 연결돼 있어요. --port로 하나를 지정해 주세요." % len(chips)
    candidates = [item for item in boards if usb_interface(item) == CH342_LOG_INTERFACE]
    if len(candidates) == 1:
        return candidates[0].device, None
    names = ", ".join(sorted(item.device for item in boards))
    return None, ("보드 포트(%s) 중 로그 포트를 확실히 고르지 못했어요. 펌웨어 굽기 포트가 아닌 다른 쪽을 "
                  "--port로 지정해 주세요 (--list-ports 참고)." % names)


def choose_port():
    port, message = pick_board_port(list_serial_ports())
    if port is None:
        raise SystemExit(message)
    log("보드 로그 포트 자동 선택: %s" % port)
    return port


def serial_loop(bridge, port, baud, stop):
    splitter = LineSplitter(bridge.stats)
    while not stop.is_set():
        try:
            handle = open_serial(port, baud)
        except Exception as error:
            bridge.serial_status = "waiting for %s" % port
            log("포트 %s 열기 실패(%s), 2초 뒤 다시 시도" % (port, type(error).__name__))
            stop.wait(2.0)
            continue
        bridge.serial_status = "connected %s" % port
        log("포트 %s 연결 (%d bps)" % (port, baud))
        try:
            while not stop.is_set():
                data = handle.read(4096)
                if data:
                    for line in splitter.push(data):
                        bridge.feed_line(line)
                else:
                    bridge.feed_line(b"")        # 조립 타임아웃 정리
        except Exception as error:
            bridge.serial_status = "disconnected"
            log("포트 %s 끊김(%s), 다시 연결합니다" % (port, type(error).__name__))
        finally:
            try:
                handle.close()
            except Exception:
                pass
        stop.wait(1.0)


def replay_loop(bridge, path, delay_ms, stop):
    stream = sys.stdin.buffer if path == "-" else open(path, "rb")
    try:
        for line in stream:
            if stop.is_set():
                break
            bridge.feed_line(line[:MAX_LINE_BYTES].rstrip(b"\r\n"))
            if delay_ms:
                stop.wait(delay_ms / 1000.0)
    finally:
        if stream is not sys.stdin.buffer:
            stream.close()
    bridge.serial_status = "replay finished"
    log("재생 파일 끝")


# ================================================================ 실행
def parse_args(argv):
    parser = argparse.ArgumentParser(description="고양이 보드 USB → CoFlorist 외부 대화 중계기")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--port", help="보드 로그 시리얼 포트 (예: /dev/cu.usbmodem…3, COM5, /dev/ttyACM1). "
                                       "생략하거나 auto면 CH342 로그 인터페이스를 장치 정보로 찾음(포트를 열어 보지 않음)")
    source.add_argument("--replay", metavar="FILE", help="시리얼 대신 CAT_EVT 줄이 담긴 파일(- 는 표준입력)로 시험")
    source.add_argument("--list-ports", action="store_true", help="시리얼 포트 목록만 보여 주고 끝내기")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 바인딩 주소 (기본 loopback)")
    parser.add_argument("--http-port", type=int, default=4321)
    parser.add_argument("--allow-origin", action="append", default=[], help="추가로 허용할 브라우저 출처")
    parser.add_argument("--allow-host", action="append", default=[], help="추가로 허용할 Host 헤더")
    parser.add_argument("--replay-delay-ms", type=int, default=0)
    parser.add_argument("--max-audio-loss", type=float, default=0.05, help="음성 프레임 누락 허용 비율 (0~0.5)")
    parser.add_argument("--no-audio", action="store_true", help="음성 이벤트를 무시하고 텍스트·상태만 전달")
    args = parser.parse_args(argv)
    if not 0 <= args.max_audio_loss <= 0.5:
        parser.error("--max-audio-loss는 0~0.5")
    if not (args.port or args.replay or args.list_ports):
        args.port = "auto"
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.list_ports:
        ports = list_serial_ports()
        chosen, message = pick_board_port(ports)
        for item in ports:
            vid = "%04x:%04x" % (item.vid, item.pid) if getattr(item, "vid", None) is not None else "-"
            role = ""
            if getattr(item, "vid", None) == CH342_VID and getattr(item, "pid", None) == CH342_PID:
                interface = usb_interface(item)
                role = {0: "보드 굽기 포트", CH342_LOG_INTERFACE: "보드 로그 포트"}.get(interface, "보드 포트")
            print("%s\t%s\t%s%s" % (item.device, vid, item.description, ("\t← " + role) if role else ""))
        print("\n자동 선택: %s" % chosen if chosen else "\n" + message)
        return 0
    origins = ["http://localhost:4317", "http://127.0.0.1:4317"] + args.allow_origin
    bridge = Bridge(max_audio_loss=args.max_audio_loss, audio_enabled=not args.no_audio)
    stop = threading.Event()
    port = args.port
    if port == "auto":
        port = choose_port()
    server, http_port = start_http(bridge, args.host, args.http_port, origins, args.allow_host)
    log("대시보드 외부 대화 주소: http://127.0.0.1:%d/events" % http_port)
    worker = threading.Thread(
        target=replay_loop if args.replay else serial_loop,
        args=(bridge, args.replay, args.replay_delay_ms, stop) if args.replay else (bridge, port, args.baud, stop),
        name="cat-bridge-input", daemon=True)
    worker.start()
    try:
        while True:                            # 재생이 끝나도 화면이 붙을 수 있게 계속 연다
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.closing = True
        server.shutdown()
        log("종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
