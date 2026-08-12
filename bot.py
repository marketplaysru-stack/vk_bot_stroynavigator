#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Строительный навигатор – генерация разнообразных постов о строительстве, ремонте и архитектуре.
Форматы: викторина, опрос, челлендж, загадка.
Расширенные темы, полезные советы, актуальные тренды.
Автопостинг каждые 6 часов. Поддержка topics.txt и RSS.
"""

import os
import io
import json
import time
import logging
import random
import re
import requests
import threading
import schedule
import feedparser
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
import vk_api
from vk_api.upload import VkUpload

load_dotenv()

# ---------- НАСТРОЙКИ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
VK_TOKEN_AI = os.getenv("VK_TOKEN_AI")
GROUP_ID_AI = int(os.getenv("GROUP_ID_AI", "-239598146"))
VK_TOKEN_USER = os.getenv("VK_TOKEN_USER")
VK_USER_ID = int(os.getenv("VK_USER_ID", "317272476"))

AGNES_API_KEY = os.getenv("AGNES_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PIXAZO_API_KEY = os.getenv("PIXAZO_API_KEY")
POLLINATIONS_BASE_URL = os.getenv("POLLINATIONS_BASE_URL", "https://image.pollinations.ai")
IMAGE_NEGATIVE_PROMPT = os.getenv("IMAGE_NEGATIVE_PROMPT", "ugly, deformed, blurry, low quality, sad, depressive, dark, gloomy, people, human")

RSS_DEFAULT_GROUP = os.getenv("RSS_DEFAULT_GROUP", "Строительный навигатор")
RSS_SOURCES_JSON = os.getenv("RSS_SOURCES", '[]')
RSS_ENABLED = os.getenv("RSS_ENABLED", "false").lower() == "true"
DATA_DIR = os.getenv("DATA_DIR", "./data")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан!")

os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "post_state.json")
USED_IMAGES_FILE = os.path.join(DATA_DIR, "used_images.json")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ---------- КЭШ КАРТИНОК ----------
def load_used_images():
    if os.path.exists(USED_IMAGES_FILE):
        with open(USED_IMAGES_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
            return set(data.get("hashes", []))
    return set()

def save_used_images(used_set):
    data = {"hashes": list(used_set), "updated": datetime.now().isoformat()}
    with open(USED_IMAGES_FILE, "w") as f:
        json.dump(data, f)

def clean_used_images():
    if not os.path.exists(USED_IMAGES_FILE):
        return
    try:
        with open(USED_IMAGES_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            save_used_images(set())
            logger.info("🧹 Кэш картинок очищен (старый формат)")
            return
        updated = datetime.fromisoformat(data.get("updated", "2000-01-01"))
        if datetime.now() - updated > timedelta(days=7):
            save_used_images(set())
            logger.info("🧹 Кэш картинок очищен (старше 7 дней)")
    except Exception as e:
        logger.warning(f"Ошибка очистки кэша: {e}")

def compute_hash(image_bytes):
    return hashlib.md5(image_bytes).hexdigest()

def is_image_used(image_bytes):
    h = compute_hash(image_bytes)
    used = load_used_images()
    return h in used

def mark_image_as_used(image_bytes):
    h = compute_hash(image_bytes)
    used = load_used_images()
    used.add(h)
    save_used_images(used)

# ---------- ЗАГРУЗКА ПОСТОВ ИЗ ФАЙЛА ----------
POSTS_FILE = "posts.txt"
def load_posts_from_file():
    posts = []
    if not os.path.exists(POSTS_FILE):
        return None
    try:
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        blocks = re.split(r'\n===', content)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if block.startswith('==='):
                block = block[3:].strip()
            lines = block.split('\n')
            if not lines:
                continue
            title = lines[0].strip()
            text = '\n'.join(lines[1:]).strip()
            if title and text:
                posts.append({'title': title, 'text': text})
        if posts:
            logger.info(f"✅ Загружено {len(posts)} готовых постов из {POSTS_FILE}")
            return posts
    except Exception as e:
        logger.error(f"Ошибка чтения {POSTS_FILE}: {e}")
    return None

# ---------- RSS ПАРСИНГ ----------
def get_rss_entries(sources_json):
    sources = json.loads(sources_json) if sources_json else []
    entries = []
    for src in sources:
        url = src.get("url")
        if not url:
            continue
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if title:
                    entries.append(title)
            logger.info(f"RSS {url}: получено {len(entries)} заголовков")
        except Exception as e:
            logger.error(f"Ошибка парсинга RSS {url}: {e}")
    return entries

# ---------- ЗАПАСНЫЕ ТЕМЫ (расширенные строительные) ----------
def load_topics():
    try:
        with open("topics.txt", "r", encoding="utf-8") as f:
            topics = [line.strip() for line in f if line.strip()]
        if topics:
            return topics
    except FileNotFoundError:
        pass
    return [
        "Современные строительные материалы",
        "Тенденции в архитектуре",
        "Ремонт своими руками",
        "Энергоэффективные дома",
        "Ландшафтный дизайн",
        "Строительство загородных домов",
        "Инновации в строительстве",
        "Безопасность на стройплощадке",
        "Отделочные материалы",
        "Инженерные системы в доме",
        "Умный дом и автоматизация",
        "Экологичное строительство",
        "Проектирование и планировка",
        "Строительство бани и сауны",
        "Теплоизоляция и утепление",
        "Кровельные материалы",
        "Фундаменты и основания",
        "Строительный контроль и надзор",
        "Ремонт квартир и офисов",
        "Строительные нормы и стандарты"
    ]

# ---------- TELEGRAM ----------
def send_message(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": chat_id, "text": text})

def get_updates(offset=None):
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=(15, 120))
        return resp.json().get("result", [])
    except Exception as e:
        logger.error(f"Ошибка получения обновлений: {e}")
        return []

# ---------- ГЕНЕРАЦИЯ ИГРОВЫХ ФОРМАТОВ (строительная тематика) ----------
def generate_construction_quiz(topic):
    questions = [
        {
            "q": "Какой материал лучше всего подходит для несущих стен в малоэтажном строительстве?",
            "options": ["Газобетон", "Кирпич", "Дерево", "Сэндвич-панели"],
            "answer": "Газобетон"
        },
        {
            "q": "Что такое «монолитное строительство»?",
            "options": ["Строительство из блоков", "Заливка бетона в опалубку", "Каркасное строительство", "Строительство из панелей"],
            "answer": "Заливка бетона в опалубку"
        },
        {
            "q": "Какой материал лучше всего удерживает тепло?",
            "options": ["Минеральная вата", "Пенопласт", "Эковата", "Пенополиуретан"],
            "answer": "Пенополиуретан"
        },
        {
            "q": "Что такое «усадка дома» и когда она происходит?",
            "options": ["В первый год после строительства", "Через 5 лет", "Никогда", "Только зимой"],
            "answer": "В первый год после строительства"
        },
        {
            "q": "Какая минимальная толщина утеплителя для стен в средней полосе России?",
            "options": ["50 мм", "100 мм", "150 мм", "200 мм"],
            "answer": "150 мм"
        },
        {
            "q": "Какой тип фундамента самый надёжный для пучинистых грунтов?",
            "options": ["Ленточный мелкозаглублённый", "Свайно-винтовой", "Плитный", "Столбчатый"],
            "answer": "Свайно-винтовой"
        },
        {
            "q": "Что такое «сухая стяжка пола»?",
            "options": ["Стяжка без воды", "Стяжка из гипса", "Стяжка из цемента", "Стяжка с подогревом"],
            "answer": "Стяжка без воды"
        },
        {
            "q": "Какая кровля считается самой долговечной?",
            "options": ["Металлочерепица", "Гибкая черепица", "Керамическая черепица", "Шифер"],
            "answer": "Керамическая черепица"
        }
    ]
    q = random.choice(questions)
    question_text = f"🧠 **Викторина: {topic}**\n\n{q['q']}\n\n"
    options_text = "\n".join([f"{chr(65+i)}) {opt}" for i, opt in enumerate(q['options'])])
    answer_text = f"\n\n✅ Правильный ответ: **{q['answer']}** (напишите свой вариант в комментариях!)"
    return question_text + options_text + answer_text

def generate_construction_poll(topic):
    return f"📊 **Опрос: {topic}**\n\nКакой тип отопления вы считаете самым эффективным для загородного дома?\n\n1️⃣ Газовое\n2️⃣ Электрическое\n3️⃣ Твёрдотопливное\n4️⃣ Тепловой насос\n\nГолосуйте в комментариях! 👇"

def generate_construction_challenge(topic):
    return f"🏆 **Челлендж: {topic}**\n\nВаше задание на неделю: найдите в своём доме или квартире место, где можно улучшить теплоизоляцию, и сделайте это. Поделитесь фото результата в комментариях!\n\nЖдём ваши работы! 💬"

def generate_construction_riddle(topic):
    riddles = [
        {"question": "Что строится, но никогда не падает, если правильно заложить?", "answer": "Фундамент"},
        {"question": "У него есть корни, но не растёт, есть ствол, но не дерево. Что это?", "answer": "Колонна"},
        {"question": "Без него дом не построить, но он не материал. Что это?", "answer": "Проект"}
    ]
    r = random.choice(riddles)
    return f"🤔 **Загадка: {topic}**\n\n{r['question']}\n\nОтвет напишите в комментариях! 👇\n\n(Правильный ответ завтра в комментариях!)"

# ---------- ГЕНЕРАЦИЯ ТЕКСТА (ОБЫЧНЫЙ ПОСТ) ----------
def generate_standard_post(topic: str) -> str:
    hooks = [
        f"🏗️ {topic.upper()} – ОСНОВА ВАШЕГО ДОМА",
        f"🔨 {topic.upper()} – КАК НЕ ПОТЕРЯТЬ КАЧЕСТВО",
        f"🏠 {topic.upper()} – ВАШ ДОМ ЗАСЛУЖИВАЕТ ЛУЧШЕГО",
        f"😤 {topic.upper()} – ОШИБКИ, КОТОРЫЕ ДОРОГО СТОЯТ",
        f"🚀 {topic.upper()} – ТЕХНОЛОГИИ, КОТОРЫЕ МЕНЯЮТ ИГРУ",
        f"🔥 {topic.upper()} – АКТУАЛЬНЫЕ СОВЕТЫ ДЛЯ СТРОИТЕЛЕЙ",
        f"💡 {topic.upper()} – ИНСАЙТЫ ОТ ПРОФЕССИОНАЛОВ",
        f"📐 {topic.upper()} – ПРАВИЛЬНАЯ ПЛАНИРОВКА – ЗАЛОГ УЮТА"
    ]
    hook = random.choice(hooks)

    leads = [
        f"Вы когда-нибудь задумывались, почему один дом стоит 20 лет, а другой – 50? Всё дело в материале и подходе. {topic} – это не просто выбор, это инвестиция в будущее.",
        f"Ремонт – это всегда стресс. Но что, если я скажу, что 80% проблем можно избежать, если знать несколько секретов? {topic} – это ключ к спокойствию.",
        f"Строительство – это не только про бетон и кирпичи. Это про вашу безопасность, комфорт и уют. {topic} – основа надёжного дома.",
        f"Строительная отрасль меняется каждый день. {topic} – то, что должен знать каждый, кто строит или ремонтирует."
    ]
    lead = random.choice(leads)

    body_pool = [
        ("🏗️", "Выбор материала", "Газобетон, кирпич, дерево – у каждого свои плюсы. Учитывай климат, нагрузку, бюджет. Например, для средней полосы газобетон – отличный выбор по теплоизоляции."),
        ("🔧", "Утепление", "Теплоизоляция – это не только комфорт, но и экономия на отоплении до 40%. Сейчас есть экологичные утеплители, которые служат десятилетиями."),
        ("🛠️", "Качество работ", "Нанять профессионалов или делать самому? Оцените свои силы, чтобы не переделывать. Доверяйте проверенным бригадам или делайте поэтапно."),
        ("📐", "Планировка", "Правильная планировка экономит до 15% площади. Учитывай эргономику и инсоляцию (световой режим)."),
        ("🔩", "Крепёж и фурнитура", "Мелочи решают всё: качественные петли, замки, уголки – долговечность конструкций. Не экономьте на этом."),
        ("⚙️", "Инженерные системы", "Электрика, водопровод, вентиляция – продумай заранее, чтобы не штробить стены потом. Закажите проект инженерных сетей."),
        ("🏠", "Дизайн интерьера", "Свет, цвет, текстуры – создают настроение. Не бойся экспериментировать, но помни о практичности."),
        ("💡", "Энергоэффективность", "Солнечные панели, тепловые насосы – окупаются за 5–7 лет и повышают стоимость дома."),
        ("📌", "Безопасность", "Пожарная сигнализация, заземление – обязательны для защиты. Продумайте план эвакуации."),
        ("🚀", "Инновации", "Умный дом, автоматизация – повышают комфорт и стоимость объекта. Управляйте отоплением и светом с телефона.")
    ]
    random.shuffle(body_pool)
    selected = body_pool[:random.randint(3, 5)]
    body = "\n".join([f"{emoji} **{title}**\n{desc}" for emoji, title, desc in selected])

    conclusions = [
        "Помните: строительство – это марафон, а не спринт. Вдумчивый подход окупается сторицей.",
        "Качественный дом строится один раз, но служит поколениям. Не экономьте na главном.",
        "Инвестируйте в надёжность, и ваш дом станет настоящей крепостью для семьи.",
        "Знание – лучший инструмент строителя. Будьте в курсе новых технологий."
    ]
    conclusion = random.choice(conclusions)

    cta_questions = [
        f"👇 А вы уже сталкивались с проблемами при строительстве или ремонте? Как решали?",
        f"👇 Что для вас самое важное при выборе материалов для дома?",
        f"👇 Согласны, что лучше один раз вложиться в качество, чем потом переделывать?",
        f"👇 Какой строительный лайфхак вы используете чаще всего?"
    ]
    cta = random.choice(cta_questions)

    comments_themes = [
        "1. «Какой материал вы считаете самым надёжным для стен?» – поделитесь опытом.",
        "2. «Как вы экономите на строительстве без потери качества?» – дайте советы.",
        "3. «Что бы вы сделали по-другому в своём доме, если бы начинали заново?» – инсайты.",
        "4. «Какой современный тренд в строительстве вы уже применили?»"
    ]
    random.shuffle(comments_themes)
    themes = "\n".join(comments_themes[:3])

    base_hashtags = ["#строительство", "#ремонт", "#дом", "#архитектура", "#стройматериалы", "#дизайн", "#инновации"]
    extra = [f"#{topic.replace(' ', '').lower()}" for _ in range(3)]
    hashtags = list(set(base_hashtags + extra))[:10]
    hashtag_str = " ".join(hashtags)

    post = f"{hook}\n\n{lead}\n\n{body}\n\n{conclusion}\n\n{cta}\n\nТемы для обсуждения:\n{themes}\n\n{hashtag_str}"
    return post

# ---------- ГЛАВНАЯ ФУНКЦИЯ ГЕНЕРАЦИИ ----------
def generate_text(topic: str) -> str:
    # С вероятностью 50% выбираем игровой формат
    if random.random() < 0.5:
        game_type = random.choice(["quiz", "poll", "challenge", "riddle"])
        if game_type == "quiz":
            return generate_construction_quiz(topic)
        elif game_type == "poll":
            return generate_construction_poll(topic)
        elif game_type == "challenge":
            return generate_construction_challenge(topic)
        elif game_type == "riddle":
            return generate_construction_riddle(topic)
    else:
        # Обычный пост
        return generate_standard_post(topic)

# ---------- КАРТИНКИ (ЯРКИЕ, СТРОИТЕЛЬНЫЕ) ----------
def download_image_with_retry(url, timeout=60, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.warning(f"Попытка {attempt+1} скачать nie powiodła się: {e}")
            time.sleep(2)
    return None

def is_valid_image(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()
        return True
    except Exception:
        return False

def sharpen_image(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(1.8)
        buf = io.BytesIO()
        img.save(buf, format='PNG', quality=95)
        return buf.getvalue()
    except:
        return image_bytes

def generate_agnes_image(prompt):
    if not AGNES_API_KEY:
        return None
    try:
        seed = random.randint(1, 1000000)
        full_prompt = f"Bright, modern construction site, architecture, building, high quality, no people, no dark"
        headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
        data = {
            "prompt": full_prompt,
            "negative_prompt": "dark, gloomy, sad, depressive, ugly, deformed, blurry, low quality, people, human, woman, girl",
            "width": 1280,
            "height": 1280,
            "num_inference_steps": 45,
            "guidance_scale": 7.5,
            "seed": seed
        }
        resp = requests.post("https://apihub.agnes-ai.cn/v1/images/generations", json=data, headers=headers, timeout=180)
        if resp.status_code == 200:
            result = resp.json()
            image_url = result.get("data", [{}])[0].get("url")
            if image_url:
                img = download_image_with_retry(image_url)
                if img and is_valid_image(img):
                    return img
    except Exception as e:
        logger.warning(f"Agnes не сработал: {e}")
    return None

def generate_pixazo_image(prompt):
    if not PIXAZO_API_KEY:
        return None
    try:
        seed = random.randint(1, 1000000)
        full_prompt = f"Bright, modern construction site, architecture, building, high quality, no people, no dark"
        url = "https://api.pixazo.com/v1/generate"
        headers = {
            "Authorization": f"Bearer {PIXAZO_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "prompt": full_prompt,
            "model": "flux",
            "width": 1280,
            "height": 1280,
            "num_inference_steps": 45,
            "guidance_scale": 7.5,
            "seed": seed
        }
        resp = requests.post(url, json=data, headers=headers, timeout=180)
        if resp.status_code == 200:
            result = resp.json()
            image_url = result.get("image_url")
            if image_url:
                img = download_image_with_retry(image_url)
                if img and is_valid_image(img):
                    return img
    except Exception as e:
        logger.warning(f"Pixazo не сработал: {e}")
    return None

def generate_pollinations_image(prompt):
    try:
        seed = random.randint(1, 1000000)
        full_prompt = f"Bright construction site, modern architecture, building, high quality, no people, no dark"
        url = f"{POLLINATIONS_BASE_URL}/prompt/{requests.utils.quote(full_prompt)}?width=1280&height=1280&nologo=true&seed={seed}&model=flux&upscale=true"
        img = download_image_with_retry(url)
        if img and is_valid_image(img):
            return img
    except Exception as e:
        logger.warning(f"Pollinations не сработал: {e}")
    return None

def search_pexels_relevant_photo(topic):
    if not PEXELS_API_KEY:
        return None
    queries = [
        f"construction {topic}",
        f"building {topic}",
        f"architecture {topic}",
        f"modern house {topic}",
        f"renovation {topic}"
    ]
    random.shuffle(queries)
    for query in queries[:2]:
        url = "https://api.pexels.com/v1/search"
        headers = {"Authorization": PEXELS_API_KEY}
        params = {"query": query, "per_page": 3, "orientation": "landscape"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                photos = data.get("photos", [])
                if photos:
                    photo_url = random.choice(photos)["src"]["large2x"]
                    img = download_image_with_retry(photo_url)
                    if img and is_valid_image(img):
                        return img
        except:
            pass
    return None

def generate_image(topic):
    clean_used_images()
    generators = [
        ("Agnes", generate_agnes_image),
        ("Pixazo", generate_pixazo_image),
        ("Pollinations", generate_pollinations_image)
    ]
    random.shuffle(generators)

    for name, func in generators:
        img = func(topic)
        if img:
            img = sharpen_image(img)
            if not is_image_used(img):
                mark_image_as_used(img)
                logger.info(f"✅ Картинка сгенерирована через {name} (яркая, строительная)")
                return img, name
            else:
                logger.info(f"⚠️ Картинка от {name} уже использовалась, пробуем следующий")

    pexel_img = search_pexels_relevant_photo(topic)
    if pexel_img:
        pexel_img = sharpen_image(pexel_img)
        if not is_image_used(pexel_img):
            mark_image_as_used(pexel_img)
            logger.info("✅ Картинка от Pexels")
            return pexel_img, "Pexels"
        else:
            logger.info("⚠️ Картинка от Pexels уже использовалась")

    banner = create_banner(topic[:20])
    banner = sharpen_image(banner)
    if not is_image_used(banner):
        mark_image_as_used(banner)
        logger.info("✅ Использован баннер")
        return banner, "баннер"
    else:
        banner2 = create_banner(topic[:15] + str(random.randint(1, 100)))
        banner2 = sharpen_image(banner2)
        mark_image_as_used(banner2)
        logger.info("✅ Использован баннер с суффиксом")
        return banner2, "баннер"

def create_banner(text, width=1024, height=1024):
    img = Image.new('RGB', (width, height), color='#1a3a5c')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
    except:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (width - tw) // 2
    y = (height - th) // 2
    draw.text((x, y), text, fill='#FFD700', font=font)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()

# ---------- ЗАГРУЗКА ФОТО ----------
def upload_photo_to_vk_via_http(image_bytes, owner_id, token):
    try:
        vk = vk_api.VkApi(token=token)
        if owner_id < 0:
            group_id = abs(owner_id)
            upload_url = vk.method('photos.getWallUploadServer', {'group_id': group_id})['upload_url']
        else:
            upload_url = vk.method('photos.getWallUploadServer', {})['upload_url']
        files = {'photo': ('image.jpg', image_bytes, 'image/jpeg')}
        resp = requests.post(upload_url, files=files, timeout=30)
        resp.raise_for_status()
        upload_data = resp.json()
        if 'photo' not in upload_data or 'server' not in upload_data or 'hash' not in upload_data:
            logger.error(f"Неполный ответ сервера загрузки: {upload_data}")
            return None
        save_params = {
            'photo': upload_data['photo'],
            'server': upload_data['server'],
            'hash': upload_data['hash']
        }
        if owner_id < 0:
            save_params['group_id'] = abs(owner_id)
        saved = vk.method('photos.saveWallPhoto', save_params)
        photo = saved[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        logger.info(f"Получен attachment: {attachment}")
        return attachment
    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        return None

# ---------- ПУБЛИКАЦИЯ ----------
def publish_to_group(text, image_bytes):
    if not VK_TOKEN_AI or not GROUP_ID_AI:
        return "❌ Нет VK токена или ID"
    try:
        attachments = []
        if image_bytes:
            attachment = upload_photo_to_vk_via_http(image_bytes, GROUP_ID_AI, VK_TOKEN_AI)
            if attachment:
                attachments.append(attachment)
                logger.info("Фото успешно загружено в группу")
            else:
                logger.warning("Не удалось загрузить фото в группу, публикуем без фото")
        params = {
            "access_token": VK_TOKEN_AI,
            "owner_id": GROUP_ID_AI,
            "message": text,
            "v": "5.131"
        }
        if GROUP_ID_AI < 0:
            params["from_group"] = 1
        if attachments:
            params["attachments"] = ",".join(attachments)
        resp = requests.get("https://api.vk.com/method/wall.post", params=params, timeout=30)
        try:
            result = resp.json()
        except json.JSONDecodeError:
            logger.error(f"Ошибка парсинга JSON. Код статуса: {resp.status_code}, ответ: {resp.text[:200]}")
            return f"❌ Ошибка API VK: невалидный ответ (код {resp.status_code})"
        if "error" in result:
            return f"❌ Ошибка VK: {result['error']['error_msg']}"
        return f"✅ Пост в группе опубликован (id: {result['response']['post_id']})"
    except Exception as e:
        logger.error(f"Ошибка публикации в группу: {e}")
        return f"❌ Ошибка: {e}"

def publish_announce_to_user(announce_text, image_bytes, group_link):
    if not VK_TOKEN_USER or not VK_USER_ID:
        return "❌ Нет токена для личной стены"
    try:
        attachments = []
        if image_bytes:
            attachment = upload_photo_to_vk_via_http(image_bytes, VK_USER_ID, VK_TOKEN_USER)
            if attachment:
                attachments.append(attachment)
                logger.info("Фото загружено на личную стену")
            else:
                logger.warning("Не удалось загрузить фото на личную стену")
        full_text = announce_text + f"\n\n👉 {group_link}" if group_link else announce_text
        params = {
            "access_token": VK_TOKEN_USER,
            "owner_id": VK_USER_ID,
            "message": full_text,
            "v": "5.131"
        }
        if attachments:
            params["attachments"] = ",".join(attachments)
        resp = requests.get("https://api.vk.com/method/wall.post", params=params, timeout=30)
        try:
            result = resp.json()
        except json.JSONDecodeError:
            logger.error(f"Ошибка парсинга JSON. Код статуса: {resp.status_code}, ответ: {resp.text[:200]}")
            return f"❌ Ошибка API VK: невалидный ответ (код {resp.status_code})"
        if "error" in result:
            return f"❌ Ошибка VK (личная): {result['error']['error_msg']}"
        return f"✅ Анонс опубликован (id: {result['response']['post_id']})"
    except Exception as e:
        logger.error(f"Ошибка публикации на личную стену: {e}")
        return f"❌ Ошибка: {e}"

# ---------- СОЗДАНИЕ ПОСТА ----------
def create_post_content(title, text=None):
    if text and len(text) > 50:
        post_text = None
        if AGNES_API_KEY:
            try:
                headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
                prompt = f"Перепиши следующий текст, чтобы он стал bardziej żywy, добавь emoji, akapity, сделай go jak post popularnego blogera o строительстве. Текст:\n\n{text}"
                data = {
                    "model": "agnes-v1",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 600,
                    "temperature": 0.8
                }
                resp = requests.post("https://apihub.agnes-ai.cn/v1/chat/completions", json=data, headers=headers, timeout=30)
                if resp.status_code == 200:
                    rewritten = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                    if rewritten and len(rewritten) > 100:
                        post_text = rewritten.strip()
                        logger.info("✅ Рерайт через Agnes выполнен")
            except Exception as e:
                logger.warning(f"Рерайт не удался: {e}")
        if not post_text:
            post_text = text
    else:
        post_text = generate_text(title)

    teaser = post_text[:150]
    if len(post_text) > 150:
        teaser += "..."
    lines = post_text.split('\n')
    if lines and len(lines[0]) < 150:
        teaser = lines[0] + "..."

    announce_text = f"🔥 Новый пост в группе {RSS_DEFAULT_GROUP}\n\n{teaser}\n\n➡️ Читать полностью и обсудить в группе:"
    group_link = f"https://vk.com/club{abs(GROUP_ID_AI)}" if GROUP_ID_AI < 0 else f"https://vk.com/public{GROUP_ID_AI}"
    image_bytes, source = generate_image(title)
    return post_text, announce_text, group_link, image_bytes, source

def publish_post_item(title, text=None):
    post_text, announce_text, group_link, image_bytes, source = create_post_content(title, text)
    group_res = publish_to_group(post_text, image_bytes)
    user_res = publish_announce_to_user(announce_text, image_bytes, group_link)
    return {"group": group_res, "user": user_res, "source": source}

# ---------- ПОЛУЧЕНИЕ СЛЕДУЮЩЕГО ПОСТА ----------
POSTS_POOL = None

def build_posts_pool():
    global POSTS_POOL
    posts = load_posts_from_file()
    if posts:
        POSTS_POOL = posts
        logger.info("📚 Используем посты z pliku posts.txt")
        return

    if RSS_ENABLED and RSS_SOURCES_JSON and RSS_SOURCES_JSON != '[]':
        entries = get_rss_entries(RSS_SOURCES_JSON)
        if entries:
            POSTS_POOL = [{'title': t, 'text': None} for t in entries]
            logger.info(f"📚 Используем RSS-заголовки: {len(POSTS_POOL)} тем")
            return

    topics = load_topics()
    POSTS_POOL = [{'title': t, 'text': None} for t in topics]
    logger.info(f"📚 Используем запасной список тем: {len(POSTS_POOL)}")

def get_posts_pool():
    global POSTS_POOL
    if POSTS_POOL is None:
        build_posts_pool()
    return POSTS_POOL

def get_next_post():
    pool = get_posts_pool()
    if not pool:
        return None, None
    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    used_indices = state.get(today, [])
    available = [i for i in range(len(pool)) if i not in used_indices]
    if not available:
        available = list(range(len(pool)))
        used_indices = []
    idx = random.choice(available)
    used_indices.append(idx)
    state[today] = used_indices
    save_state(state)
    post = pool[idx]
    return post['title'], post['text']

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ---------- ПЛАНИРОВЩИК ----------
def scheduled_post():
    logger.info("⏰ Автопостинг (каждые 6 часов)")
    try:
        title, text = get_next_post()
        if not title:
            logger.warning("Нет доступных постов dla publikacji")
            return
        result = publish_post_item(title, text)
        logger.info(f"Результат: {result}")
    except Exception as e:
        logger.error(f"Ошибка автопостинга: {e}")

def scheduler_worker():
    logger.info("📡 Планировщик запущен (4 поста в сутки)")
    scheduled_post()
    schedule.every(6).hours.do(scheduled_post)
    while True:
        schedule.run_pending()
        time.sleep(60)

# ---------- КОМАНДЫ ----------
def handle_command(chat_id, text):
    if text in ("/start", "/help"):
        send_message(chat_id,
            "🏗️ Бот «Строительный навигатор» – разнообразные посты + викторины!\n"
            "📌 Команды:\n"
            "/post <заголовок> — сгенерировать пост\n"
            "/post <текст (длиннее 50 символов)> — опубликовать с рерайтом\n"
            "/ping — проверка\n"
            "/status — статистика"
        )
        return
    if text == "/ping":
        send_message(chat_id, "🏓 Pong!")
        return
    if text == "/status":
        state = load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        used = state.get(today, [])
        total = len(get_posts_pool())
        send_message(chat_id, f"📊 Сегодня опубликовано {len(used)} постов из {total}.")
        return
    if text.startswith("/post"):
        content = text.replace("/post", "").strip()
        if not content:
            send_message(chat_id, "❌ Укажите тему или готовый текст.")
            return
        if len(content) > 50:
            title = content[:50] + "..."
            result = publish_post_item(title, content)
        else:
            result = publish_post_item(content)
        send_message(chat_id, f"📌 Группа: {result['group']}\n👤 Анонс: {result['user']}")
        return
    send_message(chat_id, "❓ Неизвестная команда. Напишите /help")

# ---------- ЗАПУСК ----------
def main():
    logger.info("🚀 Строительный навигатор запущен (викторины, опросы, разнообразные посты)")
    threading.Thread(target=scheduler_worker, daemon=True).start()
    last_update_id = 0
    while True:
        try:
            updates = get_updates(offset=last_update_id + 1)
            if updates:
                for update in updates:
                    last_update_id = update["update_id"]
                    if "message" in update:
                        msg = update["message"]
                        chat_id = msg["chat"]["id"]
                        if "text" in msg:
                            handle_command(chat_id, msg["text"].strip())
            time.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()