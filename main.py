import os
import json
import httpx
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

YANDEX_API_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_URL       = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
MODEL_URI        = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest"

# Используем только встроенные типы Python — без pydantic моделей
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SEO Generator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def ask_yandex(system_text: str, user_text: str, temperature: float = 0.6, max_tokens: int = 2000) -> str:
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise HTTPException(status_code=500, detail="Yandex API credentials не настроены в .env")

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type":  "application/json",
    }
    body = {
        "modelUri": MODEL_URI,
        "completionOptions": {
            "stream":      False,
            "temperature": temperature,
            "maxTokens":   max_tokens,
        },
        "messages": [
            {"role": "system", "text": system_text},
            {"role": "user",   "text": user_text},
        ],
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(YANDEX_URL, headers=headers, json=body)

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"Yandex GPT ошибка: {resp.text}")

    data = resp.json()
    return data["result"]["alternatives"][0]["message"]["text"]


def clean_json(raw: str) -> dict:
    clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(clean)


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_URI}


@app.post("/generate")
async def generate_description(request: Request):
    req = await request.json()

    name         = req.get("name", "")
    category     = req.get("category", "")
    platform     = req.get("platform", "Wildberries")
    material      = req.get("material", "")
    probe         = req.get("probe", "")
    coating       = req.get("coating", "")
    color         = req.get("color", "")
    insert_stone  = req.get("insert_stone", "")
    insert_weight = req.get("insert_weight", "")
    insert_cut    = req.get("insert_cut", "")
    features      = req.get("features", "")
    keywords     = req.get("keywords", "")
    example_desc = req.get("example_desc", "")
    char_count   = req.get("char_count", 700)

    if not name:
        raise HTTPException(status_code=400, detail="Укажите название товара")

    example_block = ""
    if example_desc and example_desc.strip():
        example_block = f"""
ПРИМЕР ОПИСАНИЯ КОНКУРЕНТА (используй как ориентир по стилю, но текст должен быть уникальным):
\"\"\"
{example_desc.strip()[:1000]}
\"\"\"
"""

    system = """Ты — профессиональный SEO-копирайтер для маркетплейса {platform}, специализирующийся на создании продающих и SEO-оптимизированных описаний товаров.

ТВОЯ ЗАДАЧА:
На основе входных данных и списка ключевых слов создавать:
1. Продающее SEO-описание товара для {platform}
2. Текст должен выглядеть естественно, а не как набор ключей
3. Максимально использовать SEO-потенциал без переспама
4. Поднимать релевантность карточки в поиске {platform}

────────────────────────
ПРАВИЛА ГЕНЕРАЦИИ:
────────────────────────

• Пиши только на русском языке
• Стиль — профессиональный, современный, дорогой
• Текст должен быть читаемым и живым
• Не использовать канцелярит
• Не использовать пустые фразы
• Не писать очевидные вещи ради объёма
• Не добавлять информацию, которой нет во входных данных
• Не использовать эмодзи
• Не использовать CAPS LOCK
• Не делать маркированные списки
• Не использовать слова: "лучший", "идеальный", "топ", "премиум", "элитный", "люкс"

────────────────────────
SEO-ТРЕБОВАНИЯ:
────────────────────────

• Органично внедряй ключевые слова
• Используй ключи в разных словоформах
• Распределяй ключи равномерно по тексту
• Самые важные ключи — в начале описания
• Избегай повторения одной и той же фразы подряд
• Не превращай текст в SEO-спам
• Сохраняй естественность текста

────────────────────────
СТРУКТУРА ОПИСАНИЯ:
────────────────────────

1. Сильное первое предложение с главным ключом
2. Основное описание товара
3. Материалы / особенности / преимущества
4. Для кого подходит
5. Поводы для покупки / стиль / образ
6. Завершение с дополнительными ключами

────────────────────────
ОСОБОЕ ПРАВИЛО ДЛЯ {platform}:
────────────────────────

{platform} лучше ранжирует:
• высокую плотность релевантных слов
• естественные повторения
• длинные SEO-фразы
• словоформы
• смежные запросы

Поэтому:
• аккуратно расширяй ключевые фразы
• добавляй околоцелевые запросы
• используй LSI-лексикон
• делай текст максимально поисково-релевантным

────────────────────────
ВАЖНО:
────────────────────────

• Генерируй текст как опытный SEO-копирайтер {platform}
• Делай описание максимально релевантным поиску
• Текст должен одновременно: продавать, ранжироваться, выглядеть естественно
• Не объясняй свои действия, не добавляй комментарии
• Сразу выдавай готовый результат в формате JSON без markdown

Ответ — строго JSON без markdown:
{{"description":"...","meta_keywords":"5 ключей через запятую","meta_description":"до 160 символов","seo_score":85,"uniqueness":90,"keyword_density":3.0}}""".format(platform=platform)

    # Build insert description
    insert_parts = []
    if insert_stone and insert_stone not in ("", "Без вставки"):
        insert_parts.append(insert_stone)
        if insert_weight: insert_parts.append(insert_weight)
        if insert_cut: insert_parts.append(f"огранка: {insert_cut}")
    insert_str = ", ".join(insert_parts) if insert_parts else "без вставки"

    material_full = material
    if probe: material_full += f" {probe}"

    user = f"""Название товара:
{name}

Категория:
{category}

Материал:
{material_full if material_full.strip() else "не указан"}

Проба:
{probe if probe else "не указана"}

Цвет металла:
{color if color else "не указан"}

Покрытие:
{coating if coating else "без покрытия"}

Вставка:
{insert_str}

Дополнительные особенности и преимущества:
{features if features else "не указаны"}

Целевая аудитория:
покупатели на {platform}

Ключевые слова:
{keywords if keywords else "сгенерируй самостоятельно на основе названия и категории"}

Стиль текста:
профессиональный, современный, живой

Желаемая длина:
около {char_count} символов (±50)
{example_block}
Ответ — строго JSON без markdown:
{{"description":"...","meta_keywords":"...","meta_description":"...","seo_score":85,"uniqueness":90,"keyword_density":3.0}}"""

    raw = await ask_yandex(system, user, temperature=0.5)

    try:
        return clean_json(raw)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Ошибка парсинга ответа модели: {raw[:300]}")


@app.post("/keywords")
async def extract_keywords(request: Request):
    req      = await request.json()
    articuls = req.get("articuls", [])[:30]
    card_data = req.get("card_data", {})

    if not articuls:
        raise HTTPException(status_code=400, detail="Список артикулов пуст")

    results = []

    for art in articuls:
        card = card_data.get(str(art))

        if card:
            context = f"""Реальные данные карточки WB (артикул {art}):
- Название: {card.get("title", "")}
- Бренд: {card.get("brand", "")}
- Категория: {card.get("subjectName", "")}
- Цвета: {card.get("colors", "")}
- Характеристики: {card.get("characteristics", "")}
- Описание: {str(card.get("description", ""))[:500]}

На основе ЭТИХ данных извлеки и расширь ключевые слова."""
            is_real = True
        else:
            context = f"Данные карточки WB {art} недоступны. Сгенерируй вероятное семантическое ядро для ювелирного украшения."
            is_real = False

        system = """Ты — профессиональный SEO-специалист по маркетплейсу Wildberries с опытом в нише ювелирных украшений.

ТВОЯ ЗАДАЧА:
Составить точное семантическое ядро — список ключевых слов, которые реальные покупатели вводят в поиск WB при поиске ювелирных украшений.

ПРАВИЛА:
• Только реальные поисковые запросы — не придумывай несуществующие фразы
• Используй словоформы и варианты написания
• Включай как короткие (1–2 слова), так и длинные (3–5 слов) запросы
• Учитывай специфику ювелирного рынка: металл, проба, вставки, размер, повод
• Кластеризуй слова по смыслу

КЛАСТЕРЫ:
- «Категорийные» — общие запросы по типу товара (кольцо золотое, серьги серебро)
- «По материалу» — металл, камень, проба (золото 585, бриллиант, фианит)
- «По назначению» — подарок, повод, кому (подарок на день рождения, украшение для невесты)
- «По характеристикам» — цвет, огранка, вес, размер
- «Брендовые» — с брендом если известен
- «LSI» — синонимы, смежные запросы, околоцелевые

ЧАСТОТНОСТЬ (оценочно):
• высокая — >10 000 поисков/мес
• средняя — 1 000–10 000 поисков/мес
• низкая — <1 000 поисков/мес

Отвечай строго JSON без markdown."""

        user = f"""{context}

Составь семантическое ядро из 12–18 ключевых слов.

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
                "artId":    art,
                "title":    str(art),
                "keywords": [],
                "real":     False,
                "error":    f"Ошибка парсинга: {raw[:200]}",
            })

    return {"results": results}
