import sys
import os
import requests
import json
import urllib.parse
import threading
import time
import re
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# ===== ПРИНУДИТЕЛЬНЫЙ ВЫВОД ЛОГОВ =====
sys.stdout.reconfigure(line_buffering=True)

# ===== НАСТРОЙКА ЛОГГИРОВАНИЯ =====
DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "bot_builder.log")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

def log(msg):
    logging.info(msg)

# ===== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ =====
BOT_TOKEN = os.getenv("BOT_TOKEN_NEW")               # Токен Telegram-бота
VK_TOKEN = os.getenv("VK_TOKEN_BUILDER")             # Токен строительной группы
VK_GROUP_ID = os.getenv("VK_GROUP_ID_BUILDER")       # -239598146
AGNES_API_KEY = os.getenv("AGNES_API_KEY")
GIGACHAT_API_KEY = os.getenv("GIGACHAT_API_KEY")     # Должен быть задан для приоритета
PORT = int(os.getenv("PORT", 8082))

if not BOT_TOKEN:
    log("❌ BOT_TOKEN_NEW не задан")
    sys.exit(1)
if not VK_TOKEN:
    log("❌ VK_TOKEN_BUILDER не задан")
    sys.exit(1)
if not VK_GROUP_ID:
    log("❌ VK_GROUP_ID_BUILDER не задан")
    sys.exit(1)
try:
    VK_GROUP_ID = int(VK_GROUP_ID)
except ValueError:
    log(f"❌ VK_GROUP_ID_BUILDER должен быть числом, получено: {VK_GROUP_ID}")
    sys.exit(1)
if not AGNES_API_KEY:
    log("⚠️ AGNES_API_KEY не задан (картинки через Pollinations)")
if not GIGACHAT_API_KEY:
    log("⚠️ GIGACHAT_API_KEY не задан (GigaChat не будет использоваться)")

log("🚀 Запуск бота для Строительного навигатора (приоритет GigaChat)")
log(f"📌 Группа ID: {VK_GROUP_ID}")

SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule.json")
log(f"📂 Файл расписания: {SCHEDULE_FILE}")

# ===== ПРОВЕРКА ПРАВ ТОКЕНА VK =====
def check_vk_token_permissions():
    log("🔍 Проверка прав токена VK...")
    try:
        resp = requests.get(
            "https://api.vk.com/method/photos.getWallUploadServer",
            params={"group_id": abs(VK_GROUP_ID), "access_token": VK_TOKEN, "v": "5.131"},
            timeout=10
        )
        if resp.status_code != 200:
            log(f"⚠️ Не удалось проверить права: HTTP {resp.status_code}")
            return False
        data = resp.json()
        if "error" in data:
            if data["error"]["error_code"] == 27:
                log("❌ Токен НЕ имеет права 'photos'! Бот будет публиковать без фото.")
                return False
            else:
                log(f"⚠️ Ошибка при проверке прав: {data['error']['error_msg']}")
                return False
        log("✅ Токен имеет право 'photos'.")
        return True
    except Exception as e:
        log(f"⚠️ Исключение при проверке прав: {e}")
        return False

HAS_PHOTO_PERMISSION = check_vk_token_permissions()
if not HAS_PHOTO_PERMISSION:
    log("⚠️ Бот будет публиковать только текст (без фото) из-за отсутствия прав.")

# ===== Health-сервер =====
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    server.serve_forever()

health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()
log(f"🟢 Health-сервер запущен (порт {PORT})")

# ===== ПРОВЕРКА TELEGRAM =====
try:
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10)
    if r.status_code == 200:
        bot_info = r.json()["result"]
        log(f"✅ Подключение к Telegram: @{bot_info['username']}")
    else:
        log(f"❌ Ошибка доступа к Telegram: {r.status_code}")
        sys.exit(1)
except Exception as e:
    log(f"❌ Не удалось подключиться к Telegram: {e}")
    sys.exit(1)

try:
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
    log("✅ Вебхук удалён")
except Exception as e:
    log(f"⚠️ Ошибка удаления вебхука: {e}")

# ===== ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ RETRY (с увеличенными таймаутами) =====
def retry_call(func, *args, max_retries=3, delay=2, backoff=2, **kwargs):
    last_exception = None
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
                if not result[0] and attempt < max_retries - 1:
                    raise Exception(f"Function returned failure: {result[1]}")
                return result
            if isinstance(result, dict) and result.get('error'):
                raise Exception(result['error'].get('error_msg', 'Unknown API error'))
            return result
        except Exception as e:
            last_exception = e
            log(f"   ⚠️ Попытка {attempt+1}/{max_retries} не удалась: {e}")
            if attempt < max_retries - 1:
                sleep_time = delay * (backoff ** attempt)
                log(f"   ⏳ Повтор через {sleep_time:.1f} сек...")
                time.sleep(sleep_time)
            else:
                log(f"   ❌ Все {max_retries} попыток провалились")
    raise last_exception

# ===== РАБОТА С РАСПИСАНИЕМ =====
def load_schedule():
    try:
        if os.path.exists(SCHEDULE_FILE):
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                log(f"📂 Загружено {len(data)} записей из {SCHEDULE_FILE}")
                return data
        else:
            log(f"📂 Файл {SCHEDULE_FILE} не найден, создаём новый")
            save_schedule([])
            return []
    except Exception as e:
        log(f"⚠️ Ошибка загрузки: {e}")
        return []

def save_schedule(schedule):
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)
        log(f"💾 Сохранено {len(schedule)} записей в {SCHEDULE_FILE}")
    except Exception as e:
        log(f"⚠️ Ошибка сохранения: {e}")

# ===== ГЕНЕРАЦИЯ ТЕКСТА =====
def generate_post_text(topic):
    log(f"🔤 Генерация текста для темы: {topic}")
    system_prompt = (
        "Ты — профессиональный SMM-менеджер и копирайтер. "
        "Напиши яркий, вовлекающий пост для ВКонтакте по заданной теме. "
        "Пост должен быть продающим, полезным и побуждать к действию. "
        "Используй структуру: цепляющий заголовок (до 10 слов) → проблема аудитории → решение → практическая польза → призыв к действию. "
        "Добавь эмодзи, разбей на короткие абзацы. В конце добавь 5 хештегов. Пиши человечно, без канцелярита."
    )
    user_prompt = f"Тема: {topic}"
    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "agnes-2.0-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.85
    }
    def _do():
        response = requests.post(
            "https://apihub.agnes-ai.com/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=120
        )
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text[:200]}")
        result = response.json()
        return result["choices"][0]["message"]["content"]
    try:
        text = retry_call(_do, max_retries=3, delay=2, backoff=2)
        log(f"   Текст получен, длина {len(text)}")
        return text
    except Exception as e:
        log(f"   ❌ Генерация текста провалилась: {e}")
        return None

# ============================================================
# ===== УЛУЧШЕННЫЙ ПРОМПТ ДЛЯ КАРТИНОК =====
# ============================================================

def build_image_prompt(topic):
    base = (
        f"Hyperrealistic cinematic photograph, square 1:1 format, {topic}. "
        "No text, no typography, no words, no letters, no numbers on the image. "
        "May include stylized icons, logos, geometric shapes, abstract patterns, "
        "branding elements, arrows, badges, or graphic overlays for visual appeal. "
        "Professionally styled composition, dramatic high-contrast lighting, "
        "cinematic color grading (rich reds, deep blues, warm golden highlights), "
        "shallow depth of field, sharp focus on the main subject, "
        "ultra-detailed textures (skin pores, fabric weaves, reflections, materials), "
        "8K resolution, photorealistic, editorial quality, "
        "reminiscent of high-end advertising or fashion photography, "
        "emotionally compelling, vibrant yet natural colors, "
        "background softly blurred with bokeh, spotlight effect, "
        "modern aesthetic, perfect for social media cover, "
        "professional retouching, no plastic or artificial look, "
        "captured with Hasselblad H6D, 100mm lens, f/2.8, "
        "natural motion frozen, dynamic energy, "
        "atmospheric haze, subtle lens flare, volumetric light."
    )
    return base

def generate_image_agnes(prompt):
    log("   🖼️ Попытка Agnes (улучшенный промпт)...")
    if not AGNES_API_KEY:
        log("   AGNES_API_KEY не задан")
        return None
    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "agnes-image-2.1-flash",
        "prompt": prompt,
        "size": "1024x1024",
        "n": 1
    }
    def _do():
        response = requests.post(
            "https://apihub.agnes-ai.com/v1/images/generations",
            headers=headers,
            json=data,
            timeout=120
        )
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        json_resp = response.json()
        if not json_resp.get("data") or len(json_resp["data"]) == 0:
            raise Exception("Empty data")
        return json_resp["data"][0]["url"]
    try:
        url = retry_call(_do, max_retries=2, delay=3, backoff=2)
        log("   ✅ Agnes успешно")
        return url
    except Exception as e:
        log(f"   ❌ Agnes окончательно: {e}")
        return None

def generate_image_gigachat(prompt):
    log("   🖼️ Попытка GigaChat (приоритетный)...")
    if not GIGACHAT_API_KEY:
        log("   GIGACHAT_API_KEY не задан")
        return None
    headers = {
        "Authorization": f"Bearer {GIGACHAT_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "GigaChat-Image",
        "prompt": prompt,
        "size": "1024x1024",
        "n": 1
    }
    def _do():
        response = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/images/generations",
            headers=headers,
            json=data,
            timeout=120
        )
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        json_resp = response.json()
        if not json_resp.get("data") or len(json_resp["data"]) == 0:
            raise Exception("Empty data")
        return json_resp["data"][0]["url"]
    try:
        url = retry_call(_do, max_retries=2, delay=3, backoff=2)
        log("   ✅ GigaChat успешно")
        return url
    except Exception as e:
        log(f"   ❌ GigaChat окончательно: {e}")
        return None

def generate_image_pollinations(prompt):
    log("   🖼️ Попытка Pollinations (улучшенный промпт)...")
    try:
        short_prompt = prompt[:250] + " photorealistic, high quality, 1:1"
        prompt_encoded = urllib.parse.quote(short_prompt)
        url = f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=1024&height=1024&nologo=true"
        log("   ✅ URL сформирован")
        return url
    except Exception as e:
        log(f"   ❌ Pollinations исключение: {e}")
        return None

def download_image(url):
    log(f"📥 Скачивание картинки: {url[:60]}...")
    def _do():
        response = requests.get(url, timeout=120)  # увеличен таймаут
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        content = response.content
        if b"<html" in content[:100] or b"<!DOCTYPE" in content[:100]:
            raise Exception("Получен HTML вместо изображения")
        if len(content) < 100:
            raise Exception("Слишком маленький ответ")
        return content
    try:
        content = retry_call(_do, max_retries=3, delay=2, backoff=2)
        log(f"   Успешно, размер {len(content)} байт")
        return content
    except Exception as e:
        log(f"   ❌ Скачивание провалилось: {e}")
        return None

# ===== ПУБЛИКАЦИЯ В VK (с увеличенными таймаутами) =====
def vk_api_request(method, params, token, retries=3):
    base_url = "https://api.vk.com/method/"
    params = params.copy()
    params["access_token"] = token
    params["v"] = "5.131"
    def _do():
        response = requests.get(base_url + method, params=params, timeout=60)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        json_resp = response.json()
        if "error" in json_resp:
            log(f"   ❌ VK API ошибка в {method}: {json_resp['error']}")
            raise Exception(json_resp["error"]["error_msg"])
        return json_resp["response"]
    try:
        return retry_call(_do, max_retries=retries, delay=2, backoff=2)
    except Exception as e:
        log(f"   ❌ Ошибка VK API ({method}): {e}")
        return None

def post_to_vk(image_bytes, text):
    log(f"📤 Начало публикации в строительную группу (ID {VK_GROUP_ID})")
    group_id = VK_GROUP_ID
    token = VK_TOKEN

    if image_bytes is None:
        log("   Публикация без фото (только текст)")
        result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
        if result is None:
            return False, "Ошибка публикации текста", False
        log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {result['post_id']}")
        return True, None, False

    # Если нет права photos, сразу выходим без фото
    if not HAS_PHOTO_PERMISSION:
        log("   ⚠️ Токен не имеет права 'photos', публикуем без фото")
        result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
        if result is None:
            return False, "Ошибка публикации текста (нет прав photos)", False
        log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {result['post_id']}")
        return True, None, False

    log("   Публикация с фото")
    try:
        log("   Шаг 1: Получение upload_url...")
        upload_resp = vk_api_request("photos.getWallUploadServer", {"group_id": abs(group_id)}, token=token, retries=3)
        if upload_resp is None:
            log("   ❌ Не удалось получить upload_url, публикуем без фото")
            result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
            if result is None:
                return False, "Ошибка публикации после падения upload_url", False
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {result['post_id']}")
            return True, None, False
        upload_url = upload_resp["upload_url"]
        log(f"   upload_url получен: {upload_url[:50]}...")

        log("   Шаг 2: Загрузка фото...")
        def _upload():
            files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
            resp = requests.post(upload_url, files=files, timeout=120)  # увеличен таймаут
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}")
            data = resp.json()
            log(f"   Ответ сервера загрузки: {data}")
            if data.get("error"):
                raise Exception(f"Ошибка загрузки: {data['error']}")
            if not all(k in data for k in ("server", "photo", "hash")):
                raise Exception(f"Неполный ответ: {data}")
            if data.get("photo") == "[]" or not data.get("photo"):
                raise Exception("photo пустое или '[]'")
            return data

        try:
            up = retry_call(_upload, max_retries=3, delay=2, backoff=2)
        except Exception as e:
            log(f"   ❌ Ошибка загрузки фото: {e}, публикуем без фото")
            result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
            if result is None:
                return False, "Ошибка публикации после падения загрузки фото", False
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {result['post_id']}")
            return True, None, False

        log("   Шаг 3: Сохранение фото на стене...")
        save_params = {
            "group_id": abs(group_id),
            "server": up["server"],
            "photo": up["photo"],
            "hash": up["hash"]
        }
        save_resp = vk_api_request("photos.saveWallPhoto", save_params, token=token, retries=3)
        if save_resp is None:
            log("   ❌ Ошибка сохранения фото, публикуем без фото")
            result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
            if result is None:
                return False, "Ошибка публикации после падения сохранения фото", False
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {result['post_id']}")
            return True, None, False

        photo = save_resp[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        log(f"   Фото сохранено, attachment: {attachment}")

        log("   Шаг 4: Публикация поста с фото...")
        post_params = {
            "owner_id": group_id,
            "message": text,
            "attachments": attachment,
            "from_group": 1
        }
        post_resp = vk_api_request("wall.post", post_params, token=token, retries=3)
        if post_resp is None:
            log("   ❌ Ошибка публикации с фото, пробуем без фото")
            result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
            if result is None:
                return False, "Ошибка публикации после падения с фото", True
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {result['post_id']}")
            return True, None, True

        log(f"✅ Пост опубликован с фото в группе {group_id}, ID: {post_resp['post_id']}")
        return True, None, True

    except Exception as e:
        log(f"   Исключение в post_to_vk: {e}")
        traceback.print_exc(file=sys.stdout)
        try:
            result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
            if result is not None:
                log(f"✅ Пост опубликован (без фото) после исключения, ID: {result['post_id']}")
                return True, None, False
        except:
            pass
        return False, f"Исключение: {str(e)}", False

# ===== ВЫПОЛНЕНИЕ ЗАПЛАНИРОВАННОГО ПОСТА (с приоритетом GigaChat) =====
def execute_scheduled_post(item):
    if item.get("niche") != "строительный":
        log(f"⏭️ Пропускаем задание для другой ниши: {item.get('niche')}")
        return

    topic = item["topic"]
    time_str = item["time"]
    log(f"📢 Публикую запланированный пост: '{topic}' в {time_str} (строительный)")

    log("🔤 Шаг 1: Генерация текста...")
    post_text = generate_post_text(topic)
    if not post_text:
        log("❌ Текст не сгенерирован, пропускаем пост")
        return
    log(f"✅ Текст получен, длина {len(post_text)}")

    # Приоритет: GigaChat -> Agnes -> Pollinations
    sources = [
        ("GigaChat", generate_image_gigachat),
        ("Agnes", generate_image_agnes),
        ("Pollinations", generate_image_pollinations)
    ]

    photo_uploaded = False
    success = False
    error = None

    for source_name, gen_func in sources:
        if not gen_func:
            continue
        log(f"🖼️ Шаг 2: Попытка генерации картинки через {source_name}...")
        prompt = build_image_prompt(topic)
        log(f"   Промпт: {prompt[:200]}...")
        image_url = gen_func(prompt)
        if not image_url:
            log(f"   ⚠️ {source_name} не дал URL, переключаемся на следующий источник")
            continue

        log(f"✅ URL картинки от {source_name}: {image_url[:60]}...")
        log("📥 Шаг 3: Скачивание картинки...")
        image_bytes = download_image(image_url)
        if not image_bytes:
            log(f"   ❌ Не удалось скачать картинку от {source_name}, переключаемся на следующий источник")
            continue

        log(f"✅ Картинка скачана, размер {len(image_bytes)} байт")
        log("📤 Шаг 4: Публикация в VK...")
        success, error, photo_uploaded = post_to_vk(image_bytes, post_text)

        if success:
            if photo_uploaded:
                log(f"✅ Пост успешно опубликован с фото от {source_name}!")
            else:
                log(f"✅ Пост опубликован без фото (после проблем с загрузкой) от {source_name}")
            break
        else:
            log(f"   ❌ Публикация с фото от {source_name} не удалась: {error}")
            log(f"   🔄 Переключаемся на следующий источник...")

    if not success:
        log("⚠️ Все источники не дали результат, публикуем без фото")
        success, error, _ = post_to_vk(None, post_text)
        if success:
            log("✅ Пост опубликован без фото (резервный вариант)")
        else:
            log(f"❌ Ошибка публикации без фото: {error}")

# ===== ПЛАНИРОВЩИК =====
def scheduler_loop():
    log("🔄 Планировщик запущен (проверка каждые 30 секунд)")
    while True:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            log(f"⏰ Текущее время: {now}")
            schedule = load_schedule()
            if not schedule:
                log("📭 Расписание пустое")
            else:
                for item in schedule:
                    if item.get("niche") == "строительный" and item["time"] == now and not item.get("done", False):
                        log(f"📢 Найдено задание: {item['topic']} в {item['time']}")
                        execute_scheduled_post(item)
                        item["done"] = True
                        save_schedule(schedule)
        except Exception as e:
            log(f"⚠️ Ошибка в планировщике: {e}")
            traceback.print_exc(file=sys.stdout)
        time.sleep(30)

# ===== ОБРАБОТЧИКИ КОМАНД =====
def process_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    log(f"📩 Получено: {text}")

    if text.startswith("/start"):
        send_message(chat_id,
            "👋 Бот для автопостинга в Строительный навигатор.\n"
            "🎨 Картинки: без текста, с рекламными иконками.\n"
            "🔄 Приоритет: GigaChat -> Agnes -> Pollinations\n"
            "/post_in тема минуты — добавить пост через N минут\n"
            "/run_now тема — опубликовать прямо сейчас\n"
            "/list — показать все задания\n"
            "/debug — показать содержимое schedule.json"
        )
        return

    if text.startswith("/run_now"):
        topic = text.replace("/run_now", "").strip()
        if not topic:
            send_message(chat_id, "❌ Укажи тему поста")
            return
        send_message(chat_id, f"⏳ Начинаю публикацию: '{topic}'...")
        def publish():
            log(f"📢 Ручная публикация (строительный): {topic}")
            post_text = generate_post_text(topic)
            if not post_text:
                send_message(chat_id, "❌ Не удалось сгенерировать текст")
                return

            sources = [
                ("GigaChat", generate_image_gigachat),
                ("Agnes", generate_image_agnes),
                ("Pollinations", generate_image_pollinations)
            ]

            photo_uploaded = False
            success = False
            error = None

            for source_name, gen_func in sources:
                if not gen_func:
                    continue
                log(f"🖼️ Попытка генерации через {source_name}...")
                prompt = build_image_prompt(topic)
                image_url = gen_func(prompt)
                if not image_url:
                    log(f"   ⚠️ {source_name} не дал URL, переключаемся")
                    continue
                log(f"✅ URL от {source_name}: {image_url[:60]}...")
                image_bytes = download_image(image_url)
                if not image_bytes:
                    log(f"   ❌ Не удалось скачать от {source_name}, переключаемся")
                    continue
                log(f"✅ Картинка скачана, размер {len(image_bytes)} байт")
                success, error, photo_uploaded = post_to_vk(image_bytes, post_text)
                if success:
                    if photo_uploaded:
                        send_message(chat_id, f"✅ Пост опубликован с фото от {source_name}!")
                    else:
                        send_message(chat_id, f"✅ Пост опубликован без фото (после проблем с загрузкой) от {source_name}")
                    break
                else:
                    log(f"   ❌ Публикация с фото от {source_name} не удалась: {error}")
                    log(f"   🔄 Переключаемся на следующий источник...")

            if not success:
                log("⚠️ Все источники не дали результат, публикуем без фото")
                success, error, _ = post_to_vk(None, post_text)
                if success:
                    send_message(chat_id, "✅ Пост опубликован без фото (резерв)")
                else:
                    send_message(chat_id, f"❌ Ошибка публикации: {error}")

        threading.Thread(target=publish).start()
        return

    if text.startswith("/post_in"):
        parts = text.replace("/post_in", "").strip()
        match = re.search(r'(\d+)$', parts)
        if not match:
            send_message(chat_id, "❌ Укажи минуты (число в конце)")
            return
        minutes = int(match.group(1))
        topic = parts[:match.start()].strip()
        if not topic:
            send_message(chat_id, "❌ Укажи тему поста")
            return
        publish_time = datetime.now() + timedelta(minutes=minutes)
        full_time = publish_time.strftime("%Y-%m-%d %H:%M")
        schedule = load_schedule()
        new_id = str(int(time.time()))
        schedule.append({"id": new_id, "niche": "строительный", "topic": topic, "time": full_time, "done": False})
        save_schedule(schedule)
        send_message(chat_id, f"✅ Пост добавлен в Строительный: '{topic}' в {full_time}")
        return

    if text.startswith("/list"):
        schedule = load_schedule()
        if not schedule:
            send_message(chat_id, "📭 Нет запланированных постов")
        else:
            lines = []
            for item in schedule:
                status = "✅" if item.get("done") else "⏳"
                niche_info = f"[{item.get('niche', 'ai')}] "
                lines.append(f"{status} {niche_info}ID:{item['id']} {item['topic']} -> {item['time']}")
            send_message(chat_id, "\n".join(lines[:10]))
        return

    if text.startswith("/debug"):
        try:
            if os.path.exists(SCHEDULE_FILE):
                with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                    content = f.read()
                    send_message(chat_id, f"📄 Содержимое schedule.json:\n{content[:500]}")
            else:
                send_message(chat_id, "❌ Файл не найден")
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка: {e}")
        return

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log(f"⚠️ Ошибка отправки: {e}")

# ===== ПОЛУЧЕНИЕ ОБНОВЛЕНИЙ =====
def get_updates(offset):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 10, "allowed_updates": ["message"]}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("result"):
                log(f"📨 Получены обновления: {len(data['result'])} сообщений")
                return data["result"]
        else:
            log(f"⚠️ getUpdates ошибка: {resp.status_code}")
    except Exception as e:
        log(f"⚠️ getUpdates исключение: {e}")
    return []

# ===== ТЕСТОВЫЙ ПОСТ ПРИ ЗАПУСКЕ =====
def add_test_post_if_empty():
    schedule = load_schedule()
    has_builder = any(item.get("niche") == "строительный" for item in schedule)
    if not has_builder:
        log("🧪 Добавляем тестовый пост для Строительного через 2 минуты")
        test_time = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
        schedule.append({
            "id": f"test_builder_{int(time.time())}",
            "niche": "строительный",
            "topic": "Тестовый пост для Строительного навигатора (гиперреалистичный)",
            "time": test_time,
            "done": False
        })
        save_schedule(schedule)
        log(f"🧪 Добавлен тестовый пост на {test_time}")

# ===== ГЛАВНЫЙ ЦИКЛ =====
if __name__ == "__main__":
    log("🤖 Бот для Строительного навигатора (приоритет GigaChat) запущен")
    add_test_post_if_empty()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    update_id = 0
    while True:
        updates = get_updates(update_id + 1)
        for upd in updates:
            update_id = upd["update_id"]
            if "message" in upd:
                process_message(upd["message"])
        time.sleep(0.5)