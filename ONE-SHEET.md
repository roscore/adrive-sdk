# ADrive LH42A 한 장 매뉴얼

한국어 · [English](#english)

SDK·매뉴얼·예제 코드: **https://github.com/roscore/adrive-sdk**

## 1. 시작 전에
- 액추에이터를 바이스나 볼트로 고정하십시오. 테이프·헐거운 받침 위에서 구동하지 마십시오.
- 손이 닿는 곳에 하드웨어 전원 차단 장치를 두십시오. 소프트웨어 정지는 안전 인증 E-stop이 아닙니다.
- 전원 48 V DC. 연결은 USB(Windows·Linux 드라이버 설치 없음) 또는 CAN.
- 전원을 켜기 전에 캐리지를 스트로크 중앙 부근에 두십시오. 전원 인가 직후 드라이브가 엔코더 인덱스를 스스로 탐색하며 캐리지가 짧게 움직입니다. 양 끝에서 15 mm 이상 떨어뜨리고, 엔드스톱·바이스·지그·부하에 걸리지 않게 하십시오.

## 2. 설치
```bash
pip install sdk/adrive-0.1.1-cp310-abi3-win_amd64.whl               # Windows x64
pip install sdk/adrive-0.1.1-cp310-abi3-manylinux_2_34_aarch64.whl  # Linux aarch64
python -c "import adrive; print(adrive.scan())"
```

## 3. 연결과 호밍 (전원을 켤 때마다 필수)
```python
import adrive
act = adrive.connect()   # USB/CAN 자동 탐지
act.home()               # 후퇴 끝점까지 저속 이동, 그 지점이 0 mm
```
호밍이 끝나기 전에는 이동 명령이 거부됩니다.

## 4. 이동
| 호출 | 의미 |
|---|---|
| `act.enable()` / `act.disable()` | 위치 유지 / 모터 해제 |
| `act.move_to(mm, speed=, accel=)` | 절대 위치 이동 (mm, mm/s, mm/s²) |
| `act.position()` | 현재 위치 (mm) |
| `act.stop()` | 정지 후 위치 유지 |
| `adrive.connect_all()` | USB·CAN에서 찾은 액추에이터 전부 |

기본값은 정격(200 mm/s, 2000 mm/s²)의 10 %. 소프트 리밋은 0.5 mm부터. 첫 구동이 느린 것은 정상입니다.

## 4-1. 스트로크 한계 · 게인 튜닝
| 호출 | 의미 |
|---|---|
| `act.set_stroke_limits(min, max, save=False)` | 이동 범위(mm). 밖은 거부. `save=True` 는 이 PC 에 기억 |
| `act.get_gains()` / `act.set_gains(pos=, vel=, vel_integrator=)` | 게인 읽기 / RAM 적용 (출하값 0.2–5 배) |
| `act.restore_factory_gains()` | 출하값 복원 (RAM) |
| `act.save()` | 드라이브에 저장. 모터 끈 상태에서만, 재부팅 후 다시 연결·호밍 |

GUI: 축 카드의 "Tuning …" — 게인 슬라이더, 스텝 응답(오버슈트·정착 시간), 스트로크 한계, 공장값 복원.
출하 게인은 유닛별 성적서 값이라 유닛마다 다릅니다. `restore_factory_gains()` 로 되돌리십시오.

## 5. GUI
Windows: `ADrive-GUI.exe`. Linux: `python gui/adrive_gui.py`. 하드웨어 없이 `--sim 4`.
호밍 버튼부터 누르십시오. Space 또는 Esc로 전 축 정지.

## 6. 문제 해결
| 증상 | 확인 |
|---|---|
| 장치가 보이지 않음 | USB 케이블, 48 V 전원, Linux는 사용자 USB 장치 권한 |
| "not homed" 오류 | `act.home()` 실행 |
| 축이 스스로 정지 | 통신 워치독. 스크립트가 명령을 계속 보내야 함 |
| 정지 중 위치가 미세하게 흔들림 | 0.1.1 기본 설정의 알려진 한계 |
| 연결 시 `E111` 오류 | 펌웨어가 SDK 를 지원하지 않는 시험 빌드입니다. 펌웨어 빌드 06 이상 업데이트를 요청하십시오 |
| 전원 인가 직후 엔코더 오류 `CPR_POLEPAIRS_MISMATCH`(0x2) / `ENCODER_FAILED`(0x100), 모터가 꺼진 채 | 엔코더 탐색 중 캐리지가 걸린 경우입니다. 전원을 끄고 캐리지를 엔드스톱에서 15 mm 이상 손으로 떨어뜨려 걸림을 제거한 뒤 다시 켜십시오. 재발하면 엔코더 커넥터를 확인하십시오. 저장된 설정 문제가 아닙니다 |

---

# English

SDK, manuals and examples: **https://github.com/roscore/adrive-sdk**

## 1. Before you start
- Fix the actuator with a vise or bolted mount. Never run it on tape or a loose base.
- Keep a hardware power cut-off within reach. The software stop is not a safety-rated E-stop.
- Supply 48 V DC. Connect over USB (no driver install on Windows or Linux) or CAN.
- Before power-up, place the carriage near the middle of the stroke. Right after power is applied the drive searches for the encoder index by itself and the carriage moves briefly. Keep it at least 15 mm from either end and free of the end stop, vise, fixture or load.

## 2. Install
```bash
pip install sdk/adrive-0.1.1-cp310-abi3-win_amd64.whl               # Windows x64
pip install sdk/adrive-0.1.1-cp310-abi3-manylinux_2_34_aarch64.whl  # Linux aarch64
python -c "import adrive; print(adrive.scan())"
```

## 3. Connect and home (required after every power-up)
```python
import adrive
act = adrive.connect()   # auto-detects USB or CAN
act.home()               # slow move to the retracted end stop; sets 0 mm
```
Motion commands are refused until homing has finished.

## 4. Move
| Call | Meaning |
|---|---|
| `act.enable()` / `act.disable()` | Hold position / release motor |
| `act.move_to(mm, speed=, accel=)` | Absolute move (mm, mm/s, mm/s²) |
| `act.position()` | Current position in mm |
| `act.stop()` | Stop and hold |
| `adrive.connect_all()` | All actuators found on USB and CAN |

Defaults: 10 % of the rated limits (200 mm/s, 2000 mm/s²). Soft limit starts at 0.5 mm. A slow first run is expected.

## 4-1. Stroke limits · gain tuning
| Call | Meaning |
|---|---|
| `act.set_stroke_limits(min, max, save=False)` | Travel range (mm). Outside is rejected. `save=True` remembers it on this PC |
| `act.get_gains()` / `act.set_gains(pos=, vel=, vel_integrator=)` | Read gains / apply in RAM (0.2–5x factory) |
| `act.restore_factory_gains()` | Back to factory gains (RAM) |
| `act.save()` | Store in the drive. Motor disabled only; reconnect and home after the reboot |

GUI: "Tuning …" on the axis card — gain sliders, step response (overshoot, settling time), stroke limits, factory restore.
Factory gains are per-unit certificate values and differ between units. Use `restore_factory_gains()` to go back to them.

## 5. GUI
Windows: `ADrive-GUI.exe`. Linux: `python gui/adrive_gui.py`. Add `--sim 4` to run without hardware.
Press Home first. Space or Esc stops all axes.

## 6. Troubleshooting
| Symptom | Check |
|---|---|
| No device found | USB cable, 48 V supply, USB device permission for your user on Linux |
| "not homed" error | Run `act.home()` |
| Axis stops by itself | Communication watchdog: keep your script sending commands |
| Position hunts slightly at rest | Known limit of default settings in 0.1.1 |
| `E111` error at connect | The firmware is a test build without SDK support. Ask for firmware build 06 or later |
| Encoder error `CPR_POLEPAIRS_MISMATCH` (0x2) / `ENCODER_FAILED` (0x100) right after power-up, motor stays off | The carriage was blocked during the encoder search. Cut power, move the carriage by hand at least 15 mm from the end stop, remove the obstruction and power up again. If it recurs, check the encoder connector. Not a stored-settings problem |
