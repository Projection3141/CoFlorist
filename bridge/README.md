# 고양이 보드 USB 중계기

로봇팔 위에 단 고양이 보드(Tuya T5AI)를 USB로 노트북에 꽂으면, 보드의 대화 상태·문장·음성을
CoFlorist 대시보드의 **외부 대화** 화면에 보여 줍니다.

```
보드 로그 UART(460800) ──USB──▶ cat_usb_bridge.py ──SSE(127.0.0.1:4321)──▶ CoFlorist 외부 대화
```

- 보드 로그 가운데 `CAT_EVT` 줄만 읽습니다. 다른 로그(Wi-Fi 정보 등)는 읽자마자 버리고 화면·파일 어디에도 남기지 않습니다.
- 음성은 메모리에만 두고(최대 24개·16MB·1시간) 이 컴퓨터 안에서만 열립니다. 대시보드는 자동 재생하지 않고, 문장 아래 재생 버튼을 누를 때만 소리를 냅니다.
- 로봇 명령이나 LLM 요청은 다루지 않습니다.

## 설치 (대시보드를 띄우는 노트북)

Python 3.8 이상과 pyserial만 있으면 됩니다.

```
python3 -m pip install -r bridge/requirements.txt
```

Windows에서는 `python3` 대신 `py`를 쓰면 됩니다.

## Windows에서 실행 (가장 간단한 방법)

1. 보드를 USB로 연결합니다.
2. `bridge` 폴더의 **`start-bridge.bat`을 더블클릭**합니다.
   - Python이 없으면 설치 안내가 나옵니다. https://www.python.org/downloads/ 에서 설치할 때 **Add python.exe to PATH**를 체크하세요.
   - pyserial이 없으면 처음 한 번 자동으로 설치합니다. 인터넷이 안 되는 곳이면 아래 휴대용 zip을 쓰세요.
3. 창에 `보드 로그 포트 자동 선택: COM…`이 보이면 연결된 것입니다. COM 번호는 몰라도 됩니다.
4. 대시보드(http://localhost:4317) → 설정 → **외부 대화**에 `http://127.0.0.1:4321/events`를 넣고 연결합니다.
5. 끝낼 때는 창에서 `Ctrl+C`를 누릅니다.

자동 선택이 안 되면 창에 나온 안내대로 `start-bridge.bat --port COM8`처럼 포트를 지정합니다. 장치 관리자 → 포트(COM & LPT)에 보드 포트가 두 개 보이고, 그중 펌웨어 굽기 포트가 아닌 쪽입니다.

### 휴대용 zip 만들기 (인터넷 없는 노트북용)

```
python3 -m pip install --target bridge/vendor pyserial    # pyserial을 폴더 안에 넣음 (순수 Python, OS 무관)
```

그다음 `bridge` 폴더를 통째로 zip으로 묶습니다. `cat_usb_bridge.py`, `start-bridge.bat`, `requirements.txt`, `README.md`, `vendor/`가 들어 있으면 됩니다. `tests/`와 `tools/`는 빼도 됩니다. 받는 쪽은 압축을 풀고 `start-bridge.bat`만 실행하면 됩니다(Python 3.8 이상은 필요).

## 직접 실행 (macOS·Linux·Windows 공통)

1. 보드를 USB로 연결합니다. CH342 칩(VID 1a86, PID 55d2)이라 포트가 두 개 생깁니다. USB 인터페이스 0은 펌웨어 굽기용(UART0), 인터페이스 2는 로그용(UART1, `CAT_EVT`)입니다.
2. 포트 확인. 어느 쪽이 로그 포트인지 표시해 줍니다.
   ```
   python3 bridge/cat_usb_bridge.py --list-ports
   ```
3. 중계기 실행. `--port`를 생략하면 **포트를 열어 보지 않고** USB 장치 정보(인터페이스 번호)로 로그 포트를 고릅니다. 보드가 두 개 이상이거나 인터페이스를 알 수 없으면 고르지 않고 안내만 합니다.
   ```
   python3 bridge/cat_usb_bridge.py                                  # 자동 선택
   python3 bridge/cat_usb_bridge.py --port /dev/cu.usbmodemXXXX3     # macOS (끝자리 3이 로그)
   python3 bridge/cat_usb_bridge.py --port COM8                      # Windows
   python3 bridge/cat_usb_bridge.py --port /dev/ttyACM1              # Linux
   ```
4. 대시보드(`npm start`, http://localhost:4317) → 설정 → **외부 대화**에 `http://127.0.0.1:4321/events`를 넣고 연결합니다.

중계기는 로그 포트를 열 때 DTR/RTS를 내려서 보드가 리셋되거나 다운로드 모드로 들어가지 않게 합니다. 굽기 포트는 열지 않습니다. 펌웨어를 구울 때는 중계기를 먼저 끄세요.

### 보드 없이 시험

```
python3 bridge/tools/make_catevt.py demo > demo.log
python3 bridge/cat_usb_bridge.py --replay demo.log --replay-delay-ms 40
```

### 자주 쓰는 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--baud` | 460800 | 보드 로그 속도 |
| `--http-port` | 4321 | 대시보드가 붙을 포트 |
| `--host` | 127.0.0.1 | 다른 컴퓨터에서 보려면 LAN IP로 바꾸고 `--allow-origin`, `--allow-host`도 지정 |
| `--no-audio` | 끔 | 텍스트·상태만 전달 |
| `--max-audio-loss` | 0.05 | 음성 패킷이 이 비율보다 많이 빠지면 그 음성은 버림 |

`http://127.0.0.1:4321/health`에서 연결 상태와 숫자 카운터(받은 조각 수, 버린 음성 수 등)를 볼 수 있습니다. 대화 내용은 들어 있지 않습니다.

## 브라우저

음성은 Ogg Opus 파일입니다. Chrome, Edge, Firefox에서 재생됩니다. Safari는 버전에 따라 재생되지 않을 수 있으니 Chrome이나 Edge를 권장합니다.

## 보드 → 중계기 규칙 (펌웨어 계약 v1)

보드는 로그 UART에 한 줄씩 씁니다(`tal_log_print_raw` 사용, 줄 앞에 다른 글자가 붙어도 됩니다).

```
CAT_EVT {"v":1,"id":"<부팅hex>-<번호>","part":0,"total":N,"data":"<base64>"}
```

- 원본 이벤트 JSON(UTF-8, 최대 8,192바이트)을 **72바이트씩** 잘라 각각 base64로 담습니다. 마지막 조각만 72바이트보다 짧을 수 있습니다.
- `id`는 `[A-Za-z0-9_-]` 1~64자, `part`는 0부터, `total`은 1~128입니다.
- 조각은 다른 이벤트 조각과 섞여 와도 됩니다. 2초 안에 다 오지 않으면 버립니다. 같은 `id`가 다시 오면 무시합니다.
- 원문은 base64 안에만 있으므로 로그 형식 문자(`%`) 문제가 없습니다. 보드에서 STT·답변 원문을 일반 로그로 직접 출력하면 안 됩니다.

복원한 JSON 이벤트:

| type | 필드 | 중계기 처리 |
|---|---|---|
| `state` | `id`, `state`(idle·listening·thinking·speaking·error), `turn_id`? | 그대로 SSE |
| `message` | `id`, `role`(user·assistant), `text`(1~2,000자), `turn_id`? | 그대로 SSE |
| `audio_start` | `id`, `stream_id`, `turn_id`, `role`, `sample_rate`(8000·12000·16000·24000·48000), `channels`(1), `frame_ms`(10·20·40·60) | 묶기 시작 |
| `audio_chunk` | `id`, `stream_id`, `seq`(0부터), `opus`(패킷 base64, 1~1,275바이트) | 모으기 |
| `audio_end` | `id`, `stream_id`, `frames`(보드가 보내려 한 패킷 수, 0이면 음성 없음) | Ogg Opus로 묶어 `audio` 이벤트 |

음성 상한: 스트림당 2,000패킷(60ms 기준 120초)·600KB, 동시에 8개, 30초 동안 새 조각이 없으면 버립니다.

## 중계기 → 대시보드 (SSE)

`GET /events`는 `id:`/`data:` 형식의 SSE이고, `Last-Event-ID`로 이어 받을 수 있습니다(최근 500개). 브라우저 출처는 `http://localhost:4317`, `http://127.0.0.1:4317`만 허용합니다(그 밖은 403). Host 헤더도 loopback만 받습니다(DNS rebinding 차단).

`state`·`message`에 더해 음성 이벤트가 옵니다.

```json
{"id":"…-audio","type":"audio","role":"assistant","turn_id":"…","url":"/audio/<24hex>.ogg","duration_ms":1213}
```

`url`은 스트림 주소 기준 상대 경로입니다. 대시보드는 스트림과 같은 출처의 음성만 받아들이고, 같은 `turn_id`·`role`의 문장 아래에 재생 버튼으로 붙입니다(문장보다 먼저 와도 기다렸다가 붙임). `GET /audio/<이름>.ogg`는 `Range` 요청을 지원합니다.

## 테스트

```
python3 -m unittest discover -s bridge/tests     # 중계기 (보드·시리얼 없이)
npm test                                         # 대시보드 외부 대화 포함
```
