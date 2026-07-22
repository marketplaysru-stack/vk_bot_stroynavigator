# ===== vk_feeds_builder.py =====
import requests
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ===== КОНФИГУРАЦИЯ =====
VK_FEED_SOURCES = {
    "строительный": [
        -12345678,   # ЗАМЕНИТЕ НА РЕАЛЬНЫЕ ID ГРУПП
        # можно добавить ещё группы
    ],
    # другие ниши не нужны, только строительная
}

MAX_POSTS_PER_DAY = 4
PROCESSED_IDS_FILE = "/data/processed_vk_posts_builder.txt"
MAX_POST_AGE_DAYS = 3
MIN_TEXT_LENGTH = 50

def load_processed_ids():
    try:
        with open(PROCESSED_IDS_FILE, 'r') as f:
            return set(line.strip() for line in f)
    except FileNotFoundError:
        return set()

def save_processed_ids(ids):
    with open(PROCESSED_IDS_FILE, 'w') as f:
        for pid in ids:
            f.write(f"{pid}\n")

def get_vk_api_token():
    return os.getenv("VK_TOKEN_BUILDER")  # используем токен строительной группы

def count_today_posts_from_vk(schedule):
    today_str = datetime.now().strftime("%Y-%m-%d")
    count = 0
    for item in schedule:
        if item.get("time", "").startswith(today_str) and item.get("source_type") == "vk_feed":
            count += 1
    return count

def rewrite_with_agnes(original_text, niche=""):
    AGNES_API_KEY = os.getenv("AGNES_API_KEY")
    if not AGNES_API_KEY:
        logger.warning("   AGNES_API_KEY не задан, пропускаем рерайт")
        return None

    prompt = (
        "Ты — профессиональный копирайтер. Перепиши следующий текст в уникальный, интересный пост для строительного блога. "
        "Сохрани основную мысль, но изложи её по-своему, добавь эмодзи, разбей на абзацы, сделай текст живым и вовлекающим. "
        "Добавь заголовок. В конце добавь 3–5 хештегов по теме. Исходный текст:\n\n"
        f"{original_text}"
    )
    if niche:
        prompt += f"\n\nНиша: {niche}"

    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "agnes-2.0-flash",
        "messages": [
            {"role": "system", "content": "Ты — профессиональный копирайтер и SMM-специалист."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.85
    }
    try:
        response = requests.post(
            "https://apihub.agnes-ai.com/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=60
        )
        if response.status_code == 200:
            result = response.json()
            rewritten = result["choices"][0]["message"]["content"]
            logger.info("   ✅ Рерайт выполнен")
            return rewritten
        else:
            logger.error(f"   ❌ Ошибка рерайта: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"   ❌ Исключение при рерайте: {e}")
        return None

def fetch_and_generate_topics_from_vk(schedule, limit=3):
    token = get_vk_api_token()
    if not token:
        logger.error("❌ VK_TOKEN_BUILDER не задан")
        return []

    today_count = count_today_posts_from_vk(schedule)
    remaining = MAX_POSTS_PER_DAY - today_count
    if remaining <= 0:
        logger.info(f"📊 Достигнут лимит постов из VK Feeds на сегодня ({MAX_POSTS_PER_DAY}), пропускаем проверку")
        return []

    processed_ids = load_processed_ids()
    new_topics = []
    new_ids = set()
    cutoff_date = datetime.now() - timedelta(days=MAX_POST_AGE_DAYS)

    logger.info("🔍 Проверка новых постов в строительных группах ВК...")

    for niche, group_ids in VK_FEED_SOURCES.items():
        if remaining <= 0:
            break
        for group_id in group_ids:
            if remaining <= 0:
                break
            try:
                params = {
                    "owner_id": group_id,
                    "count": min(limit, remaining),
                    "access_token": token,
                    "v": "5.131"
                }
                response = requests.get("https://api.vk.com/method/wall.get", params=params, timeout=30)
                if response.status_code != 200:
                    logger.error(f"   ❌ Ошибка запроса к группе {group_id}: {response.status_code}")
                    continue
                data = response.json()
                if "error" in data:
                    logger.error(f"   ❌ VK API ошибка: {data['error']['error_msg']}")
                    continue

                posts = data.get("response", {}).get("items", [])
                for post in posts:
                    if remaining <= 0:
                        break
                    post_id = f"{group_id}_{post['id']}"
                    if post_id in processed_ids or post_id in new_ids:
                        continue
                    post_date = datetime.fromtimestamp(post['date'])
                    if post_date < cutoff_date:
                        continue
                    text = post.get('text', '').strip()
                    if not text or len(text) < MIN_TEXT_LENGTH:
                        continue

                    rewritten = rewrite_with_agnes(text, niche)
                    if rewritten:
                        topic = rewritten[:200] + ("..." if len(rewritten) > 200 else "")
                        source_url = f"https://vk.com/wall{group_id}_{post['id']}"
                        new_topics.append({
                            "niche": niche,
                            "topic": topic,
                            "source": source_url,
                            "rewritten": rewritten,
                            "source_type": "vk_feed"
                        })
                        new_ids.add(post_id)
                        remaining -= 1
                    else:
                        topic = text[:100].replace('\n', ' ').strip()
                        if topic:
                            new_topics.append({
                                "niche": niche,
                                "topic": f"{topic} (обзор)",
                                "source": f"https://vk.com/wall{group_id}_{post['id']}",
                                "source_type": "vk_feed"
                            })
                            new_ids.add(post_id)
                            remaining -= 1

            except Exception as e:
                logger.error(f"   ❌ Исключение при обработке группы {group_id}: {e}")

    save_processed_ids(processed_ids.union(new_ids))
    logger.info(f"✅ Найдено {len(new_topics)} новых тем из строительных групп ВК (осталось лимитов на сегодня: {remaining})")
    return new_topics