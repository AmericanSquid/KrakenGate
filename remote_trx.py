import os
import time
import logging
import threading
import numpy as np
import sounddevice as sd
from flask import Flask, jsonify, Response
from pymumble_py3 import Mumble
from dotenv import load_dotenv
from ptt_controller import CM108PTT
from mumble_bridge import MumbleBridge


load_dotenv()
# ------------- ENV CONFIG -------------
MUMBLE_SERVER   = os.getenv("MUMBLE_SERVER")
MUMBLE_USERNAME = os.getenv("MUMBLE_USERNAME", "shackpi")
MUMBLE_PASSWORD = os.getenv("MUMBLE_PASSWORD", "password")
MUMBLE_CHANNEL  = os.getenv("MUMBLE_CHANNEL", "RemoteTx")
MUMBLE_PORT     = int(os.getenv("MUMBLE_PORT", "64738"))

PTT_DEVICE      = os.getenv("PTT_DEVICE", "/dev/hidraw0")
PTT_PIN         = int(os.getenv("PTT_PIN", "3"))

INPUT_NAME      = os.getenv("AUDIO_INPUT", "USB Audio CODEC")   # radio -> Pi (mic)
OUTPUT_NAME     = os.getenv("AUDIO_OUTPUT", "USB Audio CODEC")  # Pi -> radio (speaker)

TAIL_HANG       = float(os.getenv("TAIL_HANG", "0.75"))
HTTP_PORT       = int(os.getenv("HTTP_PORT", "5000"))

SAMPLE_RATE     = int(os.getenv("SAMPLE_RATE", "48000"))
CHUNK           = int(os.getenv("CHUNK", "1024"))
# --------------------------------------

# Globals
app = Flask(__name__)
ptt: CM108PTT = CM108PTT(device=PTT_DEVICE, pin=PTT_PIN)
mumble: Mumble | None = None

is_transmitting = False
last_key_time   = 0.0

rx_stream = None
tx_thread: threading.Thread | None = None
tx_lock = threading.Lock()

# simple RMS → dBFS meter for int16 PCM
current_rx_dbfs = -100.0
current_tx_dbfs = -100.0

def _dbfs_from_int16(arr: np.ndarray) -> float:
    """Compute dBFS from int16 mono samples."""
    if arr.size == 0:
        return -100.0
    x = arr.astype(np.float32)
    rms = np.sqrt(np.mean(x * x))
    return float(20.0 * np.log10(rms / 32767.0)) if rms > 0 else -100.0


def find_device_index(name: str, is_input=True) -> int:
    for idx, dev in enumerate(sd.query_devices()):
        if name.lower() in dev["name"].lower():
            if is_input and dev["max_input_channels"] > 0:
                return idx
            if not is_input and dev["max_output_channels"] > 0:
                return idx
    raise RuntimeError(f"Audio device not found or not usable for {'input' if is_input else 'output'}: {name}")

input_index  = find_device_index(INPUT_NAME,  is_input=True)
output_index = find_device_index(OUTPUT_NAME, is_input=False)

# ---------- AUDIO PATHS ----------
def audio_rx_loop():
    """Capture RF audio from radio and send into Mumble (radio → Mumble)."""
    def callback(indata, frames, time_info, status):
        global current_rx_dbfs
        current_rx_dbfs = _dbfs_from_int16(indata.reshape(-1))
        mumble_bridge.send_pcm(indata.copy())

    global rx_stream
    rx_stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=CHUNK,
        device=input_index,
        channels=1,
        dtype="int16",
        callback=callback,
    )
    rx_stream.start()
    logging.info("🎧 RX stream started (radio → mumble)")

def audio_tx_loop():
    """Spawn a thread that plays incoming Mumble audio out to the radio while TX is active."""
    def play_loop():

        global current_tx_dbfs

        logging.info("🔊 TX audio stream open (mumble → radio)")
        stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=CHUNK,
            device=output_index,
            channels=1,
            dtype="int16",
        )
        stream.start()
        try:
            while True:
                with tx_lock:
                    tx_active = is_transmitting
                if not tx_active:
                    break

                # Pull PCM from Mumble input buffer
                pcm_bytes = mumble_bridge.get_received(timeout=0.1)
                if pcm_bytes:
                    arr = np.frombuffer(pcm_bytes, dtype=np.int16)
                    stream.write(arr.reshape(-1, 1))
                    current_tx_dbfs = _dbfs_from_int16(audio.reshape(-1))
                else:
                    current_tx_dbfs = -100.0    
                    stream.write(np.zeros((CHUNK, 1), dtype=np.int16))
        finally:
            stream.stop()
            stream.close()
            logging.info("⛔ TX audio stream closed")

    global tx_thread
    # If a thread is already running, don't start another
    if tx_thread and tx_thread.is_alive():
        return
    tx_thread = threading.Thread(target=play_loop, daemon=True)
    tx_thread.start()

# ---------- TX CONTROL ----------
def start_tx():
    """Key CM108, enable Mumble TX, and ensure TX audio thread is running."""
    global is_transmitting, last_key_time
    with tx_lock:
        if not is_transmitting:
            logging.info("🟢 TX ON")
            try:
                ptt.key()
            except Exception as e:
                logging.error(f"PTT key() failed: {e}")
            is_transmitting = True
            audio_tx_loop()  # ensures the writer thread is up
        last_key_time = time.time()

def stop_tx_after_tail():
    """Unkey after TAIL_HANG seconds if no new key event arrives."""
    def _worker():
        time.sleep(TAIL_HANG)
        global is_transmitting
        with tx_lock:
            if is_transmitting and (time.time() - last_key_time) >= TAIL_HANG:
                logging.info("⚪ TX OFF (tail hang)")
                try:
                    ptt.unkey()
                except Exception as e:
                    logging.error(f"PTT unkey() failed: {e}")
                # flip state
                is_transmitting = False
    threading.Thread(target=_worker, daemon=True).start()

# ---------- FLASK API ----------
@app.route("/ptt/on", methods=["POST"])
def http_ptt_on():
    start_tx()
    return jsonify({"status": "tx_on", "tail": TAIL_HANG})

@app.route("/ptt/off", methods=["POST"])
def http_ptt_off():
    stop_tx_after_tail()
    return jsonify({"status": "tx_off_pending", "tail": TAIL_HANG})

@app.route("/status", methods=["GET"])
def http_status():
    age = time.time() - last_key_time if last_key_time else None
    with tx_lock:
        tx = is_transmitting
    return jsonify({
        "tx": tx,
        "last_key_age": age,
        "sr": SAMPLE_RATE,
        "chunk": CHUNK,
        "rx_dbfs": current_rx_dbfs,
        "tx_dbfs": current_tx_dbfs,
        "mumble_connected": mumble_bridge.connected if mumble else False
    })

@app.route("/")
def index():
    # tiny HTML UI with Tailwind-lite styles and vanilla JS
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Remote TRX</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Inter, Arial; background:#0f1116; color:#e6e6e6; margin:0; padding:24px; }}
  .card {{ background:#171a22; border:1px solid #252a36; border-radius:16px; padding:20px; max-width:720px; margin:auto; box-shadow: 0 10px 30px rgba(0,0,0,.35); }}
  h1 {{ margin:0 0 8px 0; font-size:22px; }}
  .row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
  .chip {{ padding:6px 10px; border-radius:999px; border:1px solid #2b3142; font-size:13px; }}
  .ok {{ color:#9cff9c; border-color:#254d2a; background:#112412; }}
  .warn {{ color:#ffd88a; border-color:#5a4927; background:#291e0b; }}
  .bad {{ color:#ff9a9a; border-color:#5a2a2a; background:#2a1414; }}
  .btn {{ font-weight:600; font-size:18px; padding:14px 22px; border-radius:12px; border:1px solid #3f86ff; background:#123; color:#eaf2ff; cursor:pointer; }}
  .btn:active {{ transform: translateY(1px); }}
  .btn.tx {{ border-color:#25d366; background:#102619; }}
  .meters {{ display:grid; grid-template-columns: 1fr 1fr; gap:16px; margin-top:16px; }}
  .meter {{ background:#11141b; border:1px solid #23293a; border-radius:12px; padding:12px; }}
  .bar {{ height:14px; border-radius:8px; background:linear-gradient(90deg,#1b2735, #1b2735); overflow:hidden; }}
  .fill {{ height:100%; background:linear-gradient(90deg,#6cff6c,#ffdf6a,#ff6a6a); width:0%; transition: width .1s linear; }}
  .label {{ font-size:12px; opacity:.8; margin-top:6px; display:flex; justify-content:space-between; }}
  .kbd {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#0b0d11; border:1px solid #2a3040; border-radius:6px; padding:2px 6px; }}
  .footer {{ opacity:.6; font-size:12px; text-align:center; margin-top:18px; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Remote TRX</h1>
    <div class="row" id="chips">
      <span class="chip" id="chip-mumble">Mumble: …</span>
      <span class="chip" id="chip-tx">TX: …</span>
      <span class="chip">SR: {SAMPLE_RATE} Hz</span>
      <span class="chip">Chunk: {CHUNK}</span>
    </div>

    <div style="margin:16px 0;">
      <button id="ptt" class="btn">PTT</button>
      <span style="margin-left:10px; font-size:13px; opacity:.8;">Hold <span class="kbd">Space</span> as well</span>
    </div>

    <div class="meters">
      <div class="meter">
        <div style="margin-bottom:6px;">RX (radio → Mumble)</div>
        <div class="bar"><div id="rxfill" class="fill"></div></div>
        <div class="label"><span id="rxdb">-∞ dBFS</span><span>Input: {INPUT_NAME}</span></div>
      </div>
      <div class="meter">
        <div style="margin-bottom:6px;">TX (Mumble → radio)</div>
        <div class="bar"><div id="txfill" class="fill"></div></div>
        <div class="label"><span id="txdb">-∞ dBFS</span><span>Output: {OUTPUT_NAME}</span></div>
      </div>
    </div>

    <div class="footer">Use via SSH tunnel: <span class="kbd">ssh -N -L 5000:localhost:5000 user@pi</span></div>
  </div>

<script>
const btn = document.getElementById('ptt');
let down = false;
let lastSent = 0;

function pctFromDb(db) {{
  // Map -60..0 dB → 0..100%
  if (db <= -60) return 0;
  if (db >= 0) return 100;
  return (db + 60) / 60 * 100;
}}

async function ptt(on) {{
  const now = Date.now();
  if (now - lastSent < 40) return; // rate-limit a bit
  lastSent = now;
  try {{
    await fetch(on ? '/ptt/on' : '/ptt/off', {{ method:'POST' }});
  }} catch (e) {{}}
}}

btn.addEventListener('mousedown', () => {{ down = true; btn.classList.add('tx'); ptt(true); }});
btn.addEventListener('mouseup',   () => {{ down = false; btn.classList.remove('tx'); ptt(false); }});
btn.addEventListener('mouseleave',() => {{ if(down) {{ down = false; btn.classList.remove('tx'); ptt(false); }} }});

// Spacebar hold
window.addEventListener('keydown', (e) => {{
  if (e.code === 'Space' && !down) {{ down = true; btn.classList.add('tx'); ptt(true); e.preventDefault(); }}
}});
window.addEventListener('keyup', (e) => {{
  if (e.code === 'Space' && down) {{ down = false; btn.classList.remove('tx'); ptt(false); e.preventDefault(); }}
}});

// Poll status
async function poll() {{
  try {{
    const r = await fetch('/status');
    const s = await r.json();
    // chips
    const cm = document.getElementById('chip-mumble');
    cm.textContent = 'Mumble: ' + (s.mumble_connected ? 'Connected' : 'Disconnected');
    cm.className = 'chip ' + (s.mumble_connected ? 'ok' : 'bad');

    const ct = document.getElementById('chip-tx');
    ct.textContent = 'TX: ' + (s.tx ? 'ON' : 'OFF');
    ct.className = 'chip ' + (s.tx ? 'ok' : 'warn');

    // meters
    const rxdb = document.getElementById('rxdb');
    const txdb = document.getElementById('txdb');
    const rxfill = document.getElementById('rxfill');
    const txfill = document.getElementById('txfill');

    const rx = (s.rx_dbfs ?? -100).toFixed(1);
    const tx = (s.tx_dbfs ?? -100).toFixed(1);
    rxdb.textContent = rx + ' dBFS';
    txdb.textContent = tx + ' dBFS';
    rxfill.style.width = pctFromDb(parseFloat(rx)) + '%';
    txfill.style.width = pctFromDb(parseFloat(tx)) + '%';
  }} catch (e) {{}}
  setTimeout(poll, 150);
}}
poll();
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")
# ---------- MUMBLE ----------
def on_mumble_ready():
    logging.info("🔗 Connected to Mumble server")
    if MUMBLE_CHANNEL and mumble:
        chan = mumble.channels.find_by_name(MUMBLE_CHANNEL)
        if chan:
            chan.move_in()
            logging.info(f"📡 Joined channel: {MUMBLE_CHANNEL}")

# replace your connect_mumble() with:
def connect_mumble():
    global mumble, mumble_bridge
    mumble_bridge = MumbleBridge(
        server=MUMBLE_SERVER,
        user=MUMBLE_USERNAME,
        password=MUMBLE_PASSWORD,
        port=MUMBLE_PORT,
        channel=MUMBLE_CHANNEL,
        sample_rate=SAMPLE_RATE,
        ping_interval=5.0,
        log_pings=True,
        reconnect=True,
    )
    mumble_bridge.start()
    # keep compatibility with the rest of your code:
    mumble = mumble_bridge.client

# ---------- MAIN ----------
def main():
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(message)s", force=True)
    logging.info("🚀 Starting remote_trx")

    connect_mumble()
    audio_rx_loop()  # radio → Mumble always-on

    # Start HTTP control (use SSH tunnel for security)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True, use_reloader=False),
        daemon=True
    ).start()
    logging.info(f"🌐 HTTP PTT server on 0.0.0.0:{HTTP_PORT}  (POST /ptt/on, /ptt/off, GET /status)")

    # Keep foreground alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("🛑 Ctrl+C received. Shutting down.")
    finally:
        # Clean downlinks
        with tx_lock:
            if is_transmitting:
                try:
                    if mumble:
                        mumble.set_tx(False)
                except Exception:
                    pass
                try:
                    ptt.unkey()
                except Exception:
                    pass
        try:
            if rx_stream:
                rx_stream.stop()
                rx_stream.close()
        except Exception:
            pass
        try:
            if mumble:
                mumble.stop()
        except Exception:
            pass
        try:
            if mumble_bridge:
                mumble_bridge.stop()
        except Exception:
            pass
        logging.info("✅ Clean exit")

if __name__ == "__main__":
    main()
