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

# ===== ИМПОРТ НАСТРОЕК (стандартные имена) =====
import text_prompts as txt_cfg
import image_prompts as img_cfg
import vk_feeds

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
BOT_TOKEN = os.getenv("BOT_TOKEN_NEW")          # токен для строительного бота (@Map_Assistant_Bot)
VK_TOKEN = os.getenv("VK_TOKEN_BUILDER")        # токен строительной группы
VK_GROUP_ID = os.getenv("VK_GROUP_ID_BUILDER")  # ID строительной группы
AGNES_API_KEY = os.getenv("AGNES_API_KEY")
GIGACHAT_API_KEY = os.getenv("GIGACHAT_API_KEY")
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
    log("⚠️ AGNES_API_KEY не задан (текст и картинки через резерв)")
if not GIGACHAT_API_KEY:
    log("⚠️ GIGACHAT_API_KEY не задан (будет пропущен)")

log("🚀 Запуск строительного бота (Agnes → GigaChat → Pollinations, с отдельными настройками, VK Feeds)")
log(f"📌 Группа ID: {VK_GROUP_ID}")

SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule.json")
STATS_FILE = os.path.join(DATA_DIR, "post_history_builder.json")
log(f"📂 Файл расписания: {SCHEDULE_FILE}")
log(f"📂 Файл статистики: {STATS_FILE}")

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

# ===== ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ RETRY =====
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

# ============================================================
# ===== ГЕНЕРАЦИЯ ТЕКСТА (использует text_prompts.py) =====
# ============================================================

def generate_post_text(topic):
    log(f"🔤 Генерация текста для темы: {topic}")
    if not AGNES_API_KEY:
        log("   ⚠️ AGNES_API_KEY не задан, используем fallback")
        return txt_cfg.get_fallback_text(topic)

    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": txt_cfg.TEXT_MODEL,
        "messages": [
            {"role": "system", "content": txt_cfg.build_system_prompt()},
            {"role": "user", "content": txt_cfg.build_user_prompt(topic)}
        ],
        "temperature": txt_cfg.TEXT_TEMPERATURE,
        "max_tokens": txt_cfg.TEXT_MAX_TOKENS
    }
    def _do():
        response = requests.post(
            "https://apihub.agnes-ai.com/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=txt_cfg.TEXT_TIMEOUT
        )
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text[:200]}")
        result = response.json()
        return result["choices"][0]["message"]["content"]
    try:
        text = retry_call(_do, max_retries=2, delay=2, backoff=2)
        log(f"   Текст получен, длина {len(text)}")
        processed = txt_cfg.post_process_text(text)
        return processed if processed else txt_cfg.get_fallback_text(topic)
    except Exception as e:
        log(f"   ❌ Генерация текста провалилась: {e}")
        return txt_cfg.get_fallback_text(topic)

# ============================================================
# ===== ГЕНЕРАЦИЯ КАРТИНКИ (использует image_prompts.py) =====
# ============================================================

def build_image_prompt(topic):
    return img_cfg.build_image_prompt(topic)

def generate_image_agnes(prompt):
    log("   🖼️ Попытка Agnes...")
    if not AGNES_API_KEY:
        log("   AGNES_API_KEY не задан")
        return None
    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": img_cfg.AGNES_IMAGE_PARAMS["model"],
        "prompt": prompt,
        "size": img_cfg.AGNES_IMAGE_PARAMS["size"],
        "n": img_cfg.AGNES_IMAGE_PARAMS["n"]
    }
    def _do():
        log(f"   Отправка запроса к Agnes (таймаут {img_cfg.TIMEOUT_AGNES} сек)")
        response = requests.post(
            "https://apihub.agnes-ai.com/v1/images/generations",
            headers=headers,
            json=data,
            timeout=img_cfg.TIMEOUT_AGNES
        )
        log(f"   Ответ Agnes: код {response.status_code}")
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text[:200]}")
        json_resp = response.json()
        if not json_resp.get("data") or len(json_resp["data"]) == 0:
            raise Exception("Empty data")
        url = json_resp["data"][0]["url"]
        log(f"   Agnes вернул URL: {url[:60]}...")
        return url
    try:
        url = retry_call(_do, max_retries=3, delay=3, backoff=2)
        log("   ✅ Agnes успешно")
        return url
    except Exception as e:
        log(f"   ❌ Agnes окончательно: {e}")
        return None

def generate_image_gigachat(prompt):
    log("   🖼️ Попытка GigaChat...")
    if not GIGACHAT_API_KEY:
        log("   GIGACHAT_API_KEY не задан")
        return None
    headers = {
        "Authorization": f"Bearer {GIGACHAT_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": img_cfg.GIGACHAT_IMAGE_PARAMS["model"],
        "prompt": prompt,
        "size": img_cfg.GIGACHAT_IMAGE_PARAMS["size"],
        "n": img_cfg.GIGACHAT_IMAGE_PARAMS["n"]
    }
    def _do():
        log(f"   Отправка запроса к GigaChat (таймаут {img_cfg.TIMEOUT_GIGACHAT} сек)")
        response = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/images/generations",
            headers=headers,
            json=data,
            timeout=img_cfg.TIMEOUT_GIGACHAT,
            verify=False
        )
        log(f"   Ответ GigaChat: код {response.status_code}")
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text[:200]}")
        json_resp = response.json()
        if not json_resp.get("data") or len(json_resp["data"]) == 0:
            raise Exception("Empty data")
        url = json_resp["data"][0]["url"]
        log(f"   GigaChat вернул URL: {url[:60]}...")
        return url
    try:
        url = retry_call(_do, max_retries=3, delay=3, backoff=2)
        log("   ✅ GigaChat успешно")
        return url
    except Exception as e:
        log(f"   ❌ GigaChat окончательно: {e}")
        return None

def generate_image_pollinations(prompt):
    log("   🖼️ Попытка Pollinations...")
    try:
        short_prompt = prompt[:250] + img_cfg.SUFFIX_POLLINATIONS
        prompt_encoded = urllib.parse.quote(short_prompt)
        url = f"https://image.pollinations.ai/prompt/{prompt_encoded}?width={img_cfg.POLLINATIONS_IMAGE_PARAMS['width']}&height={img_cfg.POLLINATIONS_IMAGE_PARAMS['height']}&nologo=true"
        log(f"   Pollinations URL сформирован: {url[:80]}...")
        try:
            head_resp = requests.head(url, timeout=10)
            if head_resp.status_code == 200:
                log("   ✅ Pollinations доступен (HEAD OK)")
                return url
            else:
                log(f"   ⚠️ Pollinations HEAD вернул {head_resp.status_code}, но попробуем GET позже")
                return url
        except Exception as e:
            log(f"   ⚠️ Pollinations HEAD не удался: {e}, но всё равно попробуем")
            return url
    except Exception as e:
        log(f"   ❌ Pollinations исключение: {e}")
        return None

def generate_image(topic):
    log(f"🖼️ Генерация картинки для темы: {topic}")
    prompt = build_image_prompt(topic)
    log(f"   Промпт: {prompt[:200]}...")

    for attempt in range(2):
        log(f"   Попытка {attempt+1}/2")
        url = generate_image_agnes(prompt)
        if url:
            return url
        url = generate_image_gigachat(prompt)
        if url:
            return url
        url = generate_image_pollinations(prompt)
        if url:
            return url
        if attempt == 0:
            log("   ⚠️ Первая попытка не дала URL, повторяем через 5 сек...")
            time.sleep(5)
    log("❌ Все попытки и источники недоступны")
    return None

def download_image(url):
    log(f"📥 Скачивание картинки: {url[:60]}...")
    def _do():
        response = requests.get(url, timeout=img_cfg.DOWNLOAD_TIMEOUT)
        log(f"   Ответ на скачивание: код {response.status_code}, длина {len(response.content)}")
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
        content = response.content
        if b"<html" in content[:200] or b"<!DOCTYPE" in content[:200]:
            raise Exception("Получен HTML вместо изображения")
        if len(content) < 100:
            raise Exception("Слишком маленький ответ")
        return content
    try:
        content = retry_call(_do, max_retries=img_cfg.DOWNLOAD_RETRIES, delay=img_cfg.DOWNLOAD_DELAY, backoff=img_cfg.DOWNLOAD_BACKOFF)
        log(f"   Успешно, размер {len(content)} байт")
        return content
    except Exception as e:
        log(f"   ❌ Скачивание провалилось: {e}")
        return None

# ===== ПУБЛИКАЦИЯ В VK (с POST для длинных запросов) =====
def vk_api_request(method, params, token, retries=3):
    base_url = "https://api.vk.com/method/"
    params = params.copy()
    params["access_token"] = token
    params["v"] = "5.131"

    post_methods = ["wall.post", "wall.getById", "photos.saveWallPhoto"]
    use_post = method in post_methods

    def _do():
        if use_post:
            response = requests.post(base_url + method, data=params, timeout=60)
        else:
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
            return False, "Ошибка публикации текста", False, None
        post_id = result.get("post_id")
        log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {post_id}")
        return True, None, False, post_id

    if not HAS_PHOTO_PERMISSION:
        log("   ⚠️ Токен не имеет права 'photos', публикуем без фото")
        result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
        if result is None:
            return False, "Ошибка публикации текста (нет прав photos)", False, None
        post_id = result.get("post_id")
        log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {post_id}")
        return True, None, False, post_id

    log("   Публикация с фото")
    try:
        log("   Шаг 1: Получение upload_url...")
        upload_resp = vk_api_request("photos.getWallUploadServer", {"group_id": abs(group_id)}, token=token, retries=3)
        if upload_resp is None:
            log("   ❌ Не удалось получить upload_url, публикуем без фото")
            result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
            if result is None:
                return False, "Ошибка публикации после падения upload_url", False, None
            post_id = result.get("post_id")
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {post_id}")
            return True, None, False, post_id
        upload_url = upload_resp["upload_url"]
        log(f"   upload_url получен: {upload_url[:50]}...")

        log("   Шаг 2: Загрузка фото...")
        def _upload():
            files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
            resp = requests.post(upload_url, files=files, timeout=60)
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
                return False, "Ошибка публикации после падения загрузки фото", False, None
            post_id = result.get("post_id")
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {post_id}")
            return True, None, False, post_id

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
                return False, "Ошибка публикации после падения сохранения фото", False, None
            post_id = result.get("post_id")
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {post_id}")
            return True, None, False, post_id

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
                return False, "Ошибка публикации после падения с фото", True, None
            post_id = result.get("post_id")
            log(f"✅ Пост опубликован (без фото) в группе {group_id}, ID: {post_id}")
            return True, None, True, post_id

        post_id = post_resp.get("post_id")
        log(f"✅ Пост опубликован с фото в группе {group_id}, ID: {post_id}")
        return True, None, True, post_id

    except Exception as e:
        log(f"   ❌ Исключение в post_to_vk: {e}")
        traceback.print_exc(file=sys.stdout)
        result = vk_api_request("wall.post", {"owner_id": group_id, "message": text, "from_group": 1}, token=token, retries=3)
        if result is not None:
            post_id = result.get("post_id")
            log(f"✅ Пост опубликован (без фото) после исключения, ID: {post_id}")
            return True, None, False, post_id
        return False, f"Исключение: {str(e)}", False, None

# ===== ВЫПОЛНЕНИЕ ПОСТА =====
def execute_scheduled_post(item):
    if item.get("niche") != "строительный":
        log(f"⏭️ Пропускаем задание для другой ниши: {item.get('niche')}")
        return

    niche = "строительный"
    topic = item["topic"]
    log(f"📢 Публикую запланированный пост: '{topic}' (строительный)")

    log("🔤 Шаг 1: Генерация текста...")
    post_text = generate_post_text(topic)
    if not post_text:
        log("❌ Текст не сгенерирован, пропускаем пост")
        return
    log(f"✅ Текст получен, длина {len(post_text)}")

    log("🖼️ Шаг 2: Генерация картинки (Agnes → GigaChat → Pollinations)...")
    image_url = generate_image(topic)
    image_bytes = None
    if image_url:
        log(f"✅ URL картинки: {image_url[:60]}...")
        image_bytes = download_image(image_url)
        if image_bytes:
            log(f"✅ Картинка скачана, размер {len(image_bytes)} байт")
        else:
            log("⚠️ Картинка не скачалась, публикуем без фото")
    else:
        log("⚠️ Картинка не сгенерирована, публикуем без фото")

    log("📤 Шаг 3: Публикация в VK...")
    success, error, photo_uploaded, post_id = post_to_vk(image_bytes, post_text)
    if success:
        log("✅ Пост успешно опубликован!")
        if post_id:
            # Можно добавить сбор статистики
            pass
    else:
        log(f"❌ Ошибка публикации: {error}")

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

# ===== VK FEEDS ФОНОВЫЙ ПОТОК =====
def vk_feeds_scheduler_loop():
    while True:
        try:
            schedule = load_schedule()
            new_posts = vk_feeds.fetch_and_generate_topics_from_vk(schedule, limit=3)
            for post_data in new_posts:
                niche = post_data['niche']
                topic = post_data['topic']
                minutes = 5
                publish_time = datetime.now() + timedelta(minutes=minutes)
                full_time = publish_time.strftime("%Y-%m-%d %H:%M")
                schedule = load_schedule()
                new_id = str(int(time.time()))
                schedule.append({
                    "id": new_id,
                    "niche": niche,
                    "topic": topic,
                    "time": full_time,
                    "done": False,
                    "source_type": "vk_feed"
                })
                save_schedule(schedule)
                log(f"📰 Добавлен пост из ВК в нишу '{niche}': {topic[:50]}...")
        except Exception as e:
            log(f"⚠️ Ошибка в VK Feeds планировщике: {e}")
        time.sleep(2 * 60 * 60)  # 2 часа

# ===== ОБРАБОТЧИКИ КОМАНД =====
def process_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    log(f"📩 Получено: {text}")

    if text.startswith("/start"):
        send_message(chat_id,
            "👷 Строительный бот.\n"
            "/post_in тема минуты — добавить пост через N минут\n"
            "/run_now тема — опубликовать прямо сейчас\n"
            "/list — показать все задания\n"
            "/debug — показать файл расписания\n"
            "/clear — удалить все задания\n"
            "/stats — показать статистику (упрощённо)"
        )
        return

    if text.startswith("/clear"):
        save_schedule([])
        send_message(chat_id, "✅ Все задания удалены.")
        return

    if text.startswith("/stats"):
        send_message(chat_id, "📊 Статистика пока не собирается в этом боте.")
        return

    if text.startswith("/run_now"):
        topic = text.replace("/run_now", "").strip()
        if not topic:
            send_message(chat_id, "❌ Укажи тему поста")
            return
        send_message(chat_id, f"⏳ Начинаю публикацию: '{topic}'...")
        def publish():
            item = {"niche": "строительный", "topic": topic, "time": datetime.now().strftime("%Y-%m-%d %H:%M")}
            execute_scheduled_post(item)
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
        send_message(chat_id, f"✅ Пост добавлен: '{topic}' в {full_time}")
        return

    if text.startswith("/list"):
        schedule = load_schedule()
        if not schedule:
            send_message(chat_id, "📭 Нет запланированных постов")
        else:
            lines = []
            for item in schedule:
                status = "✅" if item.get("done") else "⏳"
                lines.append(f"{status} {item['topic']} -> {item['time']}")
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
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
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
                log(f"📨 Получены обновления: {len(data['result'])}")
                return data["result"]
        else:
            log(f"⚠️ getUpdates ошибка: {resp.status_code}")
    except Exception as e:
        log(f"⚠️ getUpdates исключение: {e}")
    return []

# ===== ДОБАВЛЯЕМ ТЕСТОВЫЙ ПОСТ ПРИ ПУСТОМ РАСПИСАНИИ =====
def add_test_post_if_empty():
    schedule = load_schedule()
    has_builder = any(item.get("niche") == "строительный" for item in schedule)
    if not has_builder:
        log("🧪 Расписание пустое, добавляем тестовый пост через 2 минуты")
        test_time = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
        schedule.append({
            "id": f"test_builder_{int(time.time())}",
            "niche": "строительный",
            "topic": "Тестовый пост для строительного бота (гиперреалистичные картинки)",
            "time": test_time,
            "done": False
        })
        save_schedule(schedule)
        log(f"🧪 Добавлен тестовый пост на {test_time}")

# ===== ГЛАВНЫЙ ЦИКЛ =====
if __name__ == "__main__":
    log("🏗️ Строительный бот (с отдельными настройками и VK Feeds) запущен")
    add_test_post_if_empty()

    # Запускаем основной планировщик
    threading.Thread(target=scheduler_loop, daemon=True).start()

    # Запускаем поток для VK Feeds
    threading.Thread(target=vk_feeds_scheduler_loop, daemon=True).start()

    update_id = 0
    while True:
        updates = get_updates(update_id + 1)
        for upd in updates:
            update_id = upd["update_id"]
            if "message" in upd:
                process_message(upd["message"])
        time.sleep(0.5)