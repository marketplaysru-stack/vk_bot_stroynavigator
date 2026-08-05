#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Строительный навигатор – генерация постов по схеме: Хук → Лид → Тело → Вывод → CTA → Темы → Хештеги.
Картинки генерируются через нейросети (Agnes → Pixazo → Pollinations) с яркими, привлекательными промптами.
Кэш использованных картинок предотвращает дубли.
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
from datetime import datetime
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
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
IMAGE_NEGATIVE_PROMPT = os.getenv("IMAGE_NEGATIVE_PROMPT", "ugly, deformed, blurry, low quality, same face, boring, plain, cartoon, doll, mannequin, 3d render, smooth skin, unrealistic, extra limbs, bad anatomy, distorted, people, human, woman, girl, beach, sea, sand, swimsuit, nude, naked, portrait, selfie, smile, face, eyes, hair, meadow, field, hay, grass, farm, cow, horse, rural, village, landscape, trees, forest, nature, road, mountains, countryside, plants, outdoor, dark, gloomy")

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

# ---------- КЭШ ИСПОЛЬЗОВАННЫХ КАРТИНОК ----------
def load_used_images():
    if os.path.exists(USED_IMAGES_FILE):
        with open(USED_IMAGES_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_used_images(used_set):
    with open(USED_IMAGES_FILE, "w") as f:
        json.dump(list(used_set), f)

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

# ---------- ЗАПАСНЫЕ ТЕМЫ ----------
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
        "Инженерные системы в доме"
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

# ---------- ГЕНЕРАЦИЯ ТЕКСТА (улучшенный шаблон) ----------
def generate_text(topic: str) -> str:
    if AGNES_API_KEY:
        try:
            headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
            prompt = f"""
Напиши развернутый пост (около 250–300 слов) на тему: {topic} (строительство, ремонт, архитектура).
Пост должен быть полезным, содержать факты, примеры из жизни, практические советы и вопросы к читателям.
Структура:
1. Хук – цепляющий заголовок (5–7 слов) с эмодзи.
2. Вступление – 3–4 предложения, которые вводят в тему.
3. Основной блок – 3–5 пунктов с подзаголовками (краткими) и пояснениями (2–3 предложения каждый).
4. Вывод – 2–3 предложения с итогом и вдохновением.
5. CTA – вопрос к аудитории, вовлекающий в обсуждение.
6. Темы для комментариев – 3 вопроса.
7. Хештеги – 7–10 штук.

Пост должен быть живым, человечным, без канцелярита. Используй эмодзи, абзацы.
"""
            data = {
                "model": "agnes-v1",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 800,
                "temperature": 0.85
            }
            resp = requests.post("https://apihub.agnes-ai.cn/v1/chat/completions", json=data, headers=headers, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                if text and len(text) > 100:
                    return text.strip()
        except Exception as e:
            logger.warning(f"Agnes не сработал: {e}")

    return generate_template_text(topic)

def generate_template_text(topic: str) -> str:
    hooks = [
        f"🏗️ {topic.upper()} – ОСНОВА ВАШЕГО ДОМА",
        f"🔨 {topic.upper()} – КАК НЕ ПОТЕРЯТЬ КАЧЕСТВО",
        f"🏠 {topic.upper()} – ВАШ ДОМ ЗАСЛУЖИВАЕТ ЛУЧШЕГО",
        f"😤 {topic.upper()} – ОШИБКИ, КОТОРЫЕ ДОРОГО СТОЯТ",
        f"🚀 {topic.upper()} – ТЕХНОЛОГИИ, КОТОРЫЕ МЕНЯЮТ ИГРУ"
    ]
    hook = random.choice(hooks)

    leads = [
        f"Вы когда-нибудь задумывались, почему один дом стоит 20 лет, а другой – 50? Всё дело в материале и подходе. {topic} – это не просто выбор, это инвестиция в будущее.",
        f"Ремонт – это всегда стресс. Но что, если я скажу, что 80% проблем можно избежать, если знать несколько секретов? {topic} – это ключ к спокойствию.",
        f"Строительство – это не только про бетон и кирпичи. Это про вашу безопасность, комфорт и уют. {topic} – основа надёжного дома."
    ]
    lead = random.choice(leads)

    body_pool = [
        ("🏗️", "Выбор материала", "Газобетон, кирпич, дерево – у каждого свои плюсы. Учитывай климат, нагрузку, бюджет. Например, для средней полосы газобетон – отличный выбор по теплоизоляции."),
        ("🔧", "Утепление", "Теплоизоляция – это не только комфорт, но и экономия на отоплении до 40%. Сейчас есть экологичные утеплители, которые служат десятилетиями."),
        ("🛠️", "Качество работ", "Нанять профессионалов или делать самому? Оцените свои силы, чтобы не переделывать. Доверяйте проверенным бригадам или делайте поэтапно."),
        ("📐", "Планировка", "Правильная планировка экономит до 15% площади. Учитывай эргономику и инсоляцию (световой режим)." ),
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
        "Качественный дом строится один раз, но служит поколениям. Не экономьте на главном.",
        "Инвестируйте в надёжность, и ваш дом станет настоящей крепостью для семьи."
    ]
    conclusion = random.choice(conclusions)

    cta_questions = [
        f"👇 А вы уже сталкивались с проблемами при строительстве или ремонте? Как решали?",
        f"👇 Что для вас самое важное при выборе материалов для дома?",
        f"👇 Согласны, что лучше один раз вложиться в качество, чем потом переделывать?"
    ]
    cta = random.choice(cta_questions)

    comments_themes = [
        "1. «Какой материал вы считаете самым надёжным для стен?» – поделитесь опытом.",
        "2. «Как вы экономите на строительстве без потери качества?» – дайте советы.",
        "3. «Что бы вы сделали по-другому в своём доме, если бы начинали заново?» – инсайты."
    ]
    themes = "\n".join(comments_themes)

    base_hashtags = ["#строительство", "#ремонт", "#дом", "#архитектура", "#стройматериалы", "#дизайн", "#инновации"]
    extra = [f"#{topic.replace(' ', '').lower()}" for _ in range(3)]
    hashtags = list(set(base_hashtags + extra))[:10]
    hashtag_str = " ".join(hashtags)

    post = f"{hook}\n\n{lead}\n\n{body}\n\n{conclusion}\n\n{cta}\n\nТемы для обсуждения:\n{themes}\n\n{hashtag_str}"
    return post

# ---------- ГЕНЕРАТОРЫ КАРТИНОК (яркие, привлекательные) ----------
def generate_agnes_image(prompt):
    if not AGNES_API_KEY:
        return None
    try:
        seed = random.randint(1, 1000000)
        headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
        # Яркий, позитивный, красивый промпт
        full_prompt = f"Bright, colorful, beautiful illustration about {prompt}, modern architecture, cozy home, sunny day, high quality, no people, no nature, no dark, no gloomy"
        data = {
            "prompt": full_prompt,
            "negative_prompt": "dark, gloomy, ugly, deformed, blurry, low quality, people, human, woman, girl, nature, trees, landscape, sad",
            "width": 1024,
            "height": 1024,
            "num_inference_steps": 30,
            "guidance_scale": 7.0,
            "seed": seed
        }
        resp = requests.post("https://apihub.agnes-ai.cn/v1/images/generations", json=data, headers=headers, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            image_url = result.get("data", [{}])[0].get("url")
            if image_url:
                img_resp = requests.get(image_url, timeout=30)
                if img_resp.status_code == 200:
                    return img_resp.content
    except Exception as e:
        logger.warning(f"Agnes генерация не сработала: {e}")
    return None

def generate_pixazo_image(prompt):
    if not PIXAZO_API_KEY:
        return None
    try:
        seed = random.randint(1, 1000000)
        url = "https://api.pixazo.com/v1/generate"
        headers = {
            "Authorization": f"Bearer {PIXAZO_API_KEY}",
            "Content-Type": "application/json"
        }
        full_prompt = f"Bright, colorful, beautiful illustration about {prompt}, modern architecture, cozy home, sunny day, high quality, no people, no nature, no dark, no gloomy"
        data = {
            "prompt": full_prompt,
            "model": "flux",
            "width": 1024,
            "height": 1024,
            "num_inference_steps": 30,
            "guidance_scale": 7.0,
            "seed": seed
        }
        resp = requests.post(url, json=data, headers=headers, timeout=60)
        if resp.status_code == 200:
            result = resp.json()
            image_url = result.get("image_url")
            if image_url:
                img_resp = requests.get(image_url, timeout=30)
                if img_resp.status_code == 200:
                    return img_resp.content
    except Exception as e:
        logger.warning(f"Pixazo не сработал: {e}")
    return None

def generate_pollinations_image(prompt):
    try:
        seed = random.randint(1, 1000000)
        full_prompt = f"Bright, colorful, beautiful {prompt}, modern architecture, cozy home, sunny day, high quality, no people, no nature, no dark, no gloomy"
        url = f"{POLLINATIONS_BASE_URL}/prompt/{requests.utils.quote(full_prompt)}?width=1024&height=1024&nologo=true&seed={seed}&model=flux"
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        logger.warning(f"Pollinations не сработал: {e}")
    return None

def search_pexels_relevant_photo(topic):
    if not PEXELS_API_KEY:
        return None
    # Расширенный список ярких, красивых запросов
    queries = [
        f"beautiful modern house {topic}",
        f"cozy home interior {topic}",
        f"bright construction site {topic}",
        f"colorful architecture {topic}",
        f"sunny building {topic}",
        f"modern design {topic}",
        f"renovation bright {topic}",
        f"new home construction {topic}",
        f"luxury house {topic}",
        f"interior design bright {topic}"
    ]
    random.shuffle(queries)
    for query in queries[:3]:
        url = "https://api.pexels.com/v1/search"
        headers = {"Authorization": PEXELS_API_KEY}
        params = {"query": query, "per_page": 3, "orientation": "landscape"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                photos = data.get("photos", [])
                if photos:
                    # Берём случайное фото из результатов
                    return random.choice(photos)["src"]["large2x"]
        except:
            pass
    return None

def download_photo(url):
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content
    except:
        pass
    return None

def generate_image(topic):
    """
    Генерирует уникальную яркую картинку по теме поста.
    Перебирает генераторы, пока не найдёт уникальное изображение.
    """
    generators = [
        ("Agnes", generate_agnes_image),
        ("Pixazo", generate_pixazo_image),
        ("Pollinations", generate_pollinations_image)
    ]
    random.shuffle(generators)

    for name, func in generators:
        img = func(topic)
        if img:
            if not is_image_used(img):
                mark_image_as_used(img)
                logger.info(f"✅ Картинка сгенерирована через {name} (уникальная, яркая)")
                return img, name
            else:
                logger.info(f"⚠️ Картинка от {name} уже использовалась, пробуем следующий генератор")

    if PEXELS_API_KEY:
        photo_url = search_pexels_relevant_photo(topic)
        if photo_url:
            img = download_photo(photo_url)
            if img and not is_image_used(img):
                mark_image_as_used(img)
                logger.info("✅ Картинка от Pexels (уникальная, яркая)")
                return img, "Pexels"
            elif img:
                logger.info("⚠️ Картинка от Pexels уже использовалась")

    img = create_banner(topic[:20])
    if not is_image_used(img):
        mark_image_as_used(img)
        logger.info("✅ Использован баннер (уникальный)")
        return img, "баннер"
    else:
        img = create_banner(topic[:15] + str(random.randint(1, 100)))
        mark_image_as_used(img)
        logger.info("✅ Использован баннер с рандомным суффиксом")
        return img, "баннер"

def create_banner(text, width=1024, height=1024):
    img = Image.new('RGB', (width, height), color='#FFA500')  # яркий оранжевый фон
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
    draw.text((x, y), text, fill='#FFFFFF', font=font)  # белый текст
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()

# ---------- ЗАГРУЗКА ФОТО (ПРЯМОЙ HTTP) ----------
def upload_photo_to_vk_via_http(image_bytes, owner_id, token):
    try:
        vk = vk_api.VkApi(token=token)
        if owner_id < 0:
            group_id = abs(owner_id)
            upload_url = vk.method('photos.getWallUploadServer', {'group_id': group_id})['upload_url']
            logger.info(f"Загрузка в группу {group_id}")
        else:
            upload_url = vk.method('photos.getWallUploadServer', {})['upload_url']
            logger.info(f"Загрузка на публичную страницу/личную стену (owner_id={owner_id})")

        files = {'photo': ('image.jpg', image_bytes, 'image/jpeg')}
        resp = requests.post(upload_url, files=files)
        resp.raise_for_status()
        upload_data = resp.json()
        if 'photo' not in upload_data or 'server' not in upload_data or 'hash' not in upload_data:
            logger.error(f"Неполный ответ: {upload_data}")
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

# ---------- VK ПУБЛИКАЦИЯ ----------
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
                logger.warning("Не удалось загрузить фото в группу, попробуем без фото")
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
        resp = requests.get("https://api.vk.com/method/wall.post", params=params).json()
        if "error" in resp:
            return f"❌ Ошибка VK: {resp['error']['error_msg']}"
        return f"✅ Пост в группе опубликован (id: {resp['response']['post_id']})"
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
        resp = requests.get("https://api.vk.com/method/wall.post", params=params).json()
        if "error" in resp:
            return f"❌ Ошибка VK (личная): {resp['error']['error_msg']}"
        return f"✅ Анонс опубликован (id: {resp['response']['post_id']})"
    except Exception as e:
        logger.error(f"Ошибка публикации на личную стену: {e}")
        return f"❌ Ошибка: {e}"

# ---------- СОЗДАНИЕ ПОСТА С ТИЗЕРОМ ----------
def create_post_content(title, text=None):
    if text and len(text) > 50:
        post_text = None
        if AGNES_API_KEY:
            try:
                headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
                prompt = f"Перепиши следующий текст, чтобы он стал более żywy, добавь emoji, абзацы, сделай его jak пост популярного блогера о строительстве. Текст:\n\n{text}"
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

    announce_text = f"🏗️ Новый пост в группе {RSS_DEFAULT_GROUP}\n\n{teaser}\n\n➡️ Читать полностью и обсудить в группе:"
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
        logger.info("📚 Используем посты из файла posts.txt")
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

# ---------- СОСТОЯНИЕ ----------
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
            logger.warning("Нет доступных постов для публикации")
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
            "🏗️ Бот «Строительный навигатор» – посты по схеме!\n"
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

# ---------- ЗАПУСК ----------
def main():
    logger.info("🚀 Бот запущен")
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