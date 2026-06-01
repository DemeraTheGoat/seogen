import os
import json
import httpx
import hashlib
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()

YANDEX_API_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_URL       = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
MODEL_URI        = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest"
DATABASE_URL     = os.getenv("DATABASE_URL")
SECRET_KEY       = os.getenv("SECRET_KEY", secrets.token_hex(32))

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
        # Создаём admin если не существует
        existing = await conn.fetchrow("SELECT id FROM users WHERE username = 'admin'")
        if not existing:
            pwd_hash = hashlib.sha256("admin123".encode()).hexdigest()
            await conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES ($1, $2, $3)",
                "admin", pwd_hash, True
            )
            print("✓ Admin user created: admin / admin123")

@app.on_event("startup")
async def startup():
    await init_db()

# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

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
        raise HTTPException(status_code=401, detail="Сессия истекла или недействительна")
    return dict(row)

async def require_admin(request: Request):
    user = await get_current_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Доступ только для администратора")
    return user

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.post("/auth/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Укажите логин и пароль")
    pool = await get_db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE username = $1 AND password_hash = $2",
            username, hash_password(password)
        )
    if not user:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = await create_session(user["id"])
    return {"token": token, "username": user["username"], "is_admin": user["is_admin"]}

@app.post("/auth/logout")
async def logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        pool = await get_db()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM sessions WHERE token = $1", token)
    return {"ok": True}

@app.get("/auth/me")
async def me(request: Request):
    user = await get_current_user(request)
    return user

# ── Admin: user management ────────────────────────────────────────────────────
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
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    is_admin = data.get("is_admin", False)
    if not username or not password:
        raise HTTPException(status_code=400, detail="Укажите логин и пароль")
    pool = await get_db()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES ($1, $2, $3)",
                username, hash_password(password), is_admin
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")
    return {"ok": True}

@app.delete("/admin/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    await require_admin(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1 AND username != 'admin'", user_id)
    return {"ok": True}

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_URI}

# ── Yandex GPT ────────────────────────────────────────────────────────────────
async def ask_yandex(system_text: str, user_text: str, temperature: float = 0.5, max_tokens: int = 2000) -> str:
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise HTTPException(status_code=500, detail="Yandex API credentials не настроены")
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type":  "application/json",
    }
    body = {
        "modelUri": MODEL_URI,
        "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": max_tokens},
        "messages": [
            {"role": "system", "text": system_text},
            {"role": "user",   "text": user_text},
        ],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(YANDEX_URL, headers=headers, json=body)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Yandex GPT ошибка: {resp.text}")
    return resp.json()["result"]["alternatives"][0]["message"]["text"]

def clean_json(raw: str) -> dict:
    clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(clean)

# ── WB card fetch ─────────────────────────────────────────────────────────────
async def fetch_wb_card(art: str) -> dict | None:
    try:
        url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={art}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        p = r.json().get("data", {}).get("products", [None])[0]
        if not p:
            return None
        return {
            "title":           p.get("name", ""),
            "brand":           p.get("brand", ""),
            "description":     p.get("description", ""),
            "subjectName":     p.get("subjectName", ""),
            "colors":          ", ".join(c.get("name","") for c in p.get("colors", [])),
            "characteristics": "; ".join(f"{o.get('name','')}: {o.get('value','')}" for o in p.get("options", [])),
        }
    except Exception:
        return None

# ── Generate ──────────────────────────────────────────────────────────────────
@app.post("/generate")
async def generate_description(request: Request):
    await get_current_user(request)  # проверка авторизации
    req = await request.json()

    name          = req.get("name", "")
    category      = req.get("category", "")
    platform      = req.get("platform", "Wildberries")
    material      = req.get("material", "")
    probe         = req.get("probe", "")
    coating       = req.get("coating", "")
    color         = req.get("color", "")
    insert_stone  = req.get("insert_stone", "")
    insert_weight = req.get("insert_weight", "")
    insert_cut    = req.get("insert_cut", "")
    features      = req.get("features", "")
    keywords      = req.get("keywords", "")
    example_desc  = req.get("example_desc", "")
    char_count    = int(req.get("char_count", 700))

    if not name:
        raise HTTPException(status_code=400, detail="Укажите название товара")

    example_block = ""
    if example_desc and example_desc.strip():
        example_block = f'\nПРИМЕР ОПИСАНИЯ КОНКУРЕНТА (ориентир по стилю, текст должен быть уникальным):\n"""\n{example_desc.strip()[:1000]}\n"""\n'

    insert_parts = []
    if insert_stone and insert_stone not in ("", "Без вставки"):
        insert_parts.append(insert_stone)
        if insert_weight: insert_parts.append(insert_weight)
        if insert_cut:    insert_parts.append(f"огранка: {insert_cut}")
    insert_str = ", ".join(insert_parts) if insert_parts else "без вставки"
    material_full = f"{material} {probe}".strip() if probe else material

    system = f"""Ты — копирайтер для маркетплейса {platform}. Пишешь описания ювелирных украшений.

Правила:
- Только русский язык, стиль живой и профессиональный
- Не используй: лучший, идеальный, топ, элитный
- Без эмодзи и маркированных списков
- Не добавляй информацию которой нет в данных
- Органично включай ключевые слова в разных словоформах

Структура: сильное первое предложение → описание товара → материал и вставки → кому подходит → завершение с ключами.

КРИТИЧЕСКИ ВАЖНО: описание должно быть ровно {char_count} символов (±30). Считай символы и подгоняй текст.

Отвечай строго JSON без markdown:
{{"description":"текст","meta_keywords":"5 ключей через запятую","meta_description":"до 160 символов","seo_score":85,"uniqueness":90,"keyword_density":3.0}}"""

    user = f"""Название: {name}
Категория: {category}
Материал: {material_full if material_full.strip() else "не указан"}
Цвет металла: {color if color else "не указан"}
Покрытие: {coating if coating else "без покрытия"}
Вставка: {insert_str}
Особенности: {features if features else "не указаны"}
Ключевые слова: {keywords if keywords else "сгенерируй на основе названия"}
Длина: ровно {char_count} символов (±30)
{example_block}
Ответ — строго JSON без markdown."""

    raw = await ask_yandex(system, user, temperature=0.5, max_tokens=3000)
    try:
        result = clean_json(raw)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Ошибка парсинга: {raw[:300]}")

    # Коррекция длины если промахнулись больше чем на 50 символов
    desc = result.get("description", "")
    if abs(len(desc) - char_count) > 50:
        diff = char_count - len(desc)
        action = "расширь" if diff > 0 else "сократи"
        try:
            corrected = await ask_yandex(
                "Ты — редактор. Корректируешь длину текста до нужного количества символов.",
                f"""Текст ({len(desc)} символов):\n\"\"\"{desc}\"\"\"\n\n{action} до {char_count} символов (±30). Верни только текст без JSON и комментариев.""",
                temperature=0.3, max_tokens=3000
            )
            corrected = corrected.strip().strip('"')
            if abs(len(corrected) - char_count) < abs(len(desc) - char_count):
                result["description"] = corrected
        except Exception:
            pass

    return result

# ── Keywords ──────────────────────────────────────────────────────────────────
@app.post("/keywords")
async def extract_keywords(request: Request):
    await get_current_user(request)  # проверка авторизации
    req      = await request.json()
    articuls = req.get("articuls", [])[:30]
    if not articuls:
        raise HTTPException(status_code=400, detail="Список артикулов пуст")

    results = []
    for art in articuls:
        card = await fetch_wb_card(str(art))
        is_real = bool(card and card.get("title"))

        if is_real:
            context = f"""Реальные данные карточки WB (артикул {art}):
- Название: {card['title']}
- Бренд: {card['brand']}
- Категория: {card['subjectName']}
- Характеристики: {card['characteristics']}
- Описание: {card['description'][:600]}"""
        else:
            context = f"Карточка WB {art} недоступна. Сгенерируй семантическое ядро для ювелирного украшения."

        raw = await ask_yandex(
            "Ты — SEO-специалист по Wildberries, ниша ювелирных украшений. Отвечаешь строго JSON без markdown.",
            f"""{context}\n\nСоставь семантическое ядро из 12–18 ключевых слов.\nКластеры: Категорийные, По материалу, По назначению, По характеристикам, Брендовые, LSI.\nЧастотность: высокая/средняя/низкая.\n\nJSON: {{"title":"...","keywords":[{{"word":"...","cluster":"...","freq":"..."}}]}}""",
            temperature=0.4
        )
        try:
            parsed = clean_json(raw)
            results.append({"artId": art, "title": parsed.get("title", str(art)), "keywords": parsed.get("keywords", []), "real": is_real})
        except Exception:
            results.append({"artId": art, "title": str(art), "keywords": [], "real": False, "error": raw[:200]})

    return {"results": results}
