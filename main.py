import os
import json
import httpx
import hashlib
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

YANDEX_API_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_URL       = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
MODEL_URI        = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest"
DATABASE_URL     = os.getenv("DATABASE_URL")

app = FastAPI(title="SEO Generator API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── Database ──────────────────────────────────────────────────────────────────
import asyncpg

db_pool = None

async def get_db():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return db_pool

async def init_db():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token VARCHAR(255) PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id SERIAL PRIMARY KEY,
                code VARCHAR(32) UNIQUE NOT NULL,
                created_by INTEGER REFERENCES users(id),
                used_by INTEGER REFERENCES users(id) DEFAULT NULL,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        existing = await conn.fetchrow("SELECT id FROM users WHERE username = 'admin'")
        if not existing:
            pwd_hash = hashlib.sha256("admin123".encode()).hexdigest()
            await conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES ($1, $2, $3)",
                "admin", pwd_hash, True
            )
            print("✓ Admin created: admin / admin123")

@app.on_event("startup")
async def startup():
    await init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────
def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()

async def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    expires = datetime.utcnow() + timedelta(days=30)
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES ($1, $2, $3)",
            token, user_id, expires
        )
    return token

async def get_current_user(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT u.id, u.username, u.is_admin
            FROM sessions s JOIN users u ON s.user_id = u.id
            WHERE s.token = $1 AND s.expires_at > NOW()
        """, token)
    if not row:
        raise HTTPException(status_code=401, detail="Сессия истекла")
    return dict(row)

async def require_admin(request: Request):
    user = await get_current_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.post("/auth/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username","").strip()
    password = data.get("password","")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Укажите логин и пароль")
    pool = await get_db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE username=$1 AND password_hash=$2",
            username, hash_password(password)
        )
    if not user:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = await create_session(user["id"])
    return {"token": token, "username": user["username"], "is_admin": user["is_admin"]}

@app.post("/auth/logout")
async def logout(request: Request):
    token = request.headers.get("Authorization","").replace("Bearer ","")
    if token:
        pool = await get_db()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM sessions WHERE token=$1", token)
    return {"ok": True}

@app.get("/auth/me")
async def me(request: Request):
    return await get_current_user(request)

@app.post("/auth/register")
async def register(request: Request):
    data = await request.json()
    username = data.get("username","").strip()
    password = data.get("password","").strip()
    code     = data.get("invite_code","").strip()
    if not username or not password or not code:
        raise HTTPException(status_code=400, detail="Заполните все поля")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")
    pool = await get_db()
    async with pool.acquire() as conn:
        invite = await conn.fetchrow(
            "SELECT * FROM invite_codes WHERE code=$1 AND used_by IS NULL AND (expires_at IS NULL OR expires_at > NOW())",
            code
        )
        if not invite:
            raise HTTPException(status_code=400, detail="Инвайт-код недействителен или уже использован")
        existing = await conn.fetchrow("SELECT id FROM users WHERE username=$1", username)
        if existing:
            raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")
        user_id = await conn.fetchval(
            "INSERT INTO users (username, password_hash) VALUES ($1,$2) RETURNING id",
            username, hash_password(password)
        )
        await conn.execute("UPDATE invite_codes SET used_by=$1 WHERE code=$2", user_id, code)
    token = await create_session(user_id)
    return {"token": token, "username": username, "is_admin": False}

@app.post("/auth/change-password")
async def change_password(request: Request):
    user = await get_current_user(request)
    data = await request.json()
    old_password = data.get("old_password","")
    new_password = data.get("new_password","").strip()
    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="Заполните все поля")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Новый пароль минимум 6 символов")
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE id=$1 AND password_hash=$2",
            user["id"], hash_password(old_password)
        )
        if not row:
            raise HTTPException(status_code=400, detail="Неверный текущий пароль")
        await conn.execute(
            "UPDATE users SET password_hash=$1 WHERE id=$2",
            hash_password(new_password), user["id"]
        )
    return {"ok": True}

# ── Admin ─────────────────────────────────────────────────────────────────────
@app.get("/admin/users")
async def list_users(request: Request):
    await require_admin(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, username, is_admin, created_at FROM users ORDER BY id")
    return [dict(r) for r in rows]

@app.post("/admin/users")
async def create_user(request: Request):
    await require_admin(request)
    data = await request.json()
    username = data.get("username","").strip()
    password = data.get("password","").strip()
    is_admin = data.get("is_admin", False)
    if not username or not password:
        raise HTTPException(status_code=400, detail="Укажите логин и пароль")
    pool = await get_db()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES ($1,$2,$3)",
                username, hash_password(password), is_admin
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=400, detail="Пользователь уже существует")
    return {"ok": True}

@app.delete("/admin/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    await require_admin(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id=$1 AND username!='admin'", user_id)
    return {"ok": True}

@app.get("/admin/invites")
async def list_invites(request: Request):
    await require_admin(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT i.id, i.code, i.created_at, i.expires_at,
                   u.username as used_by_username
            FROM invite_codes i
            LEFT JOIN users u ON i.used_by = u.id
            ORDER BY i.created_at DESC
        """)
    return [dict(r) for r in rows]

@app.post("/admin/invites")
async def create_invite(request: Request):
    admin = await require_admin(request)
    data = await request.json()
    days = data.get("days")  # None = бессрочный
    code = secrets.token_hex(8).upper()
    expires = datetime.utcnow() + timedelta(days=days) if days else None
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO invite_codes (code, created_by, expires_at) VALUES ($1,$2,$3)",
            code, admin["id"], expires
        )
    return {"code": code, "expires_at": expires}

@app.delete("/admin/invites/{invite_id}")
async def delete_invite(invite_id: int, request: Request):
    await require_admin(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM invite_codes WHERE id=$1", invite_id)
    return {"ok": True}

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_URI}

# ── Yandex GPT ────────────────────────────────────────────────────────────────
async def ask_yandex(system_text, user_text, temperature=0.5, max_tokens=2000):
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise HTTPException(status_code=500, detail="Yandex API credentials не настроены")
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}", "Content-Type": "application/json"}
    body = {
        "modelUri": MODEL_URI,
        "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": max_tokens},
        "messages": [{"role": "system", "text": system_text}, {"role": "user", "text": user_text}],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(YANDEX_URL, headers=headers, json=body)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Yandex GPT ошибка: {resp.text}")
    return resp.json()["result"]["alternatives"][0]["message"]["text"]

def clean_json(raw):
    return json.loads(raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip())

# ── WB card ───────────────────────────────────────────────────────────────────
async def fetch_wb_card(art):
    try:
        url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={art}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        p = r.json().get("data", {}).get("products", [None])[0]
        if not p: return None
        return {
            "title": p.get("name",""), "brand": p.get("brand",""),
            "description": p.get("description",""), "subjectName": p.get("subjectName",""),
            "colors": ", ".join(c.get("name","") for c in p.get("colors",[])),
            "characteristics": "; ".join(f"{o.get('name','')}: {o.get('value','')}" for o in p.get("options",[])),
        }
    except: return None

# ── Generate ──────────────────────────────────────────────────────────────────
@app.post("/generate")
async def generate_description(request: Request):
    await get_current_user(request)
    req = await request.json()
    name=req.get("name",""); category=req.get("category","")
    material=req.get("material",""); probe=req.get("probe","")
    coating=req.get("coating",""); color=req.get("color","")
    insert_stone=req.get("insert_stone",""); insert_weight=req.get("insert_weight","")
    insert_cut=req.get("insert_cut",""); features=req.get("features","")
    keywords=req.get("keywords",""); example_desc=req.get("example_desc","")
    char_count=int(req.get("char_count",700))
    if not name: raise HTTPException(status_code=400, detail="Укажите название товара")

    example_block = f'\nПРИМЕР ОПИСАНИЯ КОНКУРЕНТА:\n"""\n{example_desc.strip()[:1000]}\n"""\n' if example_desc.strip() else ""
    insert_parts = [p for p in [insert_stone if insert_stone not in ("","Без вставки") else "", insert_weight, f"огранка: {insert_cut}" if insert_cut else ""] if p]
    insert_str = ", ".join(insert_parts) if insert_parts else "без вставки"
    material_full = f"{material} {probe}".strip() if probe else material

    system = f"""Ты — копирайтер для маркетплейса Wildberries. Пишешь описания ювелирных украшений.
Правила: только русский язык, живой профессиональный стиль, без канцелярита.
Не используй: лучший, идеальный, топ, элитный. Без эмодзи и маркированных списков.
Органично включай ключевые слова в разных словоформах.
Структура: сильное первое предложение → описание → материал и вставки → кому подходит → завершение с ключами.
КРИТИЧЕСКИ ВАЖНО: описание должно быть ровно {char_count} символов (±30). Считай символы.
Отвечай строго JSON без markdown: {{"description":"...","meta_keywords":"...","meta_description":"...","seo_score":85,"uniqueness":90,"keyword_density":3.0}}"""

    user = f"""Название: {name}\nКатегория: {category}\nМатериал: {material_full or 'не указан'}\nЦвет: {color or 'не указан'}\nПокрытие: {coating or 'без покрытия'}\nВставка: {insert_str}\nОсобенности: {features or 'не указаны'}\nКлючевые слова: {keywords or 'сгенерируй на основе названия'}\nДлина: ровно {char_count} символов (±30)\n{example_block}\nОтвет — строго JSON."""

    raw = await ask_yandex(system, user, temperature=0.5, max_tokens=3000)
    try: result = clean_json(raw)
    except: raise HTTPException(status_code=422, detail=f"Ошибка парсинга: {raw[:300]}")

    desc = result.get("description","")
    if abs(len(desc) - char_count) > 50:
        diff = char_count - len(desc)
        action = "расширь" if diff > 0 else "сократи"
        try:
            corrected = await ask_yandex(
                "Ты — редактор. Корректируешь длину текста.",
                f"Текст ({len(desc)} символов):\n\"\"\"{desc}\"\"\"\n\n{action} до {char_count} символов (±30). Верни только текст.",
                temperature=0.3, max_tokens=3000
            )
            corrected = corrected.strip().strip('"')
            if abs(len(corrected) - char_count) < abs(len(desc) - char_count):
                result["description"] = corrected
        except: pass
    return result

# ── Keywords ──────────────────────────────────────────────────────────────────
@app.post("/keywords")
async def extract_keywords(request: Request):
    await get_current_user(request)
    req = await request.json()
    articuls = req.get("articuls",[])[:30]
    if not articuls: raise HTTPException(status_code=400, detail="Список артикулов пуст")
    results = []
    for art in articuls:
        card = await fetch_wb_card(str(art))
        is_real = bool(card and card.get("title"))
        context = f"Реальные данные WB (артикул {art}):\n- Название: {card['title']}\n- Бренд: {card['brand']}\n- Категория: {card['subjectName']}\n- Характеристики: {card['characteristics']}\n- Описание: {card['description'][:600]}" if is_real else f"Карточка WB {art} недоступна. Сгенерируй семантическое ядро для ювелирного украшения."
        raw = await ask_yandex(
            "Ты — SEO-специалист по Wildberries, ниша ювелирки. Отвечаешь строго JSON без markdown.",
            f"{context}\n\nСоставь семантическое ядро из 12–18 ключевых слов.\nКластеры: Категорийные, По материалу, По назначению, По характеристикам, Брендовые, LSI.\nЧастотность: высокая/средняя/низкая.\nJSON: {{\"title\":\"...\",\"keywords\":[{{\"word\":\"...\",\"cluster\":\"...\",\"freq\":\"...\"}}]}}",
            temperature=0.4
        )
        try:
            parsed = clean_json(raw)
            results.append({"artId":art,"title":parsed.get("title",str(art)),"keywords":parsed.get("keywords",[]),"real":is_real})
        except:
            results.append({"artId":art,"title":str(art),"keywords":[],"real":False,"error":raw[:200]})
    return {"results": results}
