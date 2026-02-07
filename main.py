import os
import json
import asyncio
import hashlib
import logging
import socket
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn
import threading

# --- КОНФИГУРАЦИЯ И ЛОГИРОВАНИЕ ---
DATA_DIR = "data"
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_HASH", "8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
PREFERRED_PORTS = [55000, 55001, 55002]
FRONTEND_PORT = 55005

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("voting_api")

app = FastAPI(title="City Award Voting API", description="Система народного голосования для городских премий")

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- МОДЕЛИ ДАННЫХ ---
class FooterLink(BaseModel):
    label: str
    url: str

class FooterSettings(BaseModel):
    logo_url: Optional[str] = ""
    description: Optional[str] = "Официальная платформа голосования."
    copyright: Optional[str] = "© 2025 City Voting Platform"
    links: List[FooterLink] = []

class HeaderSettings(BaseModel):
    show_logo: bool = True
    logo_path: str = "win.png"

class Settings(BaseModel):
    title: str = "Городская Премия 2025"
    is_voting_active: bool = False
    anti_abuse_enabled: bool = True
    header: HeaderSettings = Field(default_factory=HeaderSettings)
    footer: FooterSettings = Field(default_factory=FooterSettings)

class Participant(BaseModel):
    id: str
    name: str
    description: Optional[str] = ""
    image_url: Optional[str] = "https://placehold.co/400x600/1a1a1a/gold?text=No+Photo"

class Category(BaseModel):
    id: str
    title: str
    max_votes: int = 2
    participants: List[Participant] = []

class VoteRequest(BaseModel):
    category_id: str
    participant_id: str

class AdminLogin(BaseModel):
    password: str

# --- JSON DB ---
class JsonDB:
    """Простая файловая база данных на основе JSON."""
    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.files = {
            "settings": os.path.join(directory, "settings.json"),
            "categories": os.path.join(directory, "categories.json"),
            "votes": os.path.join(directory, "votes.json")
        }
        self._init_db()

    def _init_db(self):
        if not os.path.exists(self.files["settings"]):
            self.save("settings", Settings().dict())
        if not os.path.exists(self.files["categories"]):
            self.save("categories", [])
        if not os.path.exists(self.files["votes"]):
            self.save("votes", [])

    def load(self, table: str) -> Any:
        try:
            with open(self.files[table], "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Error loading {table}: {e}")
            return [] if table != "settings" else Settings().dict()

    def save(self, table: str, data: Any):
        try:
            with open(self.files[table], "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error saving {table}: {e}")
            raise HTTPException(status_code=500, detail="Ошибка сохранения данных")

db = JsonDB(DATA_DIR)

# --- ВСПОМОГАТЕЛЬНЫЕ ---
def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host

async def verify_admin(x_admin_token: str = Header(None)):
    if x_admin_token != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Доступ запрещен: неверный токен")

# --- ПУБЛИЧНЫЕ ЭНДПОИНТЫ ---
@app.get("/api/status", response_model=Settings)
async def get_status():
    return db.load("settings")

@app.get("/api/categories", response_model=List[Category])
async def get_categories():
    return db.load("categories")

@app.get("/api/my-votes")
async def get_my_votes(request: Request):
    ip = get_client_ip(request)
    votes = db.load("votes")
    my_votes = {}
    for v in votes:
        if v.get('ip_address') == ip:
            cat_id = v['category_id']
            if cat_id not in my_votes:
                my_votes[cat_id] = []
            my_votes[cat_id].append(v['participant_id'])
    return my_votes

@app.get("/api/results")
async def get_results():
    settings = db.load("settings")
    categories = db.load("categories")
    votes = db.load("votes")
    results_data = {}
    for cat in categories:
        results_data[cat['id']] = {p['id']: 0 for p in cat['participants']}
    for v in votes:
        c_id = v['category_id']
        p_id = v['participant_id']
        if c_id in results_data and p_id in results_data[c_id]:
            results_data[c_id][p_id] += 1
    if settings.get('is_voting_active'):
        return {"visible": False, "message": "Результаты будут доступны после завершения голосования"}
    return {"visible": True, "data": results_data}

@app.post("/api/vote")
async def cast_vote(vote_req: VoteRequest, request: Request):
    settings = db.load("settings")
    if not settings.get('is_voting_active'):
        raise HTTPException(status_code=400, detail="Голосование в данный момент закрыто")
    ip = get_client_ip(request)
    votes = db.load("votes")
    if settings.get('anti_abuse_enabled'):
        user_cat_votes = [v for v in votes if v['ip_address'] == ip and v['category_id'] == vote_req.category_id]
        categories = db.load("categories")
        category = next((c for c in categories if c['id'] == vote_req.category_id), None)
        if not category:
            raise HTTPException(status_code=404, detail="Категория не найдена")
        if len(user_cat_votes) >= category.get('max_votes', 1):
            raise HTTPException(status_code=403, detail="Вы уже исчерпали лимит голосов в этой категории")
    new_vote = {
        "category_id": vote_req.category_id,
        "participant_id": vote_req.participant_id,
        "ip_address": ip,
        "timestamp": datetime.utcnow().isoformat()
    }
    votes.append(new_vote)
    db.save("votes", votes)
    return {"status": "success", "message": "Ваш голос учтен"}

@app.post("/api/vote/reset")
async def reset_my_vote(request: Request, payload: Dict[str, str]):
    settings = db.load("settings")
    if not settings.get('is_voting_active'):
        raise HTTPException(status_code=400, detail="Голосование закрыто")
    category_id = payload.get("category_id")
    if not category_id:
        raise HTTPException(status_code=400, detail="category_id обязателен")
    ip = get_client_ip(request)
    votes = db.load("votes")
    initial_count = len(votes)
    new_votes = [v for v in votes if not (v['ip_address'] == ip and v['category_id'] == category_id)]
    if len(new_votes) == initial_count:
        return {"status": "skipped", "message": "Голосов для удаления не найдено"}
    db.save("votes", new_votes)
    return {"status": "success", "message": "Голоса сброшены"}

# --- АДМИН ---
@app.post("/api/admin/login")
async def admin_login(login: AdminLogin):
    if hashlib.sha256(login.password.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
        logger.info("Admin logged in successfully")
        return {"token": SECRET_KEY}
    raise HTTPException(status_code=401, detail="Неверный пароль администратора")

@app.get("/api/admin/data", dependencies=[Depends(verify_admin)])
async def get_admin_data():
    return {
        "settings": db.load("settings"),
        "categories": db.load("categories"),
        "votes": db.load("votes")
    }

@app.post("/api/admin/settings", dependencies=[Depends(verify_admin)])
async def update_settings(settings: Settings):
    db.save("settings", settings.dict())
    return {"status": "updated", "data": settings}

@app.post("/api/admin/categories", dependencies=[Depends(verify_admin)])
async def update_categories(categories: List[Category]):
    db.save("categories", [c.dict() for c in categories])
    return {"status": "updated"}

# --- FRONTEND SERVER ---
frontend_app = FastAPI(title="Frontend Server")
frontend_app.mount("/", StaticFiles(directory=os.getcwd(), html=True), name="frontend")

def run_frontend():
    logger.info(f"Запуск FRONTEND сервера на порту {FRONTEND_PORT}")
    uvicorn.run(frontend_app, host="0.0.0.0", port=FRONTEND_PORT)

# --- ЗАПУСК ---
def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

if __name__ == "__main__":
    threading.Thread(target=run_frontend, daemon=True).start()
    port_to_use = PREFERRED_PORTS[0]
    for p in PREFERRED_PORTS:
        if not is_port_in_use(p):
            port_to_use = p
            break
        else:
            logger.warning(f"Порт {p} занят, проверяю следующий...")
    logger.info(f"Запуск API сервера на порту {port_to_use}")
    uvicorn.run(app, host="0.0.0.0", port=port_to_use)
