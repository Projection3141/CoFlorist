#!/usr/bin/env python3
"""보드 없이 시험할 CAT_EVT 줄을 만든다 (펌웨어 규칙의 참고 구현).

  python3 bridge/tools/make_catevt.py demo > demo.log
  python3 bridge/cat_usb_bridge.py --replay demo.log --replay-delay-ms 40

demo는 듣기 → 사용자 문장·음성 → 생각 → 답변 문장·음성 → 대기 순서의 가상 대화이고,
사이사이 일반 로그 줄을 섞어 중계기가 무시하는지 보여 준다.
"""
import argparse
import base64
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cat_usb_bridge import encode_event_lines, read_ogg_packets  # noqa: E402

DEFAULT_AUDIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "fixtures", "tone-16k-60ms.opus")


class Writer:
    def __init__(self, out, boot=None):
        self.out = out
        self.boot = boot or secrets.token_hex(4)
        self.counter = 0

    def event(self, event):
        self.counter += 1
        event = dict(event)
        event.setdefault("id", "%s-%06d" % (self.boot, self.counter))
        for line in encode_event_lines(event, "%s-%06d" % (self.boot, self.counter)):
            self.out.write(line + b"\r\n")
        return event["id"]

    def noise(self, text):
        self.out.write(("[10-25 05:11:11 ty W][cat_network.c:1] %s\r\n" % text).encode("ascii"))

    def audio(self, path, role, turn_id, sample_rate=16000):
        with open(path, "rb") as handle:
            packets = read_ogg_packets(handle.read())[2:]     # OpusHead/OpusTags 제외
        stream_id = "%s-s%04d" % (self.boot, self.counter + 1)
        self.event({"type": "audio_start", "stream_id": stream_id, "turn_id": turn_id, "role": role,
                    "sample_rate": sample_rate, "channels": 1, "frame_ms": 60})
        for seq, packet in enumerate(packets):
            self.event({"type": "audio_chunk", "stream_id": stream_id, "seq": seq,
                        "opus": base64.b64encode(packet).decode("ascii")})
        self.event({"type": "audio_end", "stream_id": stream_id, "frames": len(packets)})


def demo(writer, audio_path):
    turn = "%s-t1" % writer.boot
    writer.noise("Cat voice ready")
    writer.event({"type": "state", "state": "listening", "turn_id": turn})
    writer.event({"type": "message", "role": "user", "text": "[보드 시험] 그 사람에게 꽃을 선물하고 싶어.", "turn_id": turn})
    writer.audio(audio_path, "user", turn)
    writer.noise("Cat recording sent frames=20 dropped=0")
    writer.event({"type": "state", "state": "thinking", "turn_id": turn})
    writer.audio(audio_path, "assistant", turn)          # 문장보다 음성이 먼저 와도 붙어야 한다
    writer.event({"type": "message", "role": "assistant", "text": "[보드 시험] 흰 꽃과 빨간 꽃을 함께 준비할까요?", "turn_id": turn})
    writer.event({"type": "state", "state": "speaking", "turn_id": turn})
    writer.event({"type": "state", "state": "idle", "turn_id": turn})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    demo_parser = sub.add_parser("demo", help="가상 대화 한 턴")
    demo_parser.add_argument("--audio", default=DEFAULT_AUDIO, help="음성으로 쓸 Ogg Opus 파일")
    args = parser.parse_args()
    writer = Writer(sys.stdout.buffer)
    if args.command == "demo":
        demo(writer, args.audio)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
