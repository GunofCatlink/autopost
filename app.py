import os
import json
import schedule
import time
import threading
import uuid
import asyncio
from datetime import datetime
from flask import Flask, render_template, request, redirect, flash, send_from_directory, jsonify
from werkzeug.utils import secure_filename
from PIL import Image
from telethon import TelegramClient
from telethon.errors import RPCError
import vk_api
from vk_api.upload import VkUpload
import requests
from dotenv import load_dotenv
import hashlib
import base64

# ================== КОНФИГУРАЦИЯ ==================
load_dotenv()

# Telegram
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
SESSION_NAME = os.getenv('SESSION_NAME', 'telegram_session')
TG_CHANNEL_ID = os.getenv('TG_CHANNEL_ID', '')

# VK (нужен USER token, а не SERVICE token)
VK_TOKEN = os.getenv('VK_TOKEN', '')
VK_GROUP_ID = os.getenv('VK_GROUP_ID', '')

# Одноклассники
OK_ACCESS_TOKEN = os.getenv('OK_ACCESS_TOKEN', '')
OK_APPLICATION_KEY = os.getenv('OK_APPLICATION_KEY', '')
OK_APPLICATION_SECRET = os.getenv('OK_APPLICATION_SECRET', '')
OK_GROUP_ID = os.getenv('OK_GROUP_ID', '')

# Общие настройки
DATA_FILE = 'posts_queue.json'
SOCIAL_CONFIGS_FILE = 'social_configs.json'

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-123')
app.config['MAX_CONTENT_LENGTH'] = 50 * 2048 * 2048
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['COMPRESS_IMAGES'] = True
app.config['MAX_ALBUM_SIZE'] = 10
app.config['DAYS_TO_KEEP'] = 7
app.config['CAPTION_LIMIT'] = 4096

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# ================== TELEGRAM MANAGER (С ПРАВИЛЬНЫМИ ЛИМИТАМИ) ==================
class TelegramManager:
    """Менеджер для работы с Telegram"""

    @classmethod
    def is_authorized(cls):
        """Проверка авторизации клиента"""
        try:
            session_file = f"{SESSION_NAME}.session"
            return os.path.exists(session_file) and os.path.getsize(session_file) > 0
        except:
            return False

    @classmethod
    def send_message(cls, text, images=None):
        """Отправляет сообщение в Telegram канал и возвращает ID сообщений"""
        if not TG_CHANNEL_ID:
            print("❌ TG_CHANNEL_ID не указан")
            return {'success': False, 'message_ids': []}

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(cls._send_telegram_async(text, images))
            loop.close()
            return result
        except Exception as e:
            print(f"❌ Ошибка отправки в Telegram: {e}")
            return {'success': False, 'message_ids': []}

    @staticmethod
    async def _send_telegram_async(text, images=None):
        """Асинхронная отправка сообщения с возвратом ID сообщений"""
        client = None
        try:
            client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
            await client.connect()

            if not await client.is_user_authorized():
                print("❌ Telegram клиент не авторизован")
                return {'success': False, 'message_ids': []}

            entity = await client.get_entity(TG_CHANNEL_ID)
            message_ids = []

            # Если есть изображения
            if images and len(images) > 0:
                files = []
                for img_filename in images[:10]:  # Ограничиваем 10 файлами
                    img_path = os.path.join(app.config['UPLOAD_FOLDER'], img_filename)
                    if os.path.exists(img_path):
                        files.append(img_path)

                if files:
                    # Для альбома ограничение 1024 символа для подписи
                    caption = text[:1024] if text else None
                    sent_message = await client.send_file(entity, files, caption=caption)

                    # Если это альбом (несколько сообщений)
                    if isinstance(sent_message, list):
                        message_ids = [msg.id for msg in sent_message]
                    else:
                        message_ids = [sent_message.id]

                    print(f"✅ Telegram: отправлено {len(files)} изображений, IDs: {message_ids}")
                    return {'success': True, 'message_ids': message_ids}
                else:
                    # Если файлов нет, отправляем только текст
                    if text:
                        sent_message = await client.send_message(entity, text[:4096])
                        message_ids = [sent_message.id]
                        print(f"✅ Telegram: отправлен текст, ID: {message_ids}")
                        return {'success': True, 'message_ids': message_ids}
            else:
                # Отправляем только текст
                if text:
                    sent_message = await client.send_message(entity, text[:4096])
                    message_ids = [sent_message.id]
                    print(f"✅ Telegram: отправлен текст, ID: {message_ids}")
                    return {'success': True, 'message_ids': message_ids}
                else:
                    print("⚠️ Telegram: нет текста и изображений для отправки")
                    return {'success': False, 'message_ids': []}

            return {'success': False, 'message_ids': []}

        except Exception as e:
            print(f"❌ Ошибка отправки Telegram: {e}")
            return {'success': False, 'message_ids': []}
        finally:
            if client:
                await client.disconnect()

    @staticmethod
    async def _delete_telegram_async(message_ids, chat_entity):
        """Асинхронное удаление сообщений в Telegram"""
        client = None
        try:
            client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
            await client.connect()
            await client.delete_messages(entity=chat_entity, message_ids=message_ids)
            print(f"✅ Telegram: удалены сообщения {message_ids}")
            return True
        except Exception as e:
            print(f"❌ Ошибка удаления сообщений Telegram: {e}")
            return False
        finally:
            if client:
                await client.disconnect()

    @classmethod
    def delete_messages(cls, message_ids):
        """Синхронная обертка для удаления сообщений в Telegram"""
        if not TG_CHANNEL_ID or not message_ids:
            return False

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
            loop.run_until_complete(client.connect())
            entity = loop.run_until_complete(client.get_entity(TG_CHANNEL_ID))
            loop.run_until_complete(client.disconnect())

            result = loop.run_until_complete(cls._delete_telegram_async(message_ids, entity))
            return result
        except Exception as e:
            print(f"❌ Ошибка в delete_messages: {e}")
            return False
        finally:
            loop.close()


# ================== VK MANAGER ==================
class VKManager:
    """Менеджер для работы с VK API"""

    _vk_session = None
    _vk_upload = None

    @classmethod
    def get_session(cls):
        """Возвращает сессию VK"""
        if cls._vk_session is None and VK_TOKEN:
            try:
                cls._vk_session = vk_api.VkApi(token=VK_TOKEN)
                cls._vk_upload = VkUpload(cls._vk_session)
                print("✅ Сессия VK инициализирована")
            except Exception as e:
                print(f"❌ Ошибка инициализации VK: {e}")
        return cls._vk_session

    @classmethod
    def is_configured(cls):
        """Проверяет, настроен ли VK"""
        return bool(VK_TOKEN and VK_GROUP_ID)

    @classmethod
    def upload_photo_to_vk(cls, image_path):
        """Загружает фото на сервер VK"""
        if not cls.is_configured():
            return None

        session = cls.get_session()
        if not session:
            return None

        try:
            print(f"📤 Загружаем фото в VK: {os.path.basename(image_path)}")
            photo = cls._vk_upload.photo_wall(
                photos=image_path,
                group_id=abs(int(VK_GROUP_ID))
            )

            if photo and len(photo) > 0:
                photo_data = photo[0]
                attachment = f"photo{photo_data['owner_id']}_{photo_data['id']}"
                print(f"✅ Фото загружено: {attachment}")
                return attachment

        except Exception as e:
            print(f"❌ Ошибка загрузки фото в VK: {e}")

        return None

    @classmethod
    def post_to_vk(cls, text, images=None):
        """Публикует пост в группу VK и возвращает ID поста"""
        if not cls.is_configured():
            print("❌ VK не настроен")
            return {'success': False, 'post_id': None}

        try:
            vk = cls.get_session().get_api()

            # Подготавливаем attachments
            attachments = []
            if images:
                print(f"🖼️ Загружаем {len(images)} изображений в VK...")
                for img_filename in images:
                    img_path = os.path.join(app.config['UPLOAD_FOLDER'], img_filename)
                    if os.path.exists(img_path):
                        photo_attachment = cls.upload_photo_to_vk(img_path)
                        if photo_attachment:
                            attachments.append(photo_attachment)
                    else:
                        print(f"⚠️ Файл не найден: {img_path}")

            # Публикуем пост
            post_params = {
                'owner_id': int(VK_GROUP_ID),
                'message': text or '',
                'from_group': 1
            }

            if attachments:
                post_params['attachments'] = ','.join(attachments)
                print(f"📎 Прикреплено {len(attachments)} вложений")

            post_result = vk.wall.post(**post_params)
            post_id = post_result.get('post_id')

            print(f"✅ Пост опубликован в VK (ID: {post_id})")
            return {'success': True, 'post_id': post_id}

        except Exception as e:
            print(f"❌ Ошибка публикации в VK: {e}")
            if "method is unavailable with service token" in str(e):
                print("⚠️  Нужен USER token VK, а не SERVICE token!")
            return {'success': False, 'post_id': None}

    @classmethod
    def delete_post(cls, post_id, owner_id=None):
        """Удаляет пост со стены VK"""
        if not cls.is_configured():
            return False

        try:
            vk = cls.get_session().get_api()
            params = {
                'owner_id': int(owner_id or VK_GROUP_ID),
                'post_id': int(post_id)
            }
            result = vk.wall.delete(**params)

            if result == 1:
                print(f"✅ Пост VK {post_id} успешно удален")
                return True
            else:
                print(f"❌ Не удалось удалить пост VK {post_id}")
                return False

        except Exception as e:
            print(f"❌ Ошибка удаления поста VK: {e}")
            return False


# ================== OK MANAGER ==================
class OKManager:
    """Менеджер для работы с Одноклассниками"""

    BASE_URL = "https://api.ok.ru/fb.do"
    UPLOAD_BASE_URL = "https://api.ok.ru/api/photosV2/"

    @classmethod
    def is_configured(cls):
        """Проверяет, настроены ли Одноклассники"""
        return bool(OK_ACCESS_TOKEN and OK_APPLICATION_KEY and OK_APPLICATION_SECRET and OK_GROUP_ID)

    @classmethod
    def _generate_sig(cls, params):
        """Генерация подписи для OK API"""
        try:
            sorted_params = sorted(params.items())
            param_string = ''.join(f"{key}={value}" for key, value in sorted_params)
            param_string = param_string + OK_APPLICATION_SECRET
            md5_hash = hashlib.md5(param_string.encode('utf-8')).hexdigest()
            return md5_hash.lower()
        except Exception as e:
            print(f"❌ Ошибка генерации подписи OK: {e}")
            return None

    @classmethod
    def _make_ok_post_request(cls, method, additional_params=None):
        """POST запрос к OK API"""
        try:
            params = {
                'application_key': OK_APPLICATION_KEY,
                'format': 'json',
                'method': method,
                'access_token': OK_ACCESS_TOKEN
            }

            if additional_params:
                params.update(additional_params)

            sig_params = {k: v for k, v in params.items() if k != 'access_token'}
            sig = cls._generate_sig(sig_params)

            if not sig:
                return None

            params['sig'] = sig
            response = requests.post(cls.BASE_URL, data=params, timeout=60)

            if response.status_code == 200:
                return response.json()
            else:
                print(f"❌ Ошибка HTTP {response.status_code} для метода {method}")
                return None

        except Exception as e:
            print(f"❌ Ошибка POST запроса к OK API ({method}): {e}")
            return None

    @classmethod
    def upload_photo_to_ok(cls, image_path):
        """Загружает фото на сервер OK с улучшенной обработкой ошибок"""
        try:
            filename = os.path.basename(image_path)
            print(f"📤 Загрузка фото в OK: {filename}")

            # Проверяем размер файла (OK обычно ограничивает 20-50MB)
            file_size = os.path.getsize(image_path) / (1024 * 1024)  # в MB
            if file_size > 20:
                print(f"⚠️  Файл слишком большой: {file_size:.1f}MB (рекомендуется до 20MB)")
                # Можно попробовать сжать
                return None

            # 1. Получаем upload URL
            print("  1. Получаем upload URL...")
            upload_url_result = cls._make_ok_post_request('photosV2.getUploadUrl', {
                'gid': OK_GROUP_ID
            })

            if not upload_url_result:
                print("❌ Не удалось получить upload_url: пустой ответ")
                return None

            if 'upload_url' not in upload_url_result:
                print(f"❌ Не удалось получить upload_url: {upload_url_result}")

                # Проверяем типичные ошибки
                if 'error_code' in upload_url_result:
                    error_code = upload_url_result.get('error_code')
                    error_msg = upload_url_result.get('error_msg', 'Неизвестная ошибка')
                    print(f"   Код ошибки: {error_code}, сообщение: {error_msg}")

                    # Частые ошибки:
                    if error_code == 104:  # Неверный ID группы
                        print("   ⚠️  Проверьте OK_GROUP_ID в настройках")
                    elif error_code == 100:  # Неверный access_token
                        print("   ⚠️  Проверьте OK_ACCESS_TOKEN в настройках")

                return None

            upload_url = upload_url_result['upload_url']
            print(f"✅ Получен upload URL")

            # 2. Загружаем фото
            print("  2. Загружаем фото...")
            try:
                with open(image_path, 'rb') as photo_file:
                    files = {'photo': photo_file}
                    upload_response = requests.post(upload_url, files=files, timeout=60)

                    if upload_response.status_code != 200:
                        print(f"❌ Ошибка загрузки фото: HTTP {upload_response.status_code}")
                        print(f"   Ответ сервера: {upload_response.text[:200]}")
                        return None

                    upload_result = upload_response.json()
                    print(f"   Ответ сервера OK: {json.dumps(upload_result, ensure_ascii=False)}")

                    # Извлекаем token фото
                    if 'photos' in upload_result and upload_result['photos']:
                        photos_dict = upload_result['photos']
                        first_photo_key = list(photos_dict.keys())[0]
                        token = photos_dict[first_photo_key].get('token')

                        if token:
                            print(f"✅ Фото загружено, получен token: {token[:30]}...")
                            return token
                        else:
                            print(f"❌ В ответе нет token: {upload_result}")
                            return None
                    else:
                        print(f"❌ Неожиданный формат ответа: нет ключа 'photos'")
                        return None

            except requests.exceptions.Timeout:
                print("❌ Таймаут при загрузке фото (60 секунд)")
                return None
            except IOError as e:
                print(f"❌ Ошибка чтения файла: {e}")
                return None

        except Exception as e:
            print(f"❌ Неожиданная ошибка загрузки фото в OK: {e}")
            import traceback
            traceback.print_exc()
            return None

    @classmethod
    def post_to_ok(cls, text, images=None):
        """Публикует пост в группу Одноклассников с поддержкой нескольких изображений"""
        if not cls.is_configured():
            print("❌ OK API не настроен")
            return {'success': False, 'topic_id': None}

        try:
            # Подготавливаем текст
            message = (text or 'Пост от авто-постинга')[:2000]

            # Пробуем загрузить изображения
            photo_tokens = []
            upload_errors = []

            if images and len(images) > 0:
                print(f"🖼️ Загрузка {len(images)} изображений для OK...")

                # OK имеет ограничения на количество фото в одном посте
                # Рекомендуется не более 10, но на практике часто работает 1-3
                max_images_for_ok = 3  # Ограничиваем для надежности
                images_to_process = images[:max_images_for_ok]

                for idx, img_filename in enumerate(images_to_process, 1):
                    img_path = os.path.join(app.config['UPLOAD_FOLDER'], img_filename)

                    if not os.path.exists(img_path):
                        print(f"⚠️ Файл не найден: {img_path}")
                        upload_errors.append(f"{img_filename} - файл не найден")
                        continue

                    print(f"  [{idx}/{len(images_to_process)}] Загружаем: {img_filename}")
                    token = cls.upload_photo_to_ok(img_path)

                    if token:
                        photo_tokens.append(token)
                        print(f"    ✅ Успешно загружено (token: {token[:20]}...)")
                    else:
                        error_msg = f"{img_filename} - ошибка загрузки"
                        upload_errors.append(error_msg)
                        print(f"    ❌ {error_msg}")

            # Формируем медиа блоки
            media_blocks = [{"type": "text", "text": message}]

            # Добавляем фото блок, если есть хотя бы одно фото
            if photo_tokens:
                if len(photo_tokens) == 1:
                    # Одно фото - простая структура
                    media_blocks.append({
                        "type": "photo",
                        "list": [{"id": photo_tokens[0]}]
                    })
                    print(f"✅ Добавлено 1 фото к посту OK")
                else:
                    # Несколько фото - сложная структура
                    # Внимание: OK часто принимает только первое фото из списка!
                    photo_list = []
                    for token in photo_tokens:
                        photo_list.append({"id": token})

                    media_blocks.append({
                        "type": "photo",
                        "list": photo_list
                    })
                    print(f"✅ Добавлено {len(photo_tokens)} фото к посту OK")

                    # Предупреждение о возможных проблемах
                    if len(photo_tokens) > 1:
                        print(f"⚠️  Внимание: OK может отображать только первое фото из {len(photo_tokens)}")
                        print(f"⚠️  Рекомендуется публиковать не более 1-2 фото для OK")
            else:
                print("ℹ️  Нет загруженных фото для OK, будет текстовый пост")

            # Добавляем информацию об ошибках загрузки в текст поста
            if upload_errors and len(photo_tokens) == 0:
                # Если не загрузилось ни одного фото, но были попытки
                error_text = f"\n\n⚠️ Не удалось загрузить изображения: {', '.join(upload_errors[:3])}"
                message += error_text[:500]

            attachment_json = {"media": media_blocks}

            # Публикуем пост
            print("📤 Публикация поста в OK...")
            result = cls._make_ok_post_request('mediatopic.post', {
                'gid': OK_GROUP_ID,
                'type': 'GROUP_THEME',
                'message': message,
                'attachment': json.dumps(attachment_json, ensure_ascii=False)
            })

            if result:
                # Извлекаем topic_id из ответа
                topic_id = None
                if isinstance(result, dict):
                    if 'topic_id' in result:
                        topic_id = result['topic_id']
                    elif 'error_code' in result:
                        error_code = result.get('error_code')
                        error_msg = result.get('error_msg', 'Неизвестная ошибка')
                        print(f"❌ Ошибка OK API ({error_code}): {error_msg}")

                        # Если ошибка из-за фото, пробуем без фото
                        if error_code in [4, 100] and photo_tokens:
                            print("⚠️  Пробуем отправить без фото...")
                            return cls._post_text_only(text)
                elif isinstance(result, (int, str)):
                    topic_id = str(result)

                if topic_id:
                    # Добавляем информацию о количестве загруженных фото
                    if photo_tokens:
                        if len(photo_tokens) == len(images):
                            print(f"✅ Пост опубликован в OK! ID: {topic_id} ({len(photo_tokens)} фото)")
                        else:
                            print(
                                f"✅ Пост опубликован в OK! ID: {topic_id} ({len(photo_tokens)} из {len(images)} фото)")
                    else:
                        print(f"✅ Текстовый пост опубликован в OK! ID: {topic_id}")

                    return {'success': True, 'topic_id': topic_id}
                else:
                    print(f"❌ Не удалось получить ID поста OK")
                    return {'success': False, 'topic_id': None}
            else:
                print(f"❌ Не удалось опубликовать пост в OK")
                return {'success': False, 'topic_id': None}

        except Exception as e:
            print(f"❌ Ошибка публикации в OK: {e}")
            import traceback
            traceback.print_exc()
            return {'success': False, 'topic_id': None}

    @classmethod
    def _post_text_only(cls, text):
        """Публикация только текста"""
        try:
            message = (text or '')[:500]

            result = cls._make_ok_post_request('mediatopic.post', {
                'gid': OK_GROUP_ID,
                'message': message,
                'attachment': '{"media":[{"type":"text","text":"' + message + '"}]}'
            })

            topic_id = None
            if result and isinstance(result, (int, str)):
                topic_id = str(result)

            if topic_id:
                print(f"✅ Текстовый пост опубликован в OK (ID: {topic_id})")
                return {'success': True, 'topic_id': topic_id}

            return {'success': False, 'topic_id': None}

        except Exception as e:
            print(f"❌ Ошибка текстовой публикации: {e}")
            return {'success': False, 'topic_id': None}

    @classmethod
    def delete_post(cls, post_id):
        """Удаляет медиатопик (пост) в Одноклассниках"""
        if not cls.is_configured():
            return False

        try:
            result = cls._make_ok_post_request('mediatopic.delete', {
                'topic_id': post_id,
                'gid': OK_GROUP_ID
            })

            # Успешный ответ: {"result": true}
            success = result.get('result', False) if result else False

            if success:
                print(f"✅ Пост OK {post_id} успешно удален")
                return True
            else:
                print(f"❌ Не удалось удалить пост OK {post_id}")
                return False

        except Exception as e:
            print(f"❌ Ошибка удаления поста OK: {e}")
            return False


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def compress_image(input_path, output_path, max_size=(1280, 1280), quality=85):
    """Сжимает изображение."""
    try:
        with Image.open(input_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = rgb_img

            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            img.save(output_path, 'JPEG', quality=quality, optimize=True)
            return True
    except Exception as e:
        print(f"❌ Ошибка сжатия изображения: {e}")
        return False


def cleanup_old_files():
    """Удаляет файлы старше указанного количества дней."""
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        return

    cutoff_time = time.time() - (app.config['DAYS_TO_KEEP'] * 24 * 60 * 60)

    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(filepath):
            try:
                if os.path.getmtime(filepath) < cutoff_time:
                    os.remove(filepath)
            except Exception as e:
                print(f"❌ Ошибка удаления файла {filename}: {e}")


# ================== РАБОТА С ОЧЕРЕДЬЮ ПОСТОВ ==================
def load_posts():
    """Загружает очередь постов из файла."""
    if not os.path.exists(DATA_FILE):
        save_posts([])
        return []

    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return []
            posts = json.loads(content)
            for post in posts:
                if 'platforms' not in post:
                    post['platforms'] = ['telegram', 'vk', 'ok']
            return posts
    except json.JSONDecodeError as e:
        print(f"❌ Ошибка парсинга JSON в posts: {e}")
        backup_file = f"{DATA_FILE}.backup_{int(time.time())}"
        if os.path.exists(DATA_FILE):
            os.rename(DATA_FILE, backup_file)
            print(f"⚠️  Создан backup поврежденного файла: {backup_file}")
        save_posts([])
        return []
    except Exception as e:
        print(f"❌ Ошибка загрузки постов: {e}")
        return []


def save_posts(posts):
    """Сохраняет очередь постов в файл."""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Ошибка сохранения постов: {e}")


def load_social_configs():
    """Загружает конфигурации для соцсетей"""
    default_configs = {
        'telegram': {'enabled': True, 'channel': TG_CHANNEL_ID},
        'vk': {'enabled': bool(VK_TOKEN and VK_GROUP_ID), 'group': VK_GROUP_ID},
        'ok': {'enabled': bool(OK_ACCESS_TOKEN and OK_APPLICATION_KEY and OK_APPLICATION_SECRET), 'group': OK_GROUP_ID}
    }

    if not os.path.exists(SOCIAL_CONFIGS_FILE):
        return default_configs

    try:
        with open(SOCIAL_CONFIGS_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return default_configs
            configs = json.loads(content)

            for key in default_configs:
                if key not in configs:
                    configs[key] = default_configs[key]
                else:
                    for subkey in default_configs[key]:
                        if subkey not in configs[key]:
                            configs[key][subkey] = default_configs[key][subkey]

            return configs
    except (json.JSONDecodeError, KeyError) as e:
        print(f"❌ Ошибка загрузки конфигураций: {e}")
        return default_configs


def save_social_configs(configs):
    """Сохраняет конфигурации соцсетей"""
    try:
        with open(SOCIAL_CONFIGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Ошибка сохранения конфигураций: {e}")


# ================== ОТПРАВКА И УДАЛЕНИЕ ПОСТОВ ==================
def send_to_social_media(images, caption, platforms=None):
    """
    Публикует пост на выбранных платформах
    Возвращает (результаты, идентификаторы постов)
    """
    if platforms is None:
        platforms = ['telegram', 'vk', 'ok']

    results = {'telegram': False, 'vk': False, 'ok': False}
    post_ids = {'telegram': None, 'vk': None, 'ok': None}

    # Telegram
    if 'telegram' in platforms:
        tg_result = TelegramManager.send_message(caption, images)
        results['telegram'] = tg_result['success']
        post_ids['telegram'] = tg_result['message_ids']

    # VK
    if 'vk' in platforms and VKManager.is_configured():
        vk_result = VKManager.post_to_vk(caption, images)
        results['vk'] = vk_result['success']
        post_ids['vk'] = vk_result['post_id']

    # Одноклассники
    if 'ok' in platforms and OKManager.is_configured():
        ok_result = OKManager.post_to_ok(caption, images)
        results['ok'] = ok_result['success']
        post_ids['ok'] = ok_result['topic_id']

    return results, post_ids


def delete_from_social_media(post_id, platform):
    """
    Удаляет пост с конкретной платформы
    post_id: ID поста на платформе
    platform: 'telegram', 'vk' или 'ok'
    """
    if platform == 'telegram':
        # Для Telegram ожидается список ID сообщений
        if isinstance(post_id, list):
            return TelegramManager.delete_messages(post_id)
        elif post_id:
            return TelegramManager.delete_messages([post_id])
        else:
            return False

    elif platform == 'vk':
        return VKManager.delete_post(post_id)

    elif platform == 'ok':
        return OKManager.delete_post(post_id)

    return False


# ================== ПЛАНИРОВЩИК ==================
def check_scheduled_posts():
    """Проверяет и публикует посты, у которых наступило время."""
    posts = load_posts()
    updated = False

    for post in posts:
        if post.get('published', False):
            continue

        scheduled_time = post.get('scheduled_time')
        if scheduled_time:
            if len(scheduled_time) == 16:
                scheduled_time = scheduled_time + ":00"

            try:
                post_time = datetime.fromisoformat(scheduled_time)
                if post_time <= datetime.now():
                    image_paths = post.get('image_paths', [])
                    caption = post.get('text', '') or post.get('caption', '')
                    platforms = post.get('platforms', ['telegram', 'vk', 'ok'])

                    # Публикуем на всех выбранных платформах
                    results, post_ids = send_to_social_media(image_paths, caption, platforms)

                    # Сохраняем результаты
                    post['published'] = True
                    post['published_at'] = datetime.now().isoformat()
                    post['publish_results'] = results
                    post['platform_post_ids'] = post_ids  # Сохраняем ID постов!

                    # Логируем результаты
                    print(f"📊 Результаты публикации поста #{post.get('id')}:")
                    for platform, success in results.items():
                        status = "✅ Успешно" if success else "❌ Ошибка"
                        print(f"  - {platform}: {status} (ID: {post_ids.get(platform)})")

                    updated = True
            except ValueError as e:
                print(f"❌ Ошибка парсинга времени: {e}")

    if updated:
        save_posts(posts)


def run_scheduler():
    """Запускает фоновый планировщик."""
    cleanup_old_files()

    schedule.every(30).seconds.do(check_scheduled_posts)
    schedule.every(24).hours.do(cleanup_old_files)

    while True:
        schedule.run_pending()
        time.sleep(1)


# ================== API ДЛЯ ЗАГРУЗКИ ФАЙЛОВ ==================
@app.route('/upload', methods=['POST'])
def upload_file():
    """Загружает один или несколько файлов на сервер."""
    if 'files[]' not in request.files:
        return jsonify({'success': False, 'error': 'Файлы не найдены'}), 400

    files = request.files.getlist('files[]')
    uploaded_files = []

    for file in files:
        if file.filename == '' or not allowed_file(file.filename):
            continue

        filename = secure_filename(file.filename)
        original_name, ext = os.path.splitext(filename)
        unique_name = f"{uuid.uuid4().hex}{ext.lower()}"

        original_path = os.path.join(app.config['UPLOAD_FOLDER'], f"original_{unique_name}")
        compressed_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)

        file.save(original_path)

        if app.config['COMPRESS_IMAGES'] and ext.lower() in ['.jpg', '.jpeg', '.png', '.webp']:
            if compress_image(original_path, compressed_path):
                try:
                    os.remove(original_path)
                except:
                    pass
                final_path = compressed_path
            else:
                final_path = original_path
        else:
            final_path = original_path

        uploaded_files.append({
            'success': True,
            'filename': os.path.basename(final_path),
            'original_name': filename,
            'url': f'/uploads/{os.path.basename(final_path)}'
        })

    return jsonify({'files': uploaded_files})


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Отдает загруженные файлы."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ================== ВЕБ-ИНТЕРФЕЙС ==================
@app.route('/')
def index():
    """Главная страница."""
    posts = load_posts()
    social_configs = load_social_configs()

    telegram_status = "✅ Настроен" if (TG_CHANNEL_ID and os.path.exists(f"{SESSION_NAME}.session")) else "❌ Не настроен"
    vk_status = "✅ Настроен" if VKManager.is_configured() else "❌ Не настроен"
    ok_status = "✅ Настроен" if OKManager.is_configured() else "❌ Не настроен"

    return render_template(
        'index.html',
        posts=posts,
        telegram_channel=TG_CHANNEL_ID,
        vk_group=VK_GROUP_ID,
        ok_group=OK_GROUP_ID,
        datetime=datetime,
        telegram_status=telegram_status,
        vk_status=vk_status,
        ok_status=ok_status,
        caption_limit=app.config['CAPTION_LIMIT'],
        social_configs=social_configs
    )


@app.route('/add', methods=['GET', 'POST'])
def add_post():
    """Добавление нового поста."""
    social_configs = load_social_configs()

    if request.method == 'POST':
        text = request.form.get('text', '').strip()
        image_paths_json = request.form.get('image_paths', '[]')
        scheduled_time = request.form.get('scheduled_time', '').strip()

        platforms = []
        if request.form.get('platform_telegram'):
            platforms.append('telegram')
        if request.form.get('platform_vk'):
            platforms.append('vk')
        if request.form.get('platform_ok'):
            platforms.append('ok')

        if not platforms:
            platforms = ['telegram', 'vk', 'ok']

        try:
            image_paths = json.loads(image_paths_json)
        except:
            image_paths = []

        if not text and not image_paths:
            flash('Укажите текст или выберите изображения', 'error')
            return redirect('/add')

        if len(image_paths) > app.config['MAX_ALBUM_SIZE']:
            image_paths = image_paths[:app.config['MAX_ALBUM_SIZE']]
            flash(f'Выбрано максимум {app.config["MAX_ALBUM_SIZE"]} изображений', 'warning')

        posts = load_posts()
        new_id = max([p['id'] for p in posts], default=0) + 1

        new_post = {
            'id': new_id,
            'text': text if text else None,
            'image_paths': image_paths,
            'scheduled_time': scheduled_time if scheduled_time else None,
            'platforms': platforms,
            'published': False,
            'created_at': datetime.now().isoformat()
        }

        posts.append(new_post)
        save_posts(posts)

        platforms_text = ', '.join(platforms)
        flash(f'Пост #{new_id} добавлен ({len(image_paths)} изображений, платформы: {platforms_text})', 'success')
        return redirect('/')

    return render_template('add_post.html',
                           max_album_size=app.config['MAX_ALBUM_SIZE'],
                           caption_limit=app.config['CAPTION_LIMIT'],
                           social_configs=social_configs)


@app.route('/publish_now/<int:post_id>')
def publish_now(post_id):
    """Немедленная публикация поста."""
    posts = load_posts()
    for post in posts:
        if post['id'] == post_id and not post.get('published', False):
            image_paths = post.get('image_paths', [])
            caption = post.get('text', '') or post.get('caption', '')
            platforms = post.get('platforms', ['telegram', 'vk', 'ok'])

            results, post_ids = send_to_social_media(image_paths, caption, platforms)

            success_count = sum(1 for r in results.values() if r)

            if success_count > 0:
                post['published'] = True
                post['published_at'] = datetime.now().isoformat()
                post['publish_results'] = results
                post['platform_post_ids'] = post_ids
                save_posts(posts)

                flash(f'Пост опубликован на {success_count}/{len(platforms)} платформах', 'success')
            else:
                flash('Ошибка при публикации на всех платформах', 'error')

            break

    return redirect('/')


@app.route('/delete/<int:post_id>')
def delete_post(post_id):
    """Удаление поста из очереди."""
    posts = [p for p in load_posts() if p['id'] != post_id]
    save_posts(posts)
    flash(f'Пост #{post_id} удален из очереди', 'info')
    return redirect('/')


@app.route('/delete_post/<int:post_id>/<platform>')
def delete_social_post(post_id, platform):
    """Удаляет пост с конкретной платформы."""
    posts = load_posts()
    target_post = next((p for p in posts if p['id'] == post_id), None)

    if not target_post or not target_post.get('published'):
        flash('Пост не найден или не опубликован', 'error')
        return redirect('/')

    platform_post_id = target_post.get('platform_post_ids', {}).get(platform)

    if not platform_post_id:
        flash(f'ID поста для {platform} не найден', 'error')
        return redirect('/')

    success = delete_from_social_media(platform_post_id, platform)

    if success:
        target_post['publish_results'][platform] = False
        flash(f'Пост удален из {platform}', 'success')
    else:
        flash(f'Ошибка удаления поста из {platform}', 'error')

    save_posts(posts)
    return redirect('/')


@app.route('/social_config', methods=['GET', 'POST'])
def social_config():
    """Настройка социальных сетей."""
    if request.method == 'POST':
        configs = {
            'telegram': {
                'enabled': bool(request.form.get('telegram_enabled')),
                'channel': request.form.get('telegram_channel', TG_CHANNEL_ID)
            },
            'vk': {
                'enabled': bool(request.form.get('vk_enabled')),
                'group': request.form.get('vk_group', VK_GROUP_ID)
            },
            'ok': {
                'enabled': bool(request.form.get('ok_enabled')),
                'group': request.form.get('ok_group', OK_GROUP_ID)
            }
        }

        save_social_configs(configs)
        flash('Настройки сохранены', 'success')
        return redirect('/')

    configs = load_social_configs()
    return render_template('social_config.html', configs=configs)


@app.route('/test_social/<platform>')
def test_social(platform):
    """Тестирование подключения к социальной сети."""
    test_text = f"Тестовый пост от {datetime.now().strftime('%d.%m.%Y %H:%M')}"

    if platform == 'telegram':
        result = TelegramManager.send_message(test_text)
        success = result['success']
        message = "✅ Телеграм: тестовый пост отправлен" if success else "❌ Телеграм: ошибка отправки"

    elif platform == 'vk' and VKManager.is_configured():
        result = VKManager.post_to_vk(test_text)
        success = result['success']
        message = "✅ VK: тестовый пост отправлен" if success else "❌ VK: ошибка отправки"

    elif platform == 'ok' and OKManager.is_configured():
        result = OKManager.post_to_ok(test_text)
        success = result['success']
        message = "✅ OK: тестовый пост отправлен" if success else "❌ OK: ошибка отправки"

    else:
        message = f"❌ Платформа {platform} не настроена"

    flash(message, 'success' if '✅' in message else 'error')
    return redirect('/')


# ================== ЗАПУСК ПРИЛОЖЕНИЯ ==================
if __name__ == '__main__':
    os.makedirs(os.path.dirname(SESSION_NAME) if os.path.dirname(SESSION_NAME) else '.', exist_ok=True)

    if not os.path.exists(DATA_FILE):
        save_posts([])
        print("✅ Создан файл posts_queue.json")

    if not os.path.exists(SOCIAL_CONFIGS_FILE):
        save_social_configs({})
        print("✅ Создан файл social_configs.json")

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    print("=" * 60)
    print("🚀 Мультиплатформенный автопостинг запущен")
    print("=" * 60)
    print("📱 Поддерживаемые платформы:")
    print(f"   • Telegram: {'✅ Настроен' if TG_CHANNEL_ID else '❌ Не настроен'}")
    print(f"   • VK: {'✅ Настроен' if VKManager.is_configured() else '❌ Не настроен'}")
    print(f"   • Одноклассники: {'✅ Настроен' if OKManager.is_configured() else '❌ Не настроен'}")
    print("=" * 60)
    print("🗑️  Удаление постов: доступно для всех платформ")
    print("🌐 Веб-интерфейс: http://localhost:5000")
    print("📸 Поддержка альбомов: до 10 фото")
    print(f"📝 Лимит подписей в Telegram: {app.config['CAPTION_LIMIT']} символов")
    print("=" * 60)

    if TelegramManager.is_authorized():
        print("✅ Telethon: клиент авторизован")
    else:
        print("⚠️ Telethon: клиент не авторизован")
        print("⚠️  Запустите скрипт авторизации: python auth_telegram.py")

    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)
