import os
import json
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

YANDEX_API_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_URL       = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
MODEL_URI        = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest"

app = FastAPI(title="SEO Generator API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


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


# ── Fetch real WB card data (server-side, no CORS) ───────────────────────────
async def fetch_wb_card(art: str) -> dict | None:
    try:
        url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={art}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.json()
        p = data.get("data", {}).get("products", [None])[0]
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


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_URI}


# ── WB card proxy (for frontend to get card data without CORS) ────────────────
@app.get("/wb/card/{art}")
async def wb_card(art: str):
    card = await fetch_wb_card(art)
    if not card:
        return {"found": False}
    return {"found": True, **card}


# ── Generate description ──────────────────────────────────────────────────────
@app.post("/generate")
async def generate_description(request: Request):
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

    system = f"""Ты — копирайтер для маркетплейса {platform}. Пишешь описания ювелирных украшений для карточек товаров.

Правила написания:
- Только русский язык
- Стиль живой, профессиональный, без канцелярита
- Не используй слова: лучший, идеальный, топ, элитный
- Без эмодзи и маркированных списков
- Не добавляй информацию которой нет в данных о товаре
- Органично включай ключевые слова в разных словоформах
- Важные ключевые слова ставь в начало текста

Структура текста:
1. Первое предложение с главным ключевым словом
2. Описание товара и его свойств
3. Материал, вставки, покрытие
4. Кому подходит и по какому поводу
5. Завершение с дополнительными ключами

КРИТИЧЕСКИ ВАЖНО: описание должно быть ровно {char_count} символов (допустимо ±30 символов). Это жёсткое требование — считай символы и подгоняй текст под нужную длину.

Отвечай строго в формате JSON без markdown:
{{"description":"текст","meta_keywords":"5 ключей через запятую","meta_description":"до 160 символов","seo_score":85,"uniqueness":90,"keyword_density":3.0}}"""

    user = f"""Название товара: {name}
Категория: {category}
Материал: {material_full if material_full.strip() else "не указан"}
Цвет металла: {color if color else "не указан"}
Покрытие: {coating if coating else "без покрытия"}
Вставка: {insert_str}
Особенности: {features if features else "не указаны"}
Ключевые слова: {keywords if keywords else "сгенерируй на основе названия и категории"}
Желаемая длина: ровно {char_count} символов (±30)
{example_block}
Ответ — строго JSON без markdown."""

    raw = await ask_yandex(system, user, temperature=0.5, max_tokens=3000)

    try:
        result = clean_json(raw)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Ошибка парсинга: {raw[:300]}")

    desc = result.get("description", "")
    current_len = len(desc)
    tolerance = 50

    # Если длина не совпадает — делаем второй запрос для коррекции
    if abs(current_len - char_count) > tolerance:
        diff = char_count - current_len
        action = "расширь" if diff > 0 else "сократи"
        correction_user = f"""Вот текст описания товара ({current_len} символов):
\"\"\"{desc}\"\"\"

{action} этот текст так, чтобы он стал ровно {char_count} символов (±30).
Сохрани стиль, смысл и ключевые слова. Не добавляй новую информацию — только {'добавляй детали и развёртывай предложения' if diff > 0 else 'убирай лишнее и сокращай предложения'}.

Верни только исправленный текст описания, без JSON и комментариев."""

        correction_system = "Ты — редактор текстов. Корректируешь длину текста до нужного количества символов, сохраняя стиль и смысл."

        try:
            corrected = await ask_yandex(correction_system, correction_user, temperature=0.3, max_tokens=3000)
            corrected = corrected.strip().strip('"')
            if abs(len(corrected) - char_count) < abs(current_len - char_count):
                result["description"] = corrected
        except Exception:
            pass  # оставляем оригинал если коррекция не удалась

    return result


# ── Keywords ──────────────────────────────────────────────────────────────────
@app.post("/keywords")
async def extract_keywords(request: Request):
    req      = await request.json()
    articuls = req.get("articuls", [])[:30]

    if not articuls:
        raise HTTPException(status_code=400, detail="Список артикулов пуст")

    results = []

    for art in articuls:
        # Тянем реальные данные с WB через сервер (без CORS)
        card = await fetch_wb_card(str(art))
        is_real = bool(card and card.get("title"))

        if is_real:
            context = f"""Реальные данные карточки WB (артикул {art}):
- Название: {card['title']}
- Бренд: {card['brand']}
- Категория: {card['subjectName']}
- Цвета: {card['colors']}
- Характеристики: {card['characteristics']}
- Описание: {card['description'][:600]}

Извлеки ключевые слова на основе ЭТИХ реальных данных."""
        else:
            context = f"Карточка WB {art} недоступна. Сгенерируй семантическое ядро для ювелирного украшения с таким артикулом."

        system = """Ты — SEO-специалист по Wildberries, ниша ювелирных украшений.
Составляешь семантические ядра из реальных поисковых запросов покупателей.
Отвечаешь строго JSON без markdown."""

        user = f"""{context}

Составь семантическое ядро из 12–18 ключевых слов которые реальные покупатели вводят в поиск WB.

Кластеры:
- «Категорийные» — общие запросы по типу товара
- «По материалу» — металл, камень, проба
- «По назначению» — подарок, повод, кому
- «По характеристикам» — цвет, огранка, вес
- «Брендовые» — с брендом если известен
- «LSI» — синонимы и смежные запросы

Частотность: высокая (>10к/мес), средняя (1к–10к), низкая (<1к).

Ответ — строго JSON без markdown:
{{"title":"название товара","keywords":[{{"word":"...","cluster":"...","freq":"высокая|средняя|низкая"}}]}}"""

        raw = await ask_yandex(system, user, temperature=0.4)

        try:
            parsed = clean_json(raw)
            results.append({
                "artId":    art,
                "title":    parsed.get("title", str(art)),
                "keywords": parsed.get("keywords", []),
                "real":     is_real,
            })
        except Exception:
            results.append({
                "artId": art, "title": str(art),
                "keywords": [], "real": False,
                "error": f"Ошибка парсинга: {raw[:200]}",
            })

    return {"results": results}
