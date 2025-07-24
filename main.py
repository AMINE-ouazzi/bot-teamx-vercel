import telebot
import requests
from bs4 import BeautifulSoup
from telebot import types
import logging
import time
import json
from typing import Dict, Any, List
from fake_useragent import UserAgent
import os
import shutil
import img2pdf
import re
from PIL import Image

BOT_TOKEN = '7940323575:AAG5UM_w_2yoq7-ZJQsSvCX7HECSSPjngf8'
URL = 'https://olympustaff.com/'
ADMIN_ID = 7340138728

ARCHIVE_CHANNEL_ID = -1002635999889
CHAPTER_CACHE_FILE = 'chapters_cache.json'

CHANNELS_FILE = 'channels.json'
USERS_FILE = 'users.json'
BANNED_USERS_FILE = 'banned_users.json'

CHAPTERS_PER_BOT_PAGE = 49
PDF_MAX_SIZE_MB = 49
PDF_MAX_SIZE_BYTES = PDF_MAX_SIZE_MB * 1024 * 1024
COMPRESSION_QUALITY = 66
LOGO_FILE = 'logo.png'

CACHE_TTL = 15 * 60
COOLDOWN_SECONDS = 5  
CHAPTER_COLS = 34

CHAPTER_COOLDOWN_SECONDS = 2 * 60  
USER_TASK_STATUS: Dict[int, bool] = {} 
user_chapter_cooldown: Dict[int, float] = {}  

ua = UserAgent()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

USER_SESSIONS: Dict[int, Dict[str, Any]] = {}
user_last_click: Dict[int, float] = {}
admin_next_step = {}

def load_json_data(filename: str, default_value: Any = None) -> Any:
    try:
        with open(filename, 'r', encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_value if default_value is not None else {} if isinstance(default_value, dict) else []
def save_json_data(filename: str, data: Any):
    with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
def add_user_to_db(user_id: int):
    users = load_json_data(USERS_FILE, [])
    if user_id not in users:
        users.append(user_id)
        save_json_data(USERS_FILE, users)

def check_subscription(user_id: int) -> bool:
    channels = load_json_data(CHANNELS_FILE, [])
    if not channels: return True
    for channel in channels:
        try:
            status = bot.get_chat_member(chat_id=channel, user_id=user_id).status
            if status not in ['member', 'administrator', 'creator']: return False
        except Exception as e:
            logging.error(f"Error checking subscription for channel {channel}: {e}")
            return False
    return True

def force_subscription_markup() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    channels = load_json_data(CHANNELS_FILE, [])
    i = 1
    for channel in channels:
        try:
            chat = bot.get_chat(channel)
            link = f"https://t.me/{chat.username}" if chat.username else bot.export_chat_invite_link(channel)
            markup.add(types.InlineKeyboardButton(text=f"📢 القناة {i}", url=link))
            i += 1
        except Exception as e:
            logging.error(f"Could not create button for channel {channel}: {e}")
    markup.add(types.InlineKeyboardButton(text="✅ تحقق من الاشتراك", callback_data="check_subscription"))
    return markup

# --- دوال جلب البيانات ---
def get_random_headers() -> Dict: return {'User-Agent': ua.random, 'Referer': URL}
def fetch_page(url: str, params: Dict = None) -> BeautifulSoup | None:
    try:
        response = requests.get(url, headers=get_random_headers(), params=params, timeout=20)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching URL {url}: {e}")
        return None
def search_manhwa_page(query: str, page: int) -> Dict[str, Any] | None:
    params = {'s': query, 'page': page}
    soup = fetch_page(URL, params=params)
    if not soup or not soup.find('div', class_='bs'):
        params = {'search': query, 'page': page}
        soup = fetch_page(URL, params=params)
        if not soup: return None
    response_data = {'results': [], 'current_page': page, 'has_next': False, 'has_prev': False, 'total_pages': 1}
    if results_container := soup.find('div', class_='listupd'):
        for manhwa in results_container.find_all('div', class_='bs'):
            if (link_tag := manhwa.find('a')) and (title_tag := manhwa.find('div', class_='tt')):
                response_data['results'].append({'title': title_tag.text.strip(), 'link': link_tag['href']})
    if pagination := soup.find('ul', class_='pagination') or soup.find('div', class_='pagination'):
        response_data['has_next'] = bool(pagination.find('a', rel='next'))
        response_data['has_prev'] = page > 1
        page_links = pagination.find_all('a', class_='page-link') or pagination.find_all('a')
        pages = [int(a.text) for a in page_links if a.text.isdigit()]
        if pages:
            response_data['total_pages'] = max(pages)
        elif current_page_span := pagination.find(['span','a'], class_=['current','active']):
             try:
                current_page_num = int(current_page_span.text)
                response_data['total_pages'] = max(current_page_num, page)
             except (ValueError, TypeError): pass
    return response_data
def fetch_manga_info(manga_url: str) -> Dict[str, Any]:
    info = {'image_url': None, 'caption': "لم يتم العثور على معلومات."}
    soup = fetch_page(manga_url)
    if not soup: return info
    if sidebar := soup.find('div', class_='col-md-3'):
        main_content = soup.find('div', class_='col-md-9')
        title_part, basic_info_part, story_part, genres_part = "", [], "", ""
        if img_tag := sidebar.find('img', alt="Manga Image"): info['image_url'] = img_tag['src']
        if title_tag := soup.find('h1', class_='entry-title'): title_part = f"*{title_tag.text.strip()}*"
        for div in sidebar.find_all('div', class_='full-list-info'):
            if (key_tag := div.find('small')) and (value_tag := key_tag.find_next_sibling('small')):
                key, value = key_tag.text.strip().replace(':', ''), value_tag.text.strip()
                if any(term in key for term in ["النوع", "الحالة", "الرسام", "الرواية"]) and value_tag.a: value = value_tag.a.text.strip()
                if key and value: basic_info_part.append(f"*{key}:* `{value}`")
        if main_content:
            if genres_div := main_content.find('div', class_='review-author-info'):
                genres = [a.text.strip() for a in genres_div.find_all('a')]
                if genres: genres_part = f"\n*التصنيفات:*\n*{' - '.join(genres)}*"
            if (story_div := main_content.find('div', class_='review-content')) and story_div.p:
                story_part = f"\n*القصة:*\n_{story_div.p.text.strip()}_"
        final_caption_list = [p for p in [title_part, '\n'.join(basic_info_part), story_part, genres_part] if p]
        if final_caption_list: info['caption'] = '\n\n'.join(final_caption_list)
    return info
def fetch_all_manga_chapters(manga_url: str) -> List[Dict]:
    all_chapters, page = [], 1
    while True:
        params = {'page': str(page)} if page > 1 else None
        soup = fetch_page(manga_url, params=params)
        if not soup: break
        chapter_list_ul = soup.find('div', class_='eplister') or soup.find('div', class_='ts-chl-collapsible-content')
        if not chapter_list_ul: break
        found_chapters_on_page = []
        for li in chapter_list_ul.find_all('li'):
            if a_tag := li.find('a'):
                chapter_num_display = "???"
                num_divs = a_tag.find_all('div', class_='epl-num')
                num_text = num_divs[1].text.strip() if len(num_divs) > 1 else a_tag.text.strip()
                match = re.search(r'(\d+\.?\d*)', num_text)
                if match: chapter_num_display = match.group(1)
                found_chapters_on_page.append({'title': chapter_num_display, 'link': a_tag['href']})
        if not found_chapters_on_page: break
        all_chapters.extend(found_chapters_on_page)
        pagination = soup.find('div', class_='pagination') or soup.find('ul', class_='pagination')
        if not pagination or not (pagination.find('a', rel='next') or pagination.find('a', class_='next') or pagination.find(lambda t: t.name == 'a' and 'التالي' in t.text)): break
        page += 1
    return all_chapters

def process_chapter_request(chat_id: int, message_id: int, chapter_url: str, chapter_title: str, manga_title: str):
    USER_TASK_STATUS[chat_id] = True

    chapters_cache = load_json_data(CHAPTER_CACHE_FILE, {})
    if chapter_url in chapters_cache:
        logging.info(f"[{chat_id}] Chapter found in cache: {chapter_url}")
        cached_data = chapters_cache[chapter_url]
        archive_chat_id = cached_data.get('chat_id')
        archive_message_id = cached_data.get('message_id')
        if archive_chat_id and archive_message_id:
            try:
                bot.edit_message_text("✅ تم العثور على الفصل في الأرشيف! جارٍ الإرسال...", chat_id, message_id)
                bot.forward_message(chat_id, from_chat_id=archive_chat_id, message_id=archive_message_id)
                bot.delete_message(chat_id, message_id)
                user_chapter_cooldown[chat_id] = time.time()
                USER_TASK_STATUS.pop(chat_id, None)
                return
            except Exception as e:
                logging.warning(f"Failed to forward cached chapter, will re-create. Error: {e}")
    create_and_archive_chapter(chat_id, message_id, chapter_url, chapter_title, manga_title)

def create_and_archive_chapter(chat_id: int, message_id: int, chapter_url: str, chapter_title: str, manga_title: str):
    try:
        wait_msg = bot.edit_message_text("⏳ هذا الفصل يتم إعداده لأول مرة، يرجى الانتظار...", chat_id, message_id, reply_markup=None)
    except telebot.apihelper.ApiTelegramException:
        bot.delete_message(chat_id, message_id)
        wait_msg = bot.send_message(chat_id, "⏳ هذا الفصل يتم إعداده لأول مرة، يرجى الانتظار...")
    temp_dir = f"temp_{chat_id}_{int(time.time())}"
    os.makedirs(temp_dir, exist_ok=True)
    try:
        soup = fetch_page(chapter_url)
        if not soup: raise ValueError("Failed to fetch chapter page")
        image_container = soup.select_one('#readerarea, div.reading-content, div.image_list')
        if not image_container: raise ValueError("Image container not found")
        image_tags = image_container.find_all('img')
        if not image_tags: raise ValueError("No images found in container")
        total_images, image_paths = len(image_tags), []
        for i, img_tag in enumerate(image_tags, 1):
            img_url = (img_tag.get('data-src') or img_tag.get('src') or '').strip()
            if not img_url: continue
            bot.edit_message_text(f"⏳ جارٍ التحميل... {i}/{total_images}", chat_id, wait_msg.id)
            img_data = requests.get(img_url, headers=get_random_headers()).content
            img_path = os.path.join(temp_dir, f"page_{i:03d}.jpg")
            with open(img_path, 'wb') as f: f.write(img_data)
            try:
                with Image.open(img_path) as img:
                    if img.mode in ("RGBA", "P"): img.convert("RGB").save(img_path, 'JPEG', quality=100)
            except Exception as e: logging.warning(f"Could not process image {img_path}: {e}")
            image_paths.append(img_path)

        if not image_paths: raise ValueError("No images were downloaded successfully.")
        if os.path.exists(LOGO_FILE):
            image_paths.insert(0, LOGO_FILE)
            logging.info(f"Added logo '{LOGO_FILE}' to the beginning of the PDF.")
        else:
            logging.warning(f"Logo file '{LOGO_FILE}' not found. PDF will be created without it.")
        bot.edit_message_text(f"✅ تم التحميل.\n🔄 جارٍ إنشاء ملف PDF...", chat_id, wait_msg.id)
        safe_manga_title = re.sub(r'[\\/*?:"<>|]', "", manga_title)
        safe_chapter_title = re.sub(r'[\\/*?:"<>|]', "", chapter_title)
        pdf_filename = f"{safe_manga_title} - {safe_chapter_title}.pdf"
        pdf_path = os.path.join(temp_dir, pdf_filename)
        with open(pdf_path, "wb") as f: f.write(img2pdf.convert(image_paths))

        if os.path.getsize(pdf_path) > PDF_MAX_SIZE_BYTES:
            bot.edit_message_text(f"⚠️ حجم الملف كبير، جارٍ ضغطه...", chat_id, wait_msg.id)
            compressed_image_paths, compressed_dir = [], os.path.join(temp_dir, 'compressed')
            os.makedirs(compressed_dir)
            if os.path.exists(LOGO_FILE):
                compressed_image_paths.append(LOGO_FILE)
            for i, original_path in enumerate(image_paths, 1):
                if os.path.exists(LOGO_FILE) and original_path == LOGO_FILE: continue
                bot.edit_message_text(f"🔄 جارٍ ضغط الصور... {i}/{len(image_paths)}", chat_id, wait_msg.id)
                with Image.open(original_path) as img:
                    if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                    compressed_path = os.path.join(compressed_dir, os.path.basename(original_path))
                    img.save(compressed_path, 'JPEG', quality=COMPRESSION_QUALITY, optimize=True)
                    compressed_image_paths.append(compressed_path)
            with open(pdf_path, "wb") as f: f.write(img2pdf.convert(compressed_image_paths))
        bot.edit_message_text("📤 جارٍ أرشفة الفصل...", chat_id, wait_msg.id)
        with open(pdf_path, 'rb') as pdf_file:
            caption = (
                f"📖 *الاسم:* `{manga_title}`\n"
                f"🔖 *الفصل:* `{chapter_title}`\n\n"
                f"✨ *مقدم من الفريق:*\n"
                f"*[@team_xmabot] [@Speed_Manga]*\n"
                f"*[@teamx_archive] [@ropani] [@Franke_nstein]*\n\n"
                f"🔗 *الرابط الأصلي:*\n`{chapter_url}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*ديما مغريب تحية*"
            )
            archive_msg = bot.send_document(ARCHIVE_CHANNEL_ID, pdf_file, caption=caption)
        chapters_cache = load_json_data(CHAPTER_CACHE_FILE, {})
        archive_link = f"https://t.me/c/{str(archive_msg.chat.id).replace('-100', '')}/{archive_msg.message_id}"
        chapters_cache[chapter_url] = {'chat_id': archive_msg.chat.id, 'message_id': archive_msg.message_id, 'url_tele': archive_link}
        save_json_data(CHAPTER_CACHE_FILE, chapters_cache)
        bot.edit_message_text("✅ تم تجهيز الفصل! جارٍ الإرسال...", chat_id, wait_msg.id)
        bot.forward_message(chat_id, from_chat_id=archive_msg.chat.id, message_id=archive_msg.message_id)
        bot.delete_message(chat_id, wait_msg.id)
        user_chapter_cooldown[chat_id] = time.time()
    except Exception as e:
        logging.error(f"[{chat_id}] Error creating and archiving the class. Can you point the error to the developer to fix the problem {e}", exc_info=True)
        try: bot.edit_message_text(f"❌ حدث خطأ أثناء إعداد الفصل.", chat_id, wait_msg.id)
        except: bot.send_message(chat_id, f"❌ حدث خطأ أثناء إعداد الفصل. يرجى ابلاغ المطور  @ropani")
    finally:
        USER_TASK_STATUS.pop(chat_id, None)
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)

def create_search_results_markup(session: Dict, page: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    page_data = session['cache'][page]
    for i, item in enumerate(page_data['results']):
        markup.add(types.InlineKeyboardButton(text=item['title'], callback_data=f"select:{page}:{i}"))
    nav_buttons = []
    if page_data.get('has_prev', False):
        nav_buttons.append(types.InlineKeyboardButton("‹ السابق", callback_data=f"nav:{page-1}"))
    total_pages = session.get('total_pages', page)
    nav_buttons.append(types.InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page_data.get('has_next', False):
        nav_buttons.append(types.InlineKeyboardButton("التالي ›", callback_data=f"nav:{page+1}"))
    if len(nav_buttons) > 1: markup.row(*nav_buttons)
    return markup
def create_manga_menu_markup(page: int, index: int, in_info_view=False) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    if not in_info_view:
        markup.add(types.InlineKeyboardButton("ℹ️ عرض المعلومات", callback_data=f"info:{page}:{index}"))
    markup.add(types.InlineKeyboardButton("📚 عرض الفصول", callback_data=f"chaps:{page}:{index}"))
    markup.add(types.InlineKeyboardButton("🔙 العودة للبحث", callback_data=f"back_to_search:{page}"))
    return markup
def create_paginated_chapters_markup(full_chapters: List[Dict], bot_page: int, search_page: int, manga_index: int, reversed_order: bool) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    total_bot_pages = (len(full_chapters) + CHAPTERS_PER_BOT_PAGE - 1) // CHAPTERS_PER_BOT_PAGE
    total_bot_pages = max(1, total_bot_pages)
    display_chapters = full_chapters[::-1] if reversed_order else full_chapters
    start_index = (bot_page - 1) * CHAPTERS_PER_BOT_PAGE
    chapters_for_page = display_chapters[start_index : start_index + CHAPTERS_PER_BOT_PAGE]
    rows = [chapters_for_page[i:i + CHAPTER_COLS] for i in range(0, len(chapters_for_page), CHAPTER_COLS)]
    for row in rows:
        buttons = []
        for chap in row:
            original_index = full_chapters.index(chap)
            callback_str = f"download:{original_index}"
            buttons.append(types.InlineKeyboardButton(chap['title'], callback_data=callback_str))
        if buttons: markup.row(*buttons)
    nav_buttons = []
    if bot_page > 1:
        nav_buttons.append(types.InlineKeyboardButton("‹‹ السابق", callback_data=f"bot_page:{search_page}:{manga_index}:{bot_page-1}"))
    page_indicator = f"صفحة {bot_page}/{total_bot_pages}"
    nav_buttons.append(types.InlineKeyboardButton(page_indicator, callback_data="noop"))
    if bot_page < total_bot_pages:
        nav_buttons.append(types.InlineKeyboardButton("التالي ››", callback_data=f"bot_page:{search_page}:{manga_index}:{bot_page+1}"))
    if nav_buttons: markup.row(*nav_buttons)
    reverse_text = "⬇️ الأقدم أولاً" if reversed_order else "⬆️ الأحدث أولاً"
    markup.row(
        types.InlineKeyboardButton(reverse_text, callback_data=f"reverse:{search_page}:{manga_index}:{bot_page}"),
        types.InlineKeyboardButton("🔙 العودة", callback_data=f"select:{search_page}:{manga_index}")
    )
    return markup
def subscription_required(func):
    def wrapper(message):
        user_id = message.from_user.id
        if user_id == ADMIN_ID:
            func(message)
            return
        if not check_subscription(user_id):
            text = "✋ *عذراً، عليك الاشتراك في قنوات البوت أولاً لاستخدامه*\n\nاضغط على الأزرار بالأسفل للاشتراك، ثم اضغط على زر التحقق."
            bot.send_message(message.chat.id, text, reply_markup=force_subscription_markup())
            return
        func(message)
    return wrapper

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    add_user_to_db(user_id)
    if user_id == ADMIN_ID:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add("➕ إضافة قناة", "🗑️ حذف قناة", "📢 إذاعة", "📊 الإحصائيات")
        bot.send_message(user_id, "أهلاً بك أيها المشرف! اختر أحد الأوامر.", reply_markup=markup)
        return
    if not check_subscription(user_id):
        text = "✋ *أهلاً بك! عليك الاشتراك في قنوات البوت أولاً*\n\nاضغط على الأزرار بالأسفل للاشتراك، ثم اضغط على زر التحقق."
        bot.send_message(message.chat.id, text, reply_markup=force_subscription_markup())
        return
    bot.reply_to(message, "━━━━━━━━━━━━━━━━━━\nمرحبًا بك في البوت 👋\n\nهذا البوت تجريبي حاليًا، وقد تواجه بعض الأخطاء أثناء الاستخدام.\nفي حال حدوث أي خلل، لا تتردد في مراسلة المطور:\n@ropani\n\n📌 أرسل اسم المانهوا أو المانجا التي تبحث عنها للبدء.\n━━━━━━━━━━━━━━━━━━")
@bot.message_handler(commands=['git'])
def send_backup_files(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        logging.warning(f"Unauthorized user {user_id} tried to use /git command.")
        return
    bot.send_message(ADMIN_ID, "⏳ جارٍ جلب ملفات البيانات وإرسالها...")
    files_to_send = [CHAPTER_CACHE_FILE, CHANNELS_FILE, USERS_FILE]
    for filename in files_to_send:
        if os.path.exists(filename):
            try:
                with open(filename, 'rb') as file_doc:
                    bot.send_document(ADMIN_ID, file_doc, caption=f"📄 ملف: `{filename}`")
                time.sleep(1)
            except Exception as e:
                bot.send_message(ADMIN_ID, f"❌ فشل إرسال الملف: `{filename}`\nالخطأ: {e}")
                logging.error(f"Could not send file {filename} to admin. Error: {e}")
        else:
            bot.send_message(ADMIN_ID, f"⚠️ لم يتم العثور على الملف: `{filename}`.")
    bot.send_message(ADMIN_ID, "✅ تم إرسال جميع الملفات المتاحة.")
def get_session(chat_id: int) -> Dict | None:
    session = USER_SESSIONS.get(chat_id)
    if not session or (time.time() - session.get('timestamp', 0)) > CACHE_TTL:
        if chat_id in USER_SESSIONS: del USER_SESSIONS[chat_id]
        return None
    session['timestamp'] = time.time()
    return session
@subscription_required
def handle_initial_search(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if USER_TASK_STATUS.get(user_id, False):
        bot.reply_to(message, "⏳ *العملية السابقة لم تنتهي بعد. يرجى التحلي بالصبر.*")
        return

    query = message.text.strip()
    if not query: return
    wait_msg = bot.send_message(chat_id, f"🔍 جاري البحث عن '{query}'...")
    initial_data = search_manhwa_page(query, 1)
    bot.delete_message(chat_id, wait_msg.message_id)
    if not initial_data or not initial_data['results']:
        bot.send_message(chat_id, f"لم يتم العثور على نتائج للبحث عن '{query}'.")
        return
    USER_SESSIONS[chat_id] = {'query': query, 'cache': {1: initial_data}, 'timestamp': time.time(), 'total_pages': initial_data.get('total_pages',1)}
    markup = create_search_results_markup(USER_SESSIONS[chat_id], 1)
    bot.send_message(chat_id, f"✅ إليك نتائج البحث عن '{query}':", reply_markup=markup)

# ... (دوال broadcast_handler و admin_text_handler و user_text_handler كما هي)
@bot.message_handler(func=lambda msg: msg.from_user.id == ADMIN_ID and admin_next_step.get(msg.from_user.id) == 'broadcast', content_types=['audio', 'photo', 'voice', 'video', 'document', 'text', 'location', 'contact', 'sticker'])
def broadcast_handler(message):
    user_id = message.from_user.id
    admin_next_step.pop(user_id, None)
    users = load_json_data(USERS_FILE, [])
    if not users:
        bot.send_message(user_id, "لا يوجد مستخدمون لإرسال الإعلان إليهم.")
        return
    bot.send_message(user_id, f"📣 جاري بدء الإذاعة إلى {len(users)} مستخدم...")
    success_count, fail_count = 0, 0
    for u_id in users:
        try:
            bot.forward_message(chat_id=u_id, from_chat_id=user_id, message_id=message.message_id)
            success_count += 1
        except Exception: fail_count += 1
        time.sleep(0.05)
    bot.send_message(user_id, f"✅ انتهت الإذاعة!\n- تم الإرسال بنجاح إلى: {success_count}\n- فشل الإرسال إلى: {fail_count}")
@bot.message_handler(func=lambda message: message.from_user.id == ADMIN_ID, content_types=['text'])
def admin_text_handler(message):
    user_id = message.from_user.id
    text = message.text
    if admin_next_step.get(user_id) == 'add_channel':
        channel_id = text.strip()
        if not (channel_id.startswith('@') or channel_id.startswith('-100')):
            bot.send_message(user_id, "خطأ. أرسل معرف القناة (مثل @channel_username) أو الأيدي الرقمي.")
            return
        channels = load_json_data(CHANNELS_FILE, [])
        if channel_id in channels: bot.send_message(user_id, "هذه القناة مضافة بالفعل.")
        else:
            channels.append(channel_id)
            save_json_data(CHANNELS_FILE, channels)
            bot.send_message(user_id, f"✅ تم إضافة القناة `{channel_id}` بنجاح!")
        admin_next_step.pop(user_id, None)
    elif text == "➕ إضافة قناة":
        bot.send_message(user_id, "حسناً، الآن أرسل معرف القناة (مثل @username).\nتأكد أن البوت مشرف في القناة أولاً.")
        admin_next_step[user_id] = 'add_channel'
    elif text == "📢 إذاعة":
        bot.send_message(user_id, "الآن قم بتوجيه أو إرسال الرسالة التي تريد إذاعتها للجميع.")
        admin_next_step[user_id] = 'broadcast'
    elif text == "📊 الإحصائيات":
        users_count = len(load_json_data(USERS_FILE, []))
        bot.send_message(user_id, f"📊 عدد مستخدمي البوت: *{users_count}*")
    else:
        handle_initial_search(message)
@bot.message_handler(func=lambda message: message.from_user.id != ADMIN_ID, content_types=['text'])
@subscription_required
def user_text_handler(message):
    handle_initial_search(message)
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    user_id = call.from_user.id
    if call.data == "check_subscription":
        if check_subscription(user_id):
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.send_message(call.message.chat.id, "✅ شكراً لاشتراكك! يمكنك الآن استخدام البوت.\n\nأرسل اسم المانهوا للبحث.")
        else:
            bot.answer_callback_query(call.id, "❌ عذراً، لم تشترك في جميع القنوات بعد. حاول مرة أخرى.", show_alert=True)
        return
    if user_id != ADMIN_ID and not check_subscription(user_id):
        bot.answer_callback_query(call.id, "عليك الاشتراك في القنوات أولاً.", show_alert=True)
        return
    current_time = time.time()
    if call.data != 'noop' and current_time - user_last_click.get(user_id, 0) < COOLDOWN_SECONDS:
        bot.answer_callback_query(call.id, "يرجى الانتظار.", show_alert=False)
        return
    user_last_click[user_id] = current_time
    chat_id = call.message.chat.id
    session = get_session(chat_id)
    if not session:
        bot.answer_callback_query(call.id, "انتهت صلاحية الجلسة. ابحث مرة أخرى.", show_alert=True)
        try: bot.edit_message_text("انتهت صلاحية هذه الرسالة.", chat_id, call.message.id, reply_markup=None)
        except: pass
        return
    parts = call.data.split(':')
    action = parts[0]
    try:
        if action == "chaps":
            bot.answer_callback_query(call.id)
            if call.message.content_type == 'text':
                bot.edit_message_text("⏳ جاري جلب قائمة الفصول الكاملة...", chat_id, call.message.id, reply_markup=None)
            else:
                bot.delete_message(chat_id, call.message.id)
                new_msg = bot.send_message(chat_id, "⏳ جاري جلب قائمة الفصول الكاملة...")
                call.message.id = new_msg.message_id
            search_page, manga_index = map(int, parts[1:])
            manga_url = session['cache'][search_page]['results'][manga_index]['link']
            full_chapters = fetch_all_manga_chapters(manga_url)
            if not full_chapters:
                bot.edit_message_text("❌ لم يتم العثور على فصول لهذا العمل.", chat_id, call.message.id)
                return
            session['full_chapter_list'] = full_chapters
            session['active_manga_title'] = session['cache'][search_page]['results'][manga_index]['title']
            reversed_order = session.get('chapters_reversed', False)
            markup = create_paginated_chapters_markup(full_chapters, 1, search_page, manga_index, reversed_order)
            bot.edit_message_text(f"📚 فصول *{session['active_manga_title']}* ({len(full_chapters)} فصل):", chat_id, call.message.id, reply_markup=markup)
        elif action == "bot_page" or action == "reverse":
            # ... (no changes here)
            bot.answer_callback_query(call.id)
            search_page, manga_index, bot_page = map(int, parts[1:])
            if action == "reverse":
                session['chapters_reversed'] = not session.get('chapters_reversed', False)
            reversed_order = session.get('chapters_reversed', False)
            full_chapters = session.get('full_chapter_list', [])
            if not full_chapters:
                bot.answer_callback_query(call.id, "بيانات الفصول منتهية، أعد فتح القائمة.", show_alert=True)
                return
            markup = create_paginated_chapters_markup(full_chapters, bot_page, search_page, manga_index, reversed_order)
            bot.edit_message_reply_markup(chat_id, call.message.id, reply_markup=markup)

        elif action == "download":
            last_download_time = user_chapter_cooldown.get(user_id)
            if last_download_time and (time.time() - last_download_time) < CHAPTER_COOLDOWN_SECONDS:
                remaining_time = int(CHAPTER_COOLDOWN_SECONDS - (time.time() - last_download_time))
                bot.answer_callback_query(call.id, f"يجب عليك الانتظار {remaining_time} ثانية قبل طلب فصل آخر.", show_alert=True)
                return

            if USER_TASK_STATUS.get(user_id, False):
                bot.answer_callback_query(call.id, "⏳ العملية السابقة لم تنتهي بعد. يرجى التحلي بالصبر.", show_alert=True)
                return

            bot.answer_callback_query(call.id, "✅ تم استلام طلبك، جارٍ التحضير...")
            original_index = int(parts[1])
            manga_title = session.get('active_manga_title', 'مانجا غير معروفة')
            full_chapters = session.get('full_chapter_list', [])
            if original_index >= len(full_chapters):
                bot.edit_message_text("حدث خطأ: فهرس الفصل غير صالح.", chat_id, call.message.id)
                return
            chapter_data = full_chapters[original_index]
            process_chapter_request(chat_id, call.message.id, chapter_data['link'], chapter_data['title'], manga_title)
            return
        elif action == "select":
            bot.answer_callback_query(call.id)
            page, index = int(parts[1]), int(parts[2])
            manga_title = session['cache'][page]['results'][index]['title']
            session['chapters_reversed'] = False
            markup = create_manga_menu_markup(page, index)
            text_to_send = f"اخترت: *{manga_title}*"
            if call.message.content_type == 'text':
                bot.edit_message_text(text_to_send, chat_id, call.message.id, reply_markup=markup)
            else:
                bot.delete_message(chat_id, call.message.id)
                bot.send_message(chat_id, text_to_send, reply_markup=markup)
        elif action == "info":
            # ... (no changes here)
            bot.answer_callback_query(call.id, "جاري جلب المعلومات...")
            page, index = int(parts[1]), int(parts[2])
            manga_url = session['cache'][page]['results'][index]['link']
            details = fetch_manga_info(manga_url)
            if details and details.get('image_url'):
                markup = create_manga_menu_markup(page, index, in_info_view=True)
                bot.delete_message(chat_id, call.message.id)
                bot.send_photo(chat_id, details['image_url'], caption=details.get('caption', 'لا توجد معلومات'), reply_markup=markup)
            else:
                 bot.answer_callback_query(call.id, "فشل جلب المعلومات.", show_alert=True)
        elif action == "back_to_search":
            bot.answer_callback_query(call.id)
            page = int(parts[1])
            markup = create_search_results_markup(session, page)
            text_to_send = f"✅ نتائج البحث عن '{session['query']}':"
            if call.message.content_type != 'text':
                bot.delete_message(chat_id, call.message.id)
                bot.send_message(chat_id, text_to_send, reply_markup=markup)
            else:
                bot.edit_message_text(text_to_send, chat_id, call.message.id, reply_markup=markup)
        elif action == "nav":
            bot.answer_callback_query(call.id, "جاري جلب الصفحة...")
            page = int(parts[1])
            if page <= 0: return
            if page not in session['cache']:
                page_data = search_manhwa_page(session['query'], page)
                if page_data and page_data['results']:
                    session['cache'][page] = page_data
                    session['total_pages'] = page_data.get('total_pages', page)
                else:
                    bot.answer_callback_query(call.id, "لا توجد صفحات أخرى.")
                    return
            markup = create_search_results_markup(session, page)
            bot.edit_message_reply_markup(chat_id, call.message.id, reply_markup=markup)
        elif action == "noop":
            bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error in callback handler: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "حدث خطأ!", show_alert=True)

print("البوت قيد التشغيل (إصدار متكامل مع منع التزامن وفترة التهدئة)...")
bot.polling(non_stop=True, skip_pending=True)
