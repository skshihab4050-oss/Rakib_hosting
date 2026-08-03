import os
import json
import signal
import subprocess
import shutil
import zipfile
import hashlib
import psutil
import threading
import time
import urllib.request
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, send_file, abort
import io
import platform

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(exist_ok=True)

NORMAL_PASSWORD = os.environ.get("NORMAL_PASSWORD", "admin123")
SITE_NAME = "RAKIB"
OWNER_NAME = "RAKIB"

RUNNING_PROCESSES = {}

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": "#00ff41",
            "normal_password": NORMAL_PASSWORD,
            "site_name": SITE_NAME,
            "auto_restart_interval": 300
        }
    }

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_theme_color():
    data = load_data()
    return data.get("settings", {}).get("theme_color", "#00ff41")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        data = load_data()
        settings = data.get("settings", {})
        if settings.get("maintenance"):
            return render_template_string(MAINTENANCE_TEMPLATE,
                message=settings.get("maintenance_msg", "Under maintenance"),
                site_name=settings.get("site_name", SITE_NAME),
                theme_color=get_theme_color(),
                owner_name=OWNER_NAME)
        return f(*args, **kwargs)
    return decorated

def is_process_alive(pid):
    try:
        if not pid:
            return False
        p = psutil.Process(pid)
        return p.is_running() and p.status() not in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def kill_process(pid):
    try:
        p = psutil.Process(pid)
        children = p.children(recursive=True)
        # প্রথমে children কিল করুন
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        
        # তারপর parent প্রসেস কিল করুন
        try:
            p.terminate()
            p.wait(timeout=3)
        except psutil.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
        except Exception:
            pass
            
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def get_run_command(runtime, main_file):
    ext = Path(main_file).suffix.lower()
    if runtime == "node" or ext in (".js", ".ts", ".mjs"):
        return ["node", main_file]
    elif runtime == "static":
        return ["python", "-m", "http.server", "8080"]
    else:
        return ["python", "-u", main_file]

def _sync_process_status():
    data = load_data()
    changed = False
    for name, cfg in data["servers"].items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            changed = True
            # Clean up from RUNNING_PROCESSES
            if name in RUNNING_PROCESSES:
                try:
                    RUNNING_PROCESSES[name]["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[name]
    if changed:
        save_data(data)

# Initial sync
_sync_process_status()

def render_keep_alive():
    while True:
        try:
            time.sleep(600)
            port = os.environ.get("PORT", 5000)
            url = f"http://127.0.0.1:{port}/api/ping"
            req = urllib.request.Request(url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
            urllib.request.urlopen(req, timeout=10)
            external_url = os.environ.get("RENDER_EXTERNAL_URL")
            if external_url:
                ping_url = f"{external_url}/api/ping"
                req2 = urllib.request.Request(ping_url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
                urllib.request.urlopen(req2, timeout=10)
        except Exception:
            pass

threading.Thread(target=render_keep_alive, daemon=True).start()

@app.route("/api/ping")
def ping():
    return "pong", 200

def keep_alive():
    while True:
        time.sleep(240)
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url:
                ping_url = f"{url}/api/ping"
            else:
                port = os.environ.get("PORT", 5000)
                ping_url = f"http://127.0.0.1:{port}/api/ping"
            req = urllib.request.Request(ping_url, headers={'User-Agent': 'KeepAlive-Bot/1.0'})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

threading.Thread(target=keep_alive, daemon=True).start()

def auto_restart_server(name):
    try:
        data = load_data()
        cfg = data["servers"].get(name)
        if not cfg:
            return
        pid = cfg.get("pid")
        if pid and is_process_alive(pid):
            kill_process(pid)
            if name in RUNNING_PROCESSES:
                try:
                    RUNNING_PROCESSES[name]["proc"].terminate()
                    RUNNING_PROCESSES[name]["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[name]
        main_file = cfg.get("main_file") or "main.py"
        main_cmd = cfg.get("main_command") or ""
        extract_dir = SERVERS_DIR / name / "extracted"
        main_path = extract_dir / main_file
        if not main_path.exists():
            return
        log_path = SERVERS_DIR / name / "logs.txt"
        if main_cmd:
            cmd = main_cmd.split()
        else:
            cmd = get_run_command(cfg.get("runtime", "python"), main_file)
        env = os.environ.copy()
        env["PORT"] = str(cfg.get("port", 8080))
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] AUTO-RESTART triggered\n{'='*50}\n")
        log_file = open(log_path, "a")
        
        # Cross-platform process creation
        if platform.system() == "Windows":
            proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env)
        else:
            proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
            
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
    except Exception as e:
        print(f"Auto-restart error for {name}: {e}")

def auto_restart_monitor():
    while True:
        try:
            data = load_data()
            settings = data.get("settings", {})
            interval = settings.get("auto_restart_interval", 300)
            for name, cfg in data["servers"].items():
                pid = cfg.get("pid")
                if pid and not is_process_alive(pid):
                    cfg["status"] = "stopped"
                    cfg["pid"] = None
                    if name in RUNNING_PROCESSES:
                        try:
                            RUNNING_PROCESSES[name]["log_file"].close()
                        except Exception:
                            pass
                        del RUNNING_PROCESSES[name]
                    save_data(data)
                if cfg.get("status") == "stopped":
                    main_file = cfg.get("main_file") or "main.py"
                    extract_dir = SERVERS_DIR / name / "extracted"
                    if (extract_dir / main_file).exists():
                        threading.Thread(target=auto_restart_server, args=[name], daemon=True).start()
            time.sleep(interval)
        except Exception:
            time.sleep(30)

threading.Thread(target=auto_restart_monitor, daemon=True).start()

@app.route("/bg-music.mp3")
def serve_music():
    for f in BASE_DIR.iterdir():
        if f.is_file() and f.suffix.lower() == ".mp3":
            return send_file(f, mimetype="audio/mpeg")
    return "", 404

MATRIX_SCRIPT = """
<script>
(function(){
  const canvas = document.getElementById('matrix');
  if(!canvas) return;
  const ctx = canvas.getContext('2d');
  function resize(){ canvas.width = window.innerWidth; canvas.height = window.innerHeight; }
  resize();
  window.addEventListener('resize', resize);
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%^&*()ｱｲｳｴｵｶｷｸｹｺ";
  const fontSize = 16;
  const columns = Math.floor(canvas.width / fontSize);
  const drops = Array(columns).fill(1);
  function drawMatrix(){
    ctx.fillStyle = "rgba(5, 0, 15, 0.15)";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const ac = getComputedStyle(document.documentElement).getPropertyValue('--ac').trim() || '#fbbf24';
    ctx.fillStyle = ac;
    ctx.font = fontSize + "px 'JetBrains Mono'";
    for(let i=0;i<drops.length;i++){
      const text = chars.charAt(Math.floor(Math.random() * chars.length));
      ctx.fillText(text, i*fontSize, drops[i]*fontSize);
      if(drops[i]*fontSize > canvas.height && Math.random()>0.975) drops[i]=0;
      drops[i]++;
    }
  }
  setInterval(drawMatrix, 50);
  let autoInterval = null;
  const themeColors = ["#a855f7","#00ff41","#38bdf8","#ef4444","#fbbf24","#06b6d4","#ec4899","#84cc16"];
  let colorIndex = 0;
  function checkAutoMode(){
    const isAuto = localStorage.getItem("autoThemeState") === "on";
    if(isAuto){
      if(!autoInterval){
        autoInterval = setInterval(()=>{
          const next = themeColors[colorIndex];
          document.documentElement.style.setProperty('--ac', next);
          document.documentElement.style.setProperty('--ac-dim', next+'88');
          document.documentElement.style.setProperty('--ac-lite', next+'11');
          document.documentElement.style.setProperty('--ac-glow', next+'22');
          colorIndex = (colorIndex+1)%themeColors.length;
        }, 2000);
      }
    }else{
      if(autoInterval){ clearInterval(autoInterval); autoInterval=null; }
    }
  }
  checkAutoMode();
  setInterval(checkAutoMode, 2000);
})();
</script>
"""

AUDIO_PLAYER = """
<audio id="bgMusic" loop>
  <source src="/bg-music.mp3" type="audio/mpeg">
</audio>
<script>
(function(){
  const music = document.getElementById('bgMusic');
  const vol = localStorage.getItem('musicVolume');
  if(vol !== null) music.volume = parseFloat(vol);
  else music.volume = 0.5;
  const playing = localStorage.getItem('musicPlaying');
  if(playing !== 'false'){
    music.play().catch(()=>{});
  }
  window.toggleMusic = function(){
    if(music.paused){ music.play(); localStorage.setItem('musicPlaying','true'); }
    else { music.pause(); localStorage.setItem('musicPlaying','false'); }
  };
  window.setVolume = function(v){
    music.volume = v;
    localStorage.setItem('musicVolume', v);
  };
})();
</script>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ site_name }}— Login</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root{ --ac:#fbbf24; --ac-dim:#fbbf2488; --ac-lite:#fbbf2411; --ac-glow:#fbbf2422; }
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#05000f;font-family:'JetBrains Mono',monospace;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}
    canvas{position:fixed;inset:0;z-index:1}
    .container{position:relative;z-index:10;width:100%;max-width:400px;padding:0 20px}
    .card{
      position:relative;background:rgba(10,0,16,0.9);border:2px solid var(--ac);border-radius:16px;
      padding:40px 32px;box-shadow:0 0 50px var(--ac-glow), inset 0 0 30px var(--ac-lite);backdrop-filter:blur(4px);
    }
    .visit-badge{
      position:absolute;top:-14px;left:50%;transform:translateX(-50%);background:#05000f;padding:6px 20px;
      color:var(--ac);font-size:11px;font-weight:bold;letter-spacing:2px;white-space:nowrap;
      text-shadow:0 0 8px var(--ac);border:1.5px solid var(--ac);border-radius:30px;z-index:11;
      box-shadow:0 0 15px var(--ac-glow);
    }
    .brand-logo-wrap{text-align:center;margin-bottom:20px;display:flex;justify-content:center}
    .brand-logo{
      width:110px;height:110px;object-fit:cover;border-radius:50%;border:3px solid var(--ac);
      filter:drop-shadow(0 0 20px var(--ac));padding:3px;background:#000;
    }
    .brand{text-align:center;font-size:26px;font-weight:700;color:var(--ac);letter-spacing:4px;text-shadow:0 0 15px var(--ac);}
    .sub{text-align:center;font-size:12px;color:var(--ac-dim);letter-spacing:3px;margin-top:6px;margin-bottom:28px;}
    .prompt{font-size:14px;color:var(--ac);margin-bottom:20px;font-weight:bold;text-align:center;}
    .cursor{display:inline-block;width:10px;height:16px;background:var(--ac);margin-left:4px;vertical-align:middle;animation:blink 1s step-end infinite}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
    input{
      width:100%;padding:14px 16px;background:rgba(13,0,13,0.6);border:2px solid var(--ac);border-radius:10px;
      color:var(--ac);font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:700;outline:none;margin-bottom:14px;
      box-shadow:inset 0 0 12px var(--ac-lite), 0 0 10px var(--ac-glow);transition:all 0.3s;
    }
    input:focus{border-color:#fff;box-shadow:0 0 20px var(--ac-dim), inset 0 0 12px var(--ac-lite)}
    input::placeholder{color:var(--ac);opacity:0.5}
    .pw-wrap{position:relative;margin-bottom:16px}
    .pw-wrap input{margin-bottom:0;padding-right:48px}
    .eye-btn{position:absolute;right:14px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;color:var(--ac);font-size:20px;padding:0;opacity:0.8}
    .remember-wrap{display:flex;align-items:center;gap:10px;margin-bottom:24px;cursor:pointer;font-size:13px;color:var(--ac);}
    .remember-wrap input[type="checkbox"]{width:18px;height:18px;margin-bottom:0;accent-color:var(--ac);cursor:pointer}
    .auth-btn{
      width:100%;padding:14px;background:transparent;border:2px solid var(--ac);border-radius:10px;color:var(--ac);
      font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700;letter-spacing:3px;cursor:pointer;
      box-shadow:0 0 15px var(--ac-glow);transition:all 0.3s;text-shadow:0 0 8px var(--ac-dim);
    }
    .auth-btn:hover{background:var(--ac-lite);box-shadow:0 0 30px var(--ac-dim)}
    .like-bot{display:block;text-align:center;margin-top:18px;font-size:13px;color:var(--ac-dim);text-decoration:none;transition:all .2s;letter-spacing:1px}
    .like-bot:hover{color:var(--ac);text-shadow:0 0 10px var(--ac)}
    .footer{text-align:center;margin-top:28px;font-size:10px;color:var(--ac-dim);letter-spacing:2px;line-height:1.8;opacity:.7}
  </style>
</head>
<body>
  <canvas id="matrix"></canvas>
  <div class="container">
    <div class="card">
      <div class="visit-badge">WEBSITE VISIT: 1</div>
      <div class="brand-logo-wrap">
        <img src="/logo.png" alt="Logo" class="brand-logo" onerror="this.style.display='none'">
      </div>
      <div class="brand">{{ site_name }}</div>
      <div class="sub">$ SECURE TERMINAL V5.5</div>
      <div class="prompt">&gt; LOGIN_REQUIRED {{ site_name }} &#128150;<span class="cursor"></span></div>
      <audio id="loginSound" src="https://files.catbox.moe/0n33xi.mp3" preload="auto"></audio>
      <form method="POST" id="loginForm">
        <input type="text" name="username" id="username" placeholder="Username" autocomplete="username" value="rafin" style="display:none">
        <input type="text" name="username_visible" id="usernameVisible" placeholder="Username" autocomplete="username" oninput="document.getElementById('username').value=this.value">
        <div class="pw-wrap">
          <input type="password" name="password" placeholder="Password" id="pw" autocomplete="current-password">
          <button type="button" class="eye-btn" onclick="togglePw()">&#128065;</button>
        </div>
        <label class="remember-wrap">
          <input type="checkbox" id="rememberMe"> Remember Credentials
        </label>
        <button type="submit" class="auth-btn" onclick="playAndSubmit(event)">&gt;_&lt; LOGIN NOW</button>
      </form>
      <a href="#" class="like-bot">&#128077; LIKE BOT</a>
      <div class="footer">&#9888; UNAUTHORIZED POWER ACCESS PROHIBITED<br>{{ site_name }} SECURE SYSTEMS &#9888;</div>
    </div>
  </div>
  <script>
    window.addEventListener('load',()=>{
      const savedUser=localStorage.getItem('savedUsername');
      const savedPass=localStorage.getItem('savedPassword');
      if(savedUser&&savedPass){
        document.getElementById('username').value=savedUser;
        document.getElementById('usernameVisible').value=savedUser;
        document.getElementById('pw').value=savedPass;
        document.getElementById('rememberMe').checked=true;
      }
    });
    function playAndSubmit(event){
      event.preventDefault();
      const sound=document.getElementById('loginSound');
      const form=document.getElementById('loginForm');
      const remember=document.getElementById('rememberMe').checked;
      if(remember){
        localStorage.setItem('savedUsername',document.getElementById('usernameVisible').value||'rafin');
        localStorage.setItem('savedPassword',document.getElementById('pw').value);
      }else{
        localStorage.removeItem('savedUsername');
        localStorage.removeItem('savedPassword');
      }
      sound.play();
      setTimeout(()=>{form.submit();},500);
    }
    function togglePw(){const p=document.getElementById('pw');p.type=p.type==='password'?'text':'password';}
  </script>
  """ + MATRIX_SCRIPT + """
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ site_name }}— Dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root{ --ac:#00ff41; --ac-dim:#00ff4188; --ac-lite:#00ff4111; --ac-glow:#00ff4122; }
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#05000f;font-family:'JetBrains Mono',monospace;min-height:100vh;color:#e0e0e0;overflow-x:hidden;padding-bottom:100px}
    canvas{position:fixed;inset:0;z-index:1}
    .wrap{position:relative;z-index:10;max-width:700px;margin:0 auto;padding:16px}
    .topbar{
      background:rgba(10,0,16,0.9);border:1.5px solid var(--ac);border-radius:12px;
      padding:10px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;
      box-shadow:0 0 20px var(--ac-glow);backdrop-filter:blur(4px);margin-bottom:16px;
    }
    .topbar-left{display:flex;align-items:center;gap:10px}
    .topbar-icon{font-size:22px;text-shadow:0 0 10px var(--ac)}
    .topbar-title{color:var(--ac);font-size:16px;font-weight:700;letter-spacing:2px;text-shadow:0 0 10px var(--ac)}
    .topbar-stats{display:flex;gap:14px;align-items:center;font-size:11px;color:var(--ac-dim)}
    .topbar-stats span{color:var(--ac);font-weight:700}
    .topbar-user{color:#888;font-size:12px}
    .topbar-actions{display:flex;gap:8px;align-items:center}
    .top-btn{
      background:transparent;border:1.5px solid var(--ac);color:var(--ac);padding:6px 12px;border-radius:8px;
      font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;cursor:pointer;transition:all .3s;
      box-shadow:0 0 8px var(--ac-glow);text-decoration:none;display:inline-flex;align-items:center;gap:4px;
    }
    .top-btn:hover{background:var(--ac-lite);box-shadow:0 0 16px var(--ac-dim)}
    .clock-card{
      background:rgba(10,0,16,0.9);border:1.5px solid var(--ac);border-radius:14px;padding:24px;
      text-align:center;box-shadow:0 0 30px var(--ac-glow);margin-bottom:16px;backdrop-filter:blur(4px);
    }
    .clock-time{font-size:42px;font-weight:700;color:var(--ac);text-shadow:0 0 20px var(--ac);letter-spacing:2px}
    .clock-date{font-size:18px;color:var(--ac-dim);margin-top:4px;letter-spacing:3px}
    .clock-label{font-size:12px;color:var(--ac-dim);margin-top:8px;letter-spacing:4px;text-transform:uppercase}
    .stats-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}
    .stat-card{
      background:rgba(10,0,16,0.9);border:1.5px solid var(--ac);border-radius:14px;padding:20px;
      text-align:center;box-shadow:0 0 20px var(--ac-glow);backdrop-filter:blur(4px);transition:all .3s;
    }
    .stat-card:hover{transform:translateY(-4px);box-shadow:0 0 35px var(--ac-dim)}
    .stat-value{font-size:36px;font-weight:700;color:var(--ac);text-shadow:0 0 15px var(--ac)}
    .stat-label{font-size:11px;color:var(--ac-dim);letter-spacing:3px;margin-top:8px;text-transform:uppercase}
    .empty-state{text-align:center;padding:50px 20px;color:#444;}
    .empty-icon{font-size:60px;margin-bottom:16px;opacity:0.6}
    .empty-text{font-size:14px;color:#666;letter-spacing:2px;line-height:1.6}
    .volume-bar{
      position:fixed;bottom:70px;left:50%;transform:translateX(-50%);width:90%;max-width:500px;
      background:rgba(10,0,16,0.95);border:1.5px solid var(--ac);border-radius:12px;padding:12px 20px;
      display:flex;align-items:center;gap:12px;box-shadow:0 0 20px var(--ac-glow);z-index:20;
    }
    .volume-label{color:var(--ac);font-size:12px;letter-spacing:2px;white-space:nowrap}
    .volume-slider{flex:1;-webkit-appearance:none;height:6px;background:#222;border-radius:3px;outline:none}
    .volume-slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;background:var(--ac);border-radius:50%;cursor:pointer;box-shadow:0 0 10px var(--ac)}
    .volume-pct{color:var(--ac);font-size:12px;min-width:36px;text-align:right}
    .fab{
      position:fixed;bottom:20px;right:20px;width:56px;height:56px;background:var(--ac);border-radius:16px;
      display:flex;align-items:center;justify-content:center;font-size:28px;color:#000;cursor:pointer;
      box-shadow:0 0 25px var(--ac-dim);border:none;z-index:20;transition:all .3s;
    }
    .fab:hover{transform:scale(1.1);box-shadow:0 0 40px var(--ac)}
    .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);align-items:center;justify-content:center;z-index:1000}
    .modal.active{display:flex}
    .modal-content{
      background:rgba(10,0,16,0.95);border:2px solid var(--ac);border-radius:16px;padding:28px;
      width:90%;max-width:380px;box-shadow:0 0 50px var(--ac-glow);position:relative;
    }
    .modal-title{color:var(--ac);font-size:18px;font-weight:700;letter-spacing:3px;margin-bottom:20px;text-shadow:0 0 10px var(--ac)}
    .modal-label{color:var(--ac-dim);font-size:11px;letter-spacing:2px;margin-bottom:8px;text-transform:uppercase;display:block}
    .modal-input{
      width:100%;padding:12px 14px;background:rgba(13,0,13,0.6);border:2px solid var(--ac);border-radius:10px;
      color:var(--ac);font-family:'JetBrains Mono',monospace;font-size:14px;outline:none;margin-bottom:16px;
      box-shadow:inset 0 0 10px var(--ac-lite);
    }
    .modal-input:focus{box-shadow:0 0 15px var(--ac-dim), inset 0 0 10px var(--ac-lite)}
    .modal-select{
      width:100%;padding:12px 14px;background:rgba(13,0,13,0.6);border:2px solid var(--ac);border-radius:10px;
      color:var(--ac);font-family:'JetBrains Mono',monospace;font-size:14px;outline:none;margin-bottom:20px;
      box-shadow:inset 0 0 10px var(--ac-lite);
    }
    .modal-actions{display:flex;gap:12px}
    .modal-btn{flex:1;padding:12px;border-radius:10px;border:2px solid;font-family:'JetBrains Mono',monospace;font-weight:700;cursor:pointer;transition:all .3s;font-size:13px;letter-spacing:1px}
    .btn-create{background:var(--ac);border-color:var(--ac);color:#000;box-shadow:0 0 15px var(--ac-glow)}
    .btn-create:hover{box-shadow:0 0 30px var(--ac-dim)}
    .btn-cancel{background:transparent;border-color:#444;color:#666}
    .btn-cancel:hover{border-color:#777;color:#999}
    .server-card{
      background:rgba(10,0,16,0.9);border:1.5px solid var(--ac);border-radius:12px;padding:16px;
      box-shadow:0 0 15px var(--ac-glow);margin-bottom:14px;backdrop-filter:blur(4px);
    }
    .server-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
    .server-name{font-size:15px;font-weight:700;color:#fff;letter-spacing:1px}
    .server-status{padding:4px 12px;border-radius:20px;font-size:10px;font-weight:700;border:1px solid;letter-spacing:1px}
    .server-status.running{background:rgba(0,255,65,0.15);color:#00ff41;border-color:#00ff41}
    .server-status.stopped{background:rgba(255,0,0,0.15);color:#ff4444;border-color:#ff4444}
    .server-info{color:#888;font-size:11px;margin-bottom:12px;letter-spacing:1px}
    .server-actions{display:flex;gap:10px}
    .server-actions a, .server-actions button{
      flex:1;text-align:center;padding:8px;border-radius:8px;text-decoration:none;font-size:11px;
      font-weight:700;cursor:pointer;border:1.5px solid;font-family:'JetBrains Mono',monospace;transition:all .3s;
    }
    .btn-manage{background:transparent;color:var(--ac);border-color:var(--ac)}
    .btn-manage:hover{background:var(--ac-lite);box-shadow:0 0 12px var(--ac-dim)}
    .btn-delete{background:transparent;color:#ff4444;border-color:#ff4444}
    .btn-delete:hover{background:rgba(255,0,0,0.1);box-shadow:0 0 12px rgba(255,0,0,0.3)}
  </style>
</head>
<body>
  <canvas id="matrix"></canvas>
  <div class="wrap">
    <div class="topbar">
      <div class="topbar-left">
        <span class="topbar-icon">&#128421;</span>
        <span class="topbar-title">{{ site_name }}</span>
      </div>
      <div class="topbar-stats">
        <div>CPU: <span id="cpuVal">--%</span></div>
        <div>RAM: <span id="ramVal">--%</span></div>
        <div>DISK: <span id="diskVal">--%</span></div>
      </div>
      <div class="topbar-user">@{{ username }}</div>
      <div class="topbar-actions">
        <button class="top-btn" onclick="toggleMusic()">&#9881; LIKE</button>
        <a href="{{ url_for('logout') }}" class="top-btn">LOGOUT</a>
      </div>
    </div>
    <div class="clock-card">
      <div class="clock-time" id="clockTime">--:--:-- --</div>
      <div class="clock-date" id="clockDate">--/--/----</div>
      <div class="clock-label">TERMINAL TIME SYSTEM</div>
    </div>
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-value" id="runningVal">{{ running }}</div>
        <div class="stat-label">RUNNING</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" id="totalVal">{{ total }}</div>
        <div class="stat-label">TOTAL</div>
      </div>
    </div>
    {% if servers %}
      {% for name, cfg in servers.items() %}
      <div class="server-card">
        <div class="server-top">
          <span class="server-name">{{ name }}</span>
          <span class="server-status {{ cfg.status }}">{{ cfg.status.upper() }}</span>
        </div>
        <div class="server-info">{{ cfg.runtime }} | port {{ cfg.port }} | {{ cfg.created[:10] }}</div>
        <div class="server-actions">
          <a href="{{ url_for('server_detail', name=name) }}" class="btn-manage">MANAGE</a>
          <form method="POST" action="{{ url_for('delete_server', name=name) }}" style="flex:1" onsubmit="return confirm('Delete?')">
            <button type="submit" class="btn-delete">DELETE</button>
          </form>
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty-state">
        <div class="empty-icon">&#128194;</div>
        <div class="empty-text">NO PROJECTS YET...!!<br>CLICK + TO CREATE ONE...!!</div>
      </div>
    {% endif %}
  </div>
  <div class="volume-bar">
    <span class="volume-label">&#128266; VOLUME</span>
    <input type="range" class="volume-slider" min="0" max="100" value="50" id="volSlider" oninput="setVolume(this.value/100); document.getElementById('volPct').innerText=this.value+'%'">
    <span class="volume-pct" id="volPct">50%</span>
  </div>
  <button class="fab" onclick="openModal()">+</button>
  <div class="modal" id="createModal">
    <div class="modal-content">
      <div class="modal-title">NEW PROJECT</div>
      <form method="POST" action="{{ url_for('create_server') }}">
        <label class="modal-label">PROJECT NAME</label>
        <input type="text" name="name" class="modal-input" placeholder="my-bot" required>
        <label class="modal-label">RUNTIME</label>
        <select name="runtime" class="modal-select">
          <option value="python">Python</option>
          <option value="node">Node.js</option>
          <option value="static">Static HTML</option>
        </select>
        <div class="modal-actions">
          <button type="submit" class="modal-btn btn-create">CREATE</button>
          <button type="button" class="modal-btn btn-cancel" onclick="closeModal()">CANCEL</button>
        </div>
      </form>
    </div>
  </div>
  <script>
    function openModal(){document.getElementById('createModal').classList.add('active')}
    function closeModal(){document.getElementById('createModal').classList.remove('active')}
    function updateClock(){
      const now=new Date();
      const timeStr=now.toLocaleTimeString('en-US',{hour12:true,hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const dateStr=now.toLocaleDateString('en-GB',{day:'2-digit',month:'2-digit',year:'numeric'}).replace(/\\//g,'-');
      document.getElementById('clockTime').textContent=timeStr;
      document.getElementById('clockDate').textContent=dateStr;
    }
    updateClock();
    setInterval(updateClock,1000);
    async function updateStats(){
      try{
        const res=await fetch('{{ url_for("system_stats") }}');
        const data=await res.json();
        document.getElementById('cpuVal').textContent=data.cpu+'%';
        document.getElementById('ramVal').textContent=data.ram+'%';
        document.getElementById('diskVal').textContent=data.disk+'%';
      }catch(e){}
    }
    updateStats();
    setInterval(updateStats,3000);
    const volSlider=document.getElementById('volSlider');
    const savedVol=localStorage.getItem('musicVolume');
    if(savedVol!==null){volSlider.value=Math.round(savedVol*100);document.getElementById('volPct').innerText=Math.round(savedVol*100)+'%';}
  </script>
  """ + MATRIX_SCRIPT + AUDIO_PLAYER + """
</body>
</html>
"""

SERVER_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ server_name }} | {{ site_name }}</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root{ --ac:#a855f7; --ac-dim:#a855f788; --ac-lite:#a855f711; --ac-glow:#a855f722; --red:#ff4444; --green:#00ff41; }
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#05000f;font-family:'JetBrains Mono',monospace;min-height:100vh;color:#e0e0e0;overflow-x:hidden;padding-bottom:30px}
    canvas{position:fixed;inset:0;z-index:1}
    .wrap{position:relative;z-index:10;max-width:800px;margin:0 auto;padding:16px}
    .topbar{
      background:rgba(10,0,16,0.9);border-bottom:1.5px solid var(--ac);padding:14px 16px;
      display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
      box-shadow:0 0 20px var(--ac-glow);margin-bottom:20px;border-radius:0 0 12px 12px;
    }
    .topbar-left{display:flex;align-items:center;gap:12px}
    .back-btn{
      width:36px;height:36px;background:rgba(255,255,255,0.05);border:1.5px solid #333;border-radius:8px;
      color:#fff;display:flex;align-items:center;justify-content:center;text-decoration:none;font-size:18px;transition:all .3s;
    }
    .back-btn:hover{border-color:var(--ac);color:var(--ac)}
    .server-title{font-size:18px;font-weight:700;color:#fff;letter-spacing:1px}
    .server-sub{color:#888;font-size:11px;letter-spacing:1px}
    .status-badge{
      padding:5px 14px;border-radius:8px;font-size:11px;font-weight:700;letter-spacing:1px;border:1.5px solid;
    }
    .status-badge.running{background:rgba(0,255,65,0.15);color:var(--green);border-color:var(--green);box-shadow:0 0 10px rgba(0,255,65,0.2)}
    .status-badge.stopped{background:rgba(255,0,0,0.15);color:var(--red);border-color:var(--red);box-shadow:0 0 10px rgba(255,0,0,0.2)}
    .start-btn{
      background:transparent;border:1.5px solid var(--ac);color:var(--ac);padding:8px 18px;border-radius:8px;
      font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:1px;
      box-shadow:0 0 12px var(--ac-glow);transition:all .3s;display:inline-flex;align-items:center;gap:6px;
    }
    .start-btn:hover{background:var(--ac-lite);box-shadow:0 0 24px var(--ac-dim)}
    .tabs{
      display:flex;background:rgba(10,0,16,0.8);border:1.5px solid #222;border-radius:10px;padding:4px;margin-bottom:20px;gap:4px;
    }
    .tab{
      flex:1;padding:10px;text-align:center;border-radius:8px;font-size:11px;font-weight:700;letter-spacing:2px;
      color:#666;cursor:pointer;transition:all .3s;border:none;background:transparent;font-family:'JetBrains Mono',monospace;
    }
    .tab.active{background:rgba(168,85,247,0.15);color:var(--ac);box-shadow:0 0 12px var(--ac-glow)}
    .tab:hover:not(.active){color:#999}
    .panel{
      background:rgba(10,0,16,0.9);border:1.5px solid #222;border-radius:12px;padding:20px;
      box-shadow:0 0 20px rgba(0,0,0,0.5);backdrop-filter:blur(4px);display:none;
    }
    .panel.active{display:block}
    .panel-title{color:#fff;font-size:13px;margin-bottom:16px;letter-spacing:2px;text-transform:uppercase}
    .upload-area{
      border:2px dashed #444;border-radius:12px;padding:40px 20px;text-align:center;cursor:pointer;
      transition:all .3s;color:#666;margin-bottom:20px;
    }
    .upload-area:hover{border-color:var(--ac);background:rgba(168,85,247,0.05);color:var(--ac)}
    .upload-icon{font-size:48px;margin-bottom:12px;opacity:0.7}
    .upload-text{font-size:14px;letter-spacing:1px;margin-bottom:6px}
    .upload-sub{font-size:11px;opacity:0.7}
    .upload-area input{display:none}
    .file-empty{text-align:center;padding:30px;color:#444;font-size:13px;letter-spacing:1px}
    .log-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
    .log-title{color:#fff;font-size:13px;letter-spacing:2px}
    .log-title span{color:var(--ac-dim);font-size:11px}
    .log-actions{display:flex;gap:8px}
    .log-btn{
      padding:6px 14px;border-radius:8px;border:1.5px solid;font-family:'JetBrains Mono',monospace;
      font-size:11px;font-weight:700;cursor:pointer;transition:all .3s;background:transparent;
    }
    .log-btn.clear{color:var(--red);border-color:var(--red)}
    .log-btn.clear:hover{background:rgba(255,0,0,0.1)}
    .log-btn.auto{color:var(--ac);border-color:var(--ac)}
    .log-btn.auto:hover{background:var(--ac-lite)}
    .log-btn.refresh{color:#888;border-color:#444}
    .log-btn.refresh:hover{border-color:#666;color:#bbb}
    .log-box{
      background:rgba(5,0,5,0.95);border:1.5px solid #222;border-radius:10px;padding:16px;
      min-height:300px;font-family:'Courier New',monospace;font-size:12px;line-height:1.6;color:#aaa;white-space:pre-wrap;
    }
    .form-group{margin-bottom:14px}
    .form-label{display:block;color:var(--ac-dim);font-size:11px;letter-spacing:2px;margin-bottom:6px;text-transform:uppercase}
    .form-input{
      width:100%;padding:10px 12px;background:rgba(13,0,13,0.6);border:1.5px solid #333;border-radius:8px;
      color:#fff;font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;transition:all .3s;
    }
    .form-input:focus{border-color:var(--ac);box-shadow:0 0 10px var(--ac-glow)}
    .save-btn{
      width:100%;padding:12px;background:var(--ac);border:none;border-radius:8px;color:#000;
      font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:2px;
      box-shadow:0 0 15px var(--ac-glow);transition:all .3s;
    }
    .save-btn:hover{box-shadow:0 0 30px var(--ac-dim)}
    .msg{
      padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:12px;display:none;
    }
    .msg.success{background:rgba(0,255,65,0.1);color:var(--green);border:1px solid rgba(0,255,65,0.3)}
    .msg.error{background:rgba(255,0,0,0.1);color:var(--red);border:1px solid rgba(255,0,0,0.3)}
  </style>
</head>
<body>
  <canvas id="matrix"></canvas>
  <div class="wrap">
    <div class="topbar">
      <div class="topbar-left">
        <a href="{{ url_for('dashboard') }}" class="back-btn">&#8592;</a>
        <div>
          <div class="server-title">{{ server_name }}</div>
          <div class="server-sub">{{ config.runtime }} no main file</div>
        </div>
      </div>
      <span class="status-badge {{ config.status }}">{{ config.status.upper() }}</span>
      <button class="start-btn" onclick="startServer()">&#9654; Start</button>
    </div>
    <div class="tabs">
      <button class="tab active" onclick="switchTab('files')">FILES</button>
      <button class="tab" onclick="switchTab('packages')">PACKAGES</button>
      <button class="tab" onclick="switchTab('settings')">SETTINGS</button>
      <button class="tab" onclick="switchTab('logs')">LOGS</button>
    </div>
    <div class="panel active" id="tab-files">
      <div class="upload-area" onclick="document.getElementById('fileInput').click()">
        <div class="upload-icon">&#128194;</div>
        <div class="upload-text">Click or drag to upload ZIP / .py / .js</div>
        <div class="upload-sub">Supports: .zip, .py, .js, .ts</div>
        <input type="file" id="fileInput" onchange="uploadFile(this)">
      </div>
      <div class="msg" id="uploadMsg"></div>
      {% if files %}
        <div style="max-height:300px;overflow-y:auto">
        {% for f in files %}
          <div style="display:flex;justify-content:space-between;padding:8px 10px;border-radius:6px;font-size:12px;color:#ccc">
            <span>{{ f.name }}</span>
            <span style="color:#444">{{ f.size }}B</span>
          </div>
        {% endfor %}
        </div>
      {% else %}
        <div class="file-empty">No files uploaded yet</div>
      {% endif %}
    </div>
    <div class="panel" id="tab-packages">
      <div class="panel-title">Packages</div>
      <div class="file-empty">Package manager coming soon...</div>
    </div>
    <div class="panel" id="tab-settings">
      <div class="panel-title">Settings</div>
      <div class="form-group">
        <label class="form-label">Main File</label>
        <input type="text" id="mainFile" class="form-input" value="{{ config.main_file }}" placeholder="e.g. main.py">
      </div>
      <div class="form-group">
        <label class="form-label">Custom Command</label>
        <input type="text" id="mainCmd" class="form-input" value="{{ config.main_command }}" placeholder="e.g. python app.py">
      </div>
      <div class="form-group">
        <label class="form-label">Port</label>
        <input type="number" id="port" class="form-input" value="{{ config.port }}" placeholder="8080">
      </div>
      <button class="save-btn" onclick="saveSettings()">SAVE SETTINGS</button>
    </div>
    <div class="panel" id="tab-logs">
      <div class="log-header">
        <div class="log-title">OUTPUT LOGS <span>&#183; Live</span></div>
        <div class="log-actions">
          <button class="log-btn clear" onclick="clearLogs()">&#128465; Clear</button>
          <button class="log-btn auto" id="autoBtn" onclick="toggleAuto()">&#9208; Auto</button>
          <button class="log-btn refresh" onclick="refreshLogs()">&#8635; Refresh</button>
        </div>
      </div>
      <div class="log-box" id="logBox">No logs yet. Start the server to see output.</div>
    </div>
  </div>
  <script>
    const serverName = "{{ server_name }}";
    let autoRefresh = null;
    function switchTab(tab){
      document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('tab-'+tab).classList.add('active');
    }
    function showMsg(el,text,isError){
      el.textContent=text;el.className='msg '+(isError?'error':'success');el.style.display='block';
      setTimeout(()=>el.style.display='none',4000);
    }
    async function startServer(){
      const res=await fetch(`/server/${serverName}/start`,{method:'POST'});
      const data=await res.json();
      alert(data.success?'Server started!':'Error: '+data.error);
      if(data.success) location.reload();
    }
    async function stopServer(){
      const res=await fetch(`/server/${serverName}/stop`,{method:'POST'});
      const data=await res.json();
      alert(data.success?'Server stopped!':'Error stopping server');
      if(data.success) location.reload();
    }
    async function saveSettings(){
      const payload={main_file:document.getElementById('mainFile').value,main_command:document.getElementById('mainCmd').value,port:parseInt(document.getElementById('port').value)};
      const res=await fetch(`/server/${serverName}/settings`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await res.json();
      alert(data.success?'Settings saved!':'Error saving settings');
    }
    async function uploadFile(input){
      if(!input.files.length) return;
      const form=new FormData();form.append('file',input.files[0]);
      const res=await fetch(`/server/${serverName}/upload`,{method:'POST',body:form});
      const data=await res.json();
      const msg=document.getElementById('uploadMsg');
      showMsg(msg,data.success?`Uploaded ${data.count} file(s)`:data.error,!data.success);
      if(data.success) setTimeout(()=>location.reload(),1000);
    }
    async function refreshLogs(){
      const res=await fetch(`/server/${serverName}/logs`);
      const data=await res.json();
      document.getElementById('logBox').textContent=data.logs;
    }
    async function clearLogs(){
      await fetch(`/server/${serverName}/logs/clear`,{method:'POST'});
      refreshLogs();
    }
    function toggleAuto(){
      const btn=document.getElementById('autoBtn');
      if(autoRefresh){clearInterval(autoRefresh);autoRefresh=null;btn.style.background='transparent';btn.innerHTML='&#9208; Auto';}
      else{autoRefresh=setInterval(refreshLogs,2000);btn.style.background='var(--ac-lite)';btn.innerHTML='&#9209; Auto';}
    }
    refreshLogs();
  </script>
  """ + MATRIX_SCRIPT + AUDIO_PLAYER + """
</body>
</html>
"""

MAINTENANCE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ site_name }}— Maintenance</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root{--ac:#fbbf24;--ac-dim:#fbbf2488;--ac-lite:#fbbf2411;}
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#05000f;font-family:'JetBrains Mono',monospace;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;overflow:hidden}
    canvas{position:fixed;inset:0;z-index:1}
    .box{position:relative;z-index:10;padding:40px;background:rgba(10,0,16,0.9);border:2px solid var(--ac);border-radius:16px;box-shadow:0 0 50px var(--ac-lite);backdrop-filter:blur(4px);max-width:400px;width:90%}
    .box h1{color:var(--ac);font-size:28px;margin-bottom:20px;text-shadow:0 0 15px var(--ac);letter-spacing:3px}
    .box p{color:#888;font-size:14px;line-height:1.6}
    .box .footer{margin-top:30px;color:#333;font-size:11px;letter-spacing:2px}
    .box .footer span{color:var(--ac)}
  </style>
</head>
<body>
  <canvas id="matrix"></canvas>
  <div class="box">
    <h1>{{ site_name }}</h1>
    <p>{{ message }}</p>
    <div class="footer"><span>{{ owner_name }}</span> Server Management System</div>
  </div>
  """ + MATRIX_SCRIPT + AUDIO_PLAYER + """
</body>
</html>
"""

@app.route("/logo.png")
def serve_logo():
    # Fixed logo path issue
    logo_path = BASE_DIR / "logo.png"
    if logo_path.exists():
        return send_file(logo_path, mimetype="image/png")
    # Try alternative path
    alt_path = Path("/mnt/agents/output/logo.png")
    if alt_path.exists():
        return send_file(alt_path, mimetype="image/png")
    return "", 404

@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        data = load_data()
        username = "rakib"
        user = data["users"].get(username)
        if not user:
            data["users"][username] = {
                "joined": datetime.now().isoformat(),
                "password_hash": hash_password("RAKIB123")
            }
            save_data(data)
        session["username"] = username
        return redirect(url_for("dashboard"))
    data = load_data()
    settings = data.get("settings", {})
    return render_template_string(LOGIN_TEMPLATE,
        error=None,
        theme_color=get_theme_color(),
        site_name=settings.get("site_name", SITE_NAME),
        owner_name=OWNER_NAME)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    data = load_data()
    settings = data.get("settings", {})
    site_name = settings.get("site_name", SITE_NAME)
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    changed = False
    for name, cfg in user_servers.items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            data["servers"][name] = cfg
            changed = True
            # Clean up from RUNNING_PROCESSES
            if name in RUNNING_PROCESSES:
                try:
                    RUNNING_PROCESSES[name]["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[name]
    if changed:
        save_data(data)
    running = sum(1 for v in user_servers.values() if v.get("status") == "running")
    return render_template_string(DASHBOARD_TEMPLATE,
        servers=user_servers,
        running=running,
        total=len(user_servers),
        username=username,
        site_name=site_name,
        theme_color=get_theme_color(),
        owner_name=OWNER_NAME)

@app.route("/api/stats")
@login_required
def system_stats():
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    return jsonify({"cpu": cpu, "ram": ram, "disk": disk})

@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    cfg = {
        "name": name,
        "owner": session["username"],
        "runtime": runtime,
        "status": "stopped",
        "main_file": "",
        "main_command": "",
        "port": 8080,
        "pid": None,
        "created": datetime.now().isoformat()
    }
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))

@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if cfg and cfg.get("owner") == session["username"]:
        pid = cfg.get("pid")
        if pid:
            kill_process(pid)
        if name in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[name]["proc"].terminate()
                RUNNING_PROCESSES[name]["log_file"].close()
            except Exception:
                pass
            del RUNNING_PROCESSES[name]
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))

@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return "Server not found", 404
    if cfg.get("owner") != session["username"]:
        return "Access denied", 403
    pid = cfg.get("pid")
    if pid and not is_process_alive(pid):
        cfg["status"] = "stopped"
        cfg["pid"] = None
        data["servers"][name] = cfg
        save_data(data)
        # Clean up from RUNNING_PROCESSES
        if name in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[name]["log_file"].close()
            except Exception:
                pass
            del RUNNING_PROCESSES[name]
    if "main_command" not in cfg:
        cfg["main_command"] = ""
    extract_dir = SERVERS_DIR / name / "extracted"
    files = list_files(extract_dir)
    return render_template_string(SERVER_TEMPLATE,
        server_name=name,
        config=cfg,
        files=files,
        theme_color=get_theme_color(),
        site_name=data.get("settings", {}).get("site_name", SITE_NAME),
        owner_name=OWNER_NAME)

def list_files(directory, base="", max_depth=5):
    """Fixed recursive file listing with depth limit and safe traversal"""
    if max_depth <= 0:
        return []
    
    result = []
    if not directory.exists() or not directory.is_dir():
        return result
    
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            # Skip hidden files and system files
            if entry.name.startswith('.'):
                continue
                
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                # Recursively list subdirectories with depth limit
                result.extend(list_files(entry, rel, max_depth - 1))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except (PermissionError, OSError):
        pass
    return result

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "error": "Empty filename"}), 400
    
    # Security: Sanitize filename
    filename = Path(f.filename).name
    if not filename or filename.startswith('.') or '..' in filename:
        return jsonify({"success": False, "error": "Invalid filename"}), 400
    
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    upload_path = SERVERS_DIR / name / f"upload_{filename}"
    
    try:
        f.save(upload_path)
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to save file: {str(e)}"}), 500
    
    extracted_files = []
    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                # Security: Check for path traversal in zip
                for member in z.infolist():
                    if member.filename.startswith(("/", "\\", "..", "../")):
                        upload_path.unlink(missing_ok=True)
                        return jsonify({"success": False, "error": "Invalid zip path (path traversal)"})
                z.extractall(extract_dir)
                for member in z.infolist():
                    if not member.is_dir():
                        extracted_files.append(member.filename)
            upload_path.unlink(missing_ok=True)
        except zipfile.BadZipFile:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": "Invalid ZIP file"}), 400
        except Exception as e:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": f"Zip extraction failed: {str(e)}"}), 500
    else:
        # Security: Check file extension
        allowed_extensions = {'.py', '.js', '.ts', '.html', '.css', '.txt', '.json', '.yml', '.yaml', '.md', '.ini', '.cfg', '.conf', '.sh', '.bat', '.ps1', '.rb', '.php', '.go', '.rs', '.c', '.cpp', '.java', '.kt', '.swift'}
        if Path(filename).suffix.lower() not in allowed_extensions:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": "File type not allowed"}), 400
            
        dest = extract_dir / filename
        # Check if destination is within extract_dir (security)
        try:
            dest.relative_to(extract_dir)
        except ValueError:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": "Invalid destination path"}), 400
            
        shutil.move(str(upload_path), str(dest))
        extracted_files = [filename]
        if not cfg.get("main_file") and filename.endswith((".py", ".js", ".ts")):
            cfg["main_file"] = filename
            data["servers"][name] = cfg
            save_data(data)
    return jsonify({"success": True, "files": extracted_files, "count": len(extracted_files)})

@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    payload = request.get_json()
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    cfg["main_command"] = payload.get("main_command", cfg.get("main_command", ""))
    cfg["port"] = payload.get("port", cfg.get("port", 8080))
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    pid = cfg.get("pid")
    if pid and is_process_alive(pid):
        return jsonify({"success": False, "error": "Already running"})
    main_file = cfg.get("main_file") or "main.py"
    main_cmd = cfg.get("main_command") or ""
    extract_dir = SERVERS_DIR / name / "extracted"
    main_path = extract_dir / main_file
    if not main_path.exists():
        return jsonify({"success": False, "error": f"{main_file} not found. Upload your files first."})
    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if main_cmd:
        cmd = main_cmd.split()
    else:
        cmd = get_run_command(cfg.get("runtime", "python"), main_file)
    env = os.environ.copy()
    env["PORT"] = str(cfg.get("port", 8080))
    try:
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] Starting: {' '.join(cmd)}\n{'='*50}\n")
        log_file = open(log_path, "a")
        
        # Cross-platform process creation
        if platform.system() == "Windows":
            proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env)
        else:
            proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
            
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False}), 403
    pid = cfg.get("pid")
    stopped = False
    
    if name in RUNNING_PROCESSES:
        entry = RUNNING_PROCESSES[name]
        proc = entry["proc"]
        
        try:
            # Cross-platform process termination
            if platform.system() == "Windows":
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
                
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
                
        try:
            entry["log_file"].close()
        except Exception:
            pass
            
        del RUNNING_PROCESSES[name]
        stopped = True
        
    if pid and not stopped:
        kill_process(pid)
        
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        with open(log_path, "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] Server stopped\n")
    except Exception:
        pass
        
    cfg["status"] = "stopped"
    cfg["pid"] = None
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"logs": "Server not found"})
    log_path = SERVERS_DIR / name / "logs.txt"
    if not log_path.exists():
        return jsonify({"logs": "No logs yet. Start the server to see output."})
    try:
        if log_path.stat().st_size > 1024 * 1024:
            with open(log_path, 'r', errors='replace') as f:
                f.seek(-50000, 2)
                content = f.read()
            content = "... (showing last 50KB) ...\n" + content
        else:
            content = log_path.read_text(errors="replace")
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
            content = "... (showing last 200 lines) ...\n" + "\n".join(lines)
        return jsonify({"logs": content or "No output yet."})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})

@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
def clear_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False})
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        log_path.write_text("")
    except Exception:
        pass
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)