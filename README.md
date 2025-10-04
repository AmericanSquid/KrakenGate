# KrakenGate – Remote Control for Legacy Radios

**KrakenGate** is a **spin-off of [KrakenRelay](https://github.com/AmericanSquid/KrakenRelay)** designed for **remote operation of legacy radios** over the internet using [Mumble](https://www.mumble.info).

It’s currently in **testing / development**, so expect rough edges.

KrakenGate reuses:

* The **`ptt_controller` module** directly from KrakenRelay (CM108 HID PTT control).
* A **modified version of the old, deprecated Mumble interface** from KrakenRelay, adapted into a standalone `MumbleBridge`.

---

## ✨ Purpose

Many older or “legacy” radios don’t support native network control or digital modes.
KrakenGate gives you a way to:

* Listen to your station’s audio anywhere via Mumble.
* Key your transmitter remotely with hardware PTT.
* Feed audio back into the radio so remote users can transmit through it.

Essentially, it’s an RF-to-internet bridge built from a Raspberry Pi and a DigiRig interface.

---

## ✨ Features

* **Two-way audio bridging**

  * **Radio RX → Mumble**: captures audio from the radio and streams it to a Mumble channel.
  * **Mumble → Radio TX**: plays remote Mumble audio into the radio when PTT is keyed.

* **Hardware PTT with CM108**

  * Keys/unkeys a USB CM108 interface (or DigiRig Lite) on a defined GPIO pin.

* **Web UI + REST API**

  * PTT button + spacebar control.
  * Real-time RX/TX meters.
  * JSON status API for integration/monitoring.

* **Secure tunneling**

  * Web server is meant to run behind SSH tunnel or VPN.

---

## ⚠️ Status

This is **experimental**.
It’s being tested as a lightweight, single-purpose bridge, separate from KrakenRelay’s larger feature set.
Expect:

* Breaking changes.
* Incomplete error handling.
* Bugs carried over from the legacy Mumble interface.

---

## 🗂 Requirements

* Linux host (Raspberry Pi recommended).
* **Legacy/analog radio** with:

  * Audio in/out connected to a USB soundcard.
  * PTT control via CM108 HID device (e.g. DigiRig Lite).
* Python 3.9+.

Python deps:

```bash
pip install sounddevice flask pymumble-py3 python-dotenv numpy
```

---

## ⚙️ Configuration

All settings are in `.env`:

| Variable          | Default           | Description                  |
| ----------------- | ----------------- | ---------------------------- |
| `MUMBLE_SERVER`   | `127.0.0.1`       | Mumble server address        |
| `MUMBLE_PORT`     | `64738`           | Server port                  |
| `MUMBLE_USERNAME` | `shackpi`         | Username                     |
| `MUMBLE_PASSWORD` | `password`        | Password                     |
| `MUMBLE_CHANNEL`  | `RemoteTx`        | Channel to join              |
| `PTT_DEVICE`      | `/dev/hidraw0`    | HID device                   |
| `PTT_PIN`         | `3`               | Pin to toggle                |
| `AUDIO_INPUT`     | `USB Audio CODEC` | Input device (radio → Pi)    |
| `AUDIO_OUTPUT`    | `USB Audio CODEC` | Output device (Pi → radio)   |
| `SAMPLE_RATE`     | `48000`           | Sample rate                  |
| `CHUNK`           | `1024`            | Block size                   |
| `TAIL_HANG`       | `0.75`            | Seconds of hang before unkey |
| `HTTP_PORT`       | `5000`            | Flask server port            |

---

## 🚀 Running

```bash
python3 remote_trx.py
```

Access from your browser:

Open [http://localhost:5000](http://localhost:5000) for the dashboard.

* Click/hold the button or press **spacebar** for PTT.
* Watch RX/TX meters update in real time.

---

## 🔀 Audio / PTT Flow

```
Legacy Radio RX ──► USB Soundcard (Input) ──► MumbleBridge ──► Mumble Server
Mumble Server ──► MumbleBridge ──► USB Soundcard (Output) ──► Legacy Radio Mic (when PTT active)
                                        │
                                        ▼
                                CM108 HID PTT Keying
```

---

## 🛑 Stopping

Press **Ctrl+C** to exit.
The app will unkey, close audio streams, and disconnect cleanly.

---

## 🔮 Roadmap / Known Gaps

* Refine error handling and reconnection logic.
* Reconcile TX/RX audio level reporting.
* Cleanup leftover legacy code from KrakenRelay’s Mumble interface.
