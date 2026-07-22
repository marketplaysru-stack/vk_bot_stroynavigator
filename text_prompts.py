import random

SYSTEM_PROMPT = (
    "Ты — эксперт-строитель, автор образовательного блога о строительстве и ремонте. "
    "Твоя задача — писать полезные, честные и понятные посты для информационного портала. "
    "Ты не предлагаешь услуги, не продаёшь монтаж и не составляешь сметы. "
    "Ты делишься проверенными знаниями, лайфхаками, разбором частых ошибок, "
    "актуальной информацией о СНИПах, ГОСТах и строительных нормах в России. "
    "Пиши доходчиво, с примерами, без воды. "
    "Пост должен быть практичным и вызывать доверие. "
    "Формат: дружелюбный, экспертный, но без высокомерия. "
    "Структура: 70% полезный контент, 20% примеры/обсуждение, 10% вопрос к аудитории. "
    "Используй эмодзи, разделители. В конце добавь 5 хештегов."
)

TEXT_MODEL = "agnes-2.0-flash"
TEXT_TEMPERATURE = 0.85
TEXT_MAX_TOKENS = 2048
TEXT_TIMEOUT = 60

FALLBACK_TEXT = (
    "❓ {topic}\n\n"
    "Поделитесь своим опытом в комментариях! 👇\n\n"
    "#строительство #ремонт #советы #СНИП #ГОСТ"
)

EMOJIS_BLOCKS = {
    "header": ["🏗️", "🔨", "🛠️", "📐", "🧱", "⚡"],
    "tips": ["✅", "📌", "🔍", "💡", "⚠️", "⚙️"],
    "discussion": ["💬", "🗣️", "👷", "🤝"],
    "engagement": ["👇", "✍️", "📝", "🤔", "💭"],
}

def get_random_emojis(block_type, count=2):
    emojis = EMOJIS_BLOCKS.get(block_type, ["✨"])
    return " ".join(random.sample(emojis, min(count, len(emojis))))

TOPIC_HASHTAGS = {
    "foundation": ["#фундамент", "#бетон", "#арматура", "#грунт", "#геология"],
    "walls": ["#стены", "#кирпич", "#газобетон", "#утеплитель", "#фасад"],
    "roof": ["#кровля", "#крыша", "#черепица", "#металлочерепица", "#стропила"],
    "electrical": ["#электрика", "#проводка", "#щит", "#автомат", "#кабель"],
    "plumbing": ["#сантехника", "#трубы", "#канализация", "#водоснабжение", "#отопление"],
    "repair": ["#ремонт", "#отделка", "#плитка", "#обои", "#напольныепокрытия"],
    "snip": ["#СНИП", "#ГОСТ", "#нормы", "#закон", "#строительныеправила"],
    "general": ["#строительство", "#ремонт", "#советы", "#строительныйблог", "#лайфхак"],
}
DEFAULT_HASHTAGS = ["#строительство", "#ремонт", "#советы", "#СНИП", "#ГОСТ"]

def get_hashtags(topic, count=5):
    topic_lower = topic.lower()
    for key, tags in TOPIC_HASHTAGS.items():
        if key in topic_lower:
            return random.sample(tags, min(count, len(tags)))
    return random.sample(DEFAULT_HASHTAGS, count)

def build_system_prompt():
    return SYSTEM_PROMPT

def build_user_prompt(topic):
    return f"Тема: {topic}"

def post_process_text(text):
    if not text or len(text) < 50:
        return None
    if "#" not in text:
        text += "\n\n" + " ".join(get_hashtags(text))
    return text

def get_fallback_text(topic):
    return FALLBACK_TEXT.format(topic=topic)