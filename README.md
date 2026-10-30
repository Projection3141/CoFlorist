# CoFlorist

**Track: Physical AI.** CoFlorist is an operator-assisted flower-arranging dashboard. A person talks to it (typed or spoken Korean); a **real, isolated 384-neuron subset of the FlyWire fruit-fly connectome** is stimulated and measured; **NVIDIA Nemotron on Nebius Token Factory** turns the request plus the measured circuit state into a short spoken reply and a flower placement plan; a **human operator** then drives the robot arm and confirms every pick, place and hand-over. No robot command is sent automatically, the circuit generates no words, and nothing here claims that a fly brain understands flowers or emotions.

*English summary first; the full Korean guide follows below. 한국어 안내는 아래에 있습니다.*

## Why we built it

Flower shops are full of small, repeated hand-overs where the hard part is timing, not grasping: wait, offer, or reach. We wanted a physical assistant whose "when to act" signal comes from a measurable biological circuit rather than a hand-tuned timer, and whose language comes from an open-weight NVIDIA model that a small team can run through a pay-per-token API. The fruit-fly sugar-sensing circuit is one of the few complete, public, cell-level circuits where stimulus in and motor neuron out are both known, so it can be driven and read honestly.

## How it works

1. **Input.** The main screen accepts a typed sentence or a microphone recording (up to 60 s). Speech is transcribed locally with faster-whisper (CPU, int8, `base`). The retained `flora/` reference device (a Tuya T5AI cat) is another input path, documented in `docs/EXTERNAL_FEED_CONTRACT.md`.
2. **Fly circuit (input → output in one sentence).** Explicit app rules map the utterance to a stimulus rate in Hz on the 21 sugar-sensing neurons of a 384-neuron, 20,407-connection FlyWire v630 subset; Brian2 simulates 200 ms; the measured firing (active neurons, downstream rate, MN9 motor-neuron rate) is thresholded by app rules into `wait` / `confirm` / `propose` (shown as WAIT / OFFER / REACH). That decision gates how the model answers and what the operator is advised to do. It is a connectome-derived subset, not a whole brain, and it does not perceive flower colour. Measured sweep: `docs/FLORA_BRAIN_SWEEP.md`.
3. **Language: NVIDIA Nemotron via Nebius Token Factory.** `server/nebius.js` calls `https://api.tokenfactory.nebius.com/v1/chat/completions` at runtime with the system prompt in `server/system-prompt.txt`. The model receives the user turn, recipient context and the circuit decision, and must return one JSON object: `reply` (2–3 short Korean sentences), `intent` (`gift|arrange|pause|resume|approve|change|chat`) and `placements` (colour / position / quantity). Default model: **`nvidia/nemotron-3-super-120b-a12b`** (`NEBIUS_MODEL`; any `nvidia/*nemotron*` ID on the account is accepted). The `flora/` reference server uses **`nvidia/Nemotron-3_5-Lightning`** on the same endpoint, chosen after a side-by-side test for Korean quality at 0.6–0.9 s per spoken turn (`flora/docs/MEASUREMENTS.md`).
4. **Output.** The reply is shown and spoken (Windows Korean voice, WAV), the placement plan appears as pending steps, and the operator performs and logs start / placed / failed / retry, then user approval, lift, hand-over, receipt and gripper release. Only after that is a delivery marked complete.

Nemotron is used **as served, with prompt engineering only**: no fine-tuning, no weights in this repository. The fly circuit never generates sentences; the prompt tells the model to treat the circuit result as a pacing signal, not as evidence about the user's feelings.

## Quick start (verified on Windows)

Requirements: Windows, Node.js 24, Python 3.10+ (3.12 recommended), Chrome or Edge, a Windows Korean voice (e.g. Microsoft Heami), internet access for Nebius. A Nebius Token Factory API key is required for model answers: sign in to the Nebius Token Factory console, create an API key, and put it in `.env`; usage is billed per token (see the API reference link below).

```powershell
npm install
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # then set NEBIUS_API_KEY (and NEBIUS_MODEL if you want another Nemotron ID)
powershell -File scripts/setup-brain.ps1 -PythonExecutable "C:\path\to\python.exe"   # downloads ~90 MB of pinned FlyWire source data once
npm run setup:voice
npm start
```

Open http://localhost:4317 (the server binds to the local PC only). API keys never reach the browser, SQLite or exports.

**Without an API key** you can still verify the parts that do not call a model:

```powershell
npm test                              # API, pipeline and local-speech tests (no network key needed)
node python/test-brain-worker.mjs     # real circuit numbers: no-stimulus vs stimulus, same-seed reproducibility, silencing
```

macOS: `sh scripts/setup-brain-macos.sh` prepares and tests the circuit (Python 3.13 confirmed); the local voice pipeline (Whisper setup script and Korean TTS) is Windows-only at the moment. The `flora/server` reference implementation has 175 tests that run with fake providers and no keys (`cd flora/server && uv run pytest -q`).

## What is real and what is not

- The circuit is a real FlyWire v630 subset with original signed connection weights; removing the rest of the brain changes its dynamics. Neuron and edge counts and firing rates are measured values.
- Badges and placement plans are **suggestions**. The robot is manually operated; the app records what the operator did. Numerical tests do not imply a successful physical pick.
- Recipient "history" is app memory, not neural learning. The camera view is display-only; nothing recognises flowers or people.
- Speech recognition and synthesis are local open-source / OS components, not Nemotron.

## Timeline (submission-period disclosure)

Nothing in this repository predates the hackathon's prior-work cut-off. The CoFlorist repository was created on 2026-10-25. The `flora/` reference server descends from a voice-device server the same team started on 2026-09-29 and switched to Nemotron on Nebius Token Factory on 2026-10-29; the cat firmware was written in October 2026. During the submission period we added the FlyWire circuit worker and sweep, the Nemotron dialogue pipeline, the operator workflow, the external-feed contract and the Flora reference device/server.

## Sources and licenses

- CoFlorist is MIT-licensed ([LICENSE](LICENSE)). The `flora/` reference implementation is MIT under its own `LICENSE`, except TuyaOpen-derived files, which stay Apache-2.0 ([flora/THIRD_PARTY_NOTICES.md](flora/THIRD_PARTY_NOTICES.md), [flora/firmware/LICENSE-APACHE-2.0](flora/firmware/LICENSE-APACHE-2.0)).
- Third-party code, datasets, model weights and user-provided images retain their own licenses; see `python/README.md` and the dataset manifest.
- Fly circuit: [Drosophila brain model](https://github.com/philshiu/Drosophila_brain_model/tree/91bdd1e7dcf193f3e7ca5a8933497fcef63b7960) © 2023 Philip Shiu and Nico Spiller, MIT (Shiu et al., *Nature* 2024). Underlying data: FlyWire v630 connectome by the FlyWire Consortium (Dorkenwald et al., *Nature* 2024; Schlegel et al., *Nature* 2024), downloaded at setup time from the pinned repository, with sources, hashes and extraction rule recorded in `data/flywire/manifest.json`; the dataset itself is not redistributed here and remains under FlyWire's terms.
- Language model: NVIDIA Nemotron (OpenMDW open weights), accessed as a hosted service on [Nebius Token Factory](https://docs.tokenfactory.nebius.com/api-reference/introduction). No weights are included.
- [Brian2](https://brian2.readthedocs.io/) (simulation), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (local STT), Windows SpeechSynthesizer (local Korean TTS).

---

## 한국어 안내 (Korean guide)

메인 화면에서 텍스트를 입력하거나 마이크로 말하면, 실제 FlyWire 부분회로를 계산한 뒤 그 결과를 반영한 Nebius 답변을 한국어 음성으로 들려주는 로컬 꽃 작업 대시보드입니다. 화면은 **메인**과 **설정**으로 구성됩니다.

대화 흐름은 다음과 같습니다.

1. 메시지 입력칸의 텍스트 또는 마이크 녹음을 받습니다. 녹음은 로컬 Whisper가 텍스트로 인식합니다.
2. 입력 문장을 앱의 명시적인 규칙으로 자극률에 매핑하고, Python/Brian2가 실제 FlyWire 부분회로를 계산합니다.
3. 활성 뉴런·스파이크·하위 회로 발화율에서 읽은 결과를 NVIDIA Nemotron에 전달하여 답변을 만듭니다.
4. 답변을 메인 대화에 표시하고 Windows 한국어 목소리로 WAV를 생성하여 브라우저에서 재생합니다.

부분회로가 직접 한국어 문장을 생성하는 것은 아닙니다. 입력과 신경 자극, 신경 활동과 응답 방식의 연결은 앱에서 정의한 매핑이며, 실제 문장 생성은 Nebius의 언어 모델이 담당합니다. 신경 계산에 실패하면 해당 대화의 모델 생성을 진행하지 않습니다.

## 실행

Windows, Node.js 24, Python 3.10 이상, 최근 Chrome 또는 Edge, Windows 한국어 음성 팩(예: Microsoft Heami)이 필요합니다. 음성 인식과 합성은 이 PC에서 실행하며, Nebius 답변 생성에는 인터넷 연결이 필요합니다.

```powershell
npm install
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

이미 `.env`가 있으면 복사하지 않고 기존 파일을 사용하세요. `.env`의 `NEBIUS_API_KEY`와 `NEBIUS_MODEL`을 설정합니다. 기본 모델은 `nvidia/nemotron-3-super-120b-a12b`이며, 계정에서 사용할 수 있는 NVIDIA Nemotron 모델이어야 합니다. 키는 브라우저·SQLite·내보내기에 포함되지 않습니다.

처음 한 번 실제 신경망 데이터와 음성 인식 모델을 준비합니다.

```powershell
powershell -File scripts/setup-brain.ps1 -PythonExecutable "C:\path\to\python.exe"
npm run setup:voice
npm start
```

[http://localhost:4317](http://localhost:4317)을 여세요. 서버는 로컬 PC에만 바인딩됩니다. 기존 `.venv`와 `data/flywire/`가 준비되어 있으면 신경망 설치를 반복할 필요가 없습니다.

## 메인 화면에서 대화하기

- 입력칸에 문장을 쓰고 보내기를 누르거나, 마이크 버튼을 눌러 말한 다음 다시 눌러 녹음을 마칩니다. 녹음은 최대 60초이며 8MB 이내로 전송됩니다.
- 메인 신경망 영역에서 실제 계산 상태와 측정 결과를 확인합니다. 이후 답변 생성과 음성 준비 상태가 이어집니다.
- 답변 음성은 자동으로 재생됩니다. 브라우저가 자동 재생을 차단하면 해당 답변의 재생 버튼을 누르세요.
- 대기·초기화·대화 연결 끄기는 진행 중인 대화와 재생을 중단합니다. 실제 로봇 정지는 운영자가 수행합니다.

**설정 → 대화 연결**에는 켜기/끄기만 있습니다. API 키와 모델은 `.env`에서 읽으며 화면에서 입력하거나 변경하지 않습니다. 별도 음성 시연 페이지와 외부 대화 연결은 제공하지 않습니다.

받는 사람과 이전 이야기, 카메라, 음성 출력, 신경망 경로는 기존 설정에서 관리합니다. 이전 이야기가 예시라면 **시연용 예시 이야기**로 표시하세요. 현재 대화에서 말한 조건을 먼저 사용하며, 카메라는 표시용이고 꽃이나 사람을 인식하지 않습니다. [카메라 연결 설명](docs/CAMERA.md)

## 실제 신경망 실행 범위

현재 준비 데이터는 **384개 뉴런과 20,407개 실제 연결로 구성된 고립 부분회로**입니다. 전체 초파리 뇌를 실행하는 것이 아닙니다. 선호·실패 기록은 앱의 대화 기억이며 신경 학습을 의미하지 않습니다. 데이터 출처와 SHA-256은 `data/flywire/manifest.json`에 기록됩니다.

메인에서 대화를 보내면 작업자가 자동으로 시작됩니다. 경로를 바꿔야 할 때는 **설정 → 초파리 신경망**에서 `.venv\Scripts\python.exe`와 `data\flywire\subset.json`을 지정하세요. 설치·부분회로 구성에 대해서는 [작업자 설명](python/README.md)과 [측정된 자극 실험](docs/FLORA_BRAIN_SWEEP.md)을 참고하세요.

## 꽃 작업과 운영자 확인

꽃 색·위치·수량을 지정하면 남은 배치가 제안됩니다. 운영자가 실제 로봇을 조종하고 작업 시작·배치 완료·실패·재시도를 기록합니다. 배치를 마친 뒤 사용자 승인, 들어 올리기, 건네기, 실제 수령 확인, 그리퍼 해제 확인을 차례로 수행해야 전달 완료가 됩니다.

`flora/`와 `examples/`의 외부 기기 코드는 참고 구현으로 보관되어 있습니다. 현재 메인 화면의 대화는 위의 로컬 입력·신경 계산·Nebius·음성 경로를 사용합니다.

## 검증과 저장

```powershell
npm test
node python/test-brain-worker.mjs
```

API 테스트는 별도 로컬 서버에서 환경 변수 키 비노출, 연결 끄기, 입력 검증, 운영자 상태 전이, WAV 재생과 구간 요청을 확인합니다. 신경망 작업자 검증은 실제 데이터로 자극 없는 상태와 자극 입력, 동일 시드 재현성, 연결 억제 효과를 계산합니다. 수치 검증은 실제 로봇 동작의 성공을 의미하지 않습니다.

비밀이 아닌 설정과 대화·감사 기록은 `.runtime/coflorist.sqlite`에 저장됩니다. 서버 재시작은 새 활성 대화를 시작하며 설정을 유지합니다. 생성 WAV와 Whisper 모델도 Git에서 제외된 `.runtime/`에 저장됩니다. 내보내기에는 수령자 이야기와 대화·작업 기록이 포함되지만 API 키는 포함되지 않습니다.

## 출처와 라이선스

- [Nebius Token Factory API](https://docs.tokenfactory.nebius.com/api-reference/introduction): 실제 NVIDIA Nemotron 모델 요청.
- [Drosophila brain model](https://github.com/philshiu/Drosophila_brain_model/tree/91bdd1e7dcf193f3e7ca5a8933497fcef63b7960): copyright © 2023 Philip Shiu and Nico Spiller, MIT. 원본 라이선스는 `data/flywire/source/LICENSE`에 보존됩니다.
- [Brian2](https://brian2.readthedocs.io/): 신경 계산.
- [FlyWire](https://flywire.ai/) v630 connectome: 위 저장소가 고정 커밋으로 내려받는 원본 데이터. FlyWire Consortium(Dorkenwald et al., Nature 2024; Schlegel et al., Nature 2024)에 귀속되며 데이터 이용 조건은 FlyWire의 안내를 따릅니다. 저장소에는 데이터 원본이 들어 있지 않고 `data/flywire/manifest.json`에 출처·해시만 기록됩니다.
- [NVIDIA Nemotron](https://huggingface.co/nvidia) (OpenMDW 공개 가중치): Nebius Token Factory에서 호스팅 서비스로 호출합니다. 가중치는 저장소에 포함되지 않습니다.
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper): CPU int8 Whisper base 음성 인식.
- [Windows SpeechSynthesizer](https://learn.microsoft.com/en-us/uwp/api/windows.media.speechsynthesis.speechsynthesizer): 설치된 한국어 목소리로 WAV 합성.

CoFlorist는 MIT 라이선스입니다([LICENSE](LICENSE)). `flora/` 참고 구현은 자체 MIT 라이선스를 사용하며, TuyaOpen에서 유래한 파일에는 Apache-2.0이 적용됩니다([고지](flora/THIRD_PARTY_NOTICES.md)).
