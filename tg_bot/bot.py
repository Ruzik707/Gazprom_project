import asyncio
import logging
import os

import pandas as pd
import numpy as np
import io
import pickle as pk
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, \
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage

from catboost import CatBoostRegressor, Pool
from sentence_transformers import SentenceTransformer

load_dotenv()

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
TOKEN = os.getenv('BOT_TOKEN')

MODEL_PATH = os.getenv("MODEL_PATH", "../models/best_model.cbm")
PCA_PATH = os.getenv("PCA_PATH", "../models/pca_model.pkl")
DATASET_PATH = os.getenv("DATASET_PATH", "data/processed/clean_dataset.parquet")
EMB_MODEL_NAME = os.getenv("EMB_MODEL_NAME", "BAAI/bge-m3")

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

catboost_model = None
pca_model = None
text_model = None
df_stats = None
df_train = None


# ==================== ЗАГРУЗКА МОДЕЛЕЙ И ДАТАСЕТА  ====================
def load_models():
    """Загружает все модели один раз при старте"""
    global catboost_model, pca_model, text_model, df_stats, df_train

    try:
        logger.info("🔄 Загрузка CatBoost модели...")
        catboost_model = CatBoostRegressor()
        catboost_model.load_model(MODEL_PATH)
        logger.info("✅ CatBoost модель загружена")

        logger.info("🔄 Загрузка PCA модели...")
        with open(PCA_PATH, 'rb') as file:
            pca_model = pk.load(file)
        logger.info("✅ PCA модель загружена")

        logger.info("🔄 Загрузка текстовой модели (это может занять время)...")
        text_model = SentenceTransformer(EMB_MODEL_NAME, device='cpu')
        logger.info("✅ Текстовая модель загружена")

        logger.info("🔄 Загрузка датасета...")
        df_train = pd.read_parquet(DATASET_PATH)
        logger.info("✅ Текстовая модель загружена")
        df_stats = compute_stats(df_train)
        logger.info("✅ Статистики для imputation вычислены")

        logger.info("🎉 Все модели успешно загружены!")
        return True

    except Exception as e:
        logger.error(f"❌ Ошибка загрузки моделей: {e}")
        return False


# ==================== Статистика датасета (для пропусков) ====================
def compute_stats(df):
    stats = {}
    num_f = ['area_kitchen', 'construction_year', 'ceiling_height', 'metros_count', 'metros_min_time', 'elevators_int',
             'bathroom_int']
    cat_f = ['flat_type', 'renovation_type', 'parking', 'house_type']

    for col in num_f:
        stats[col] = df.groupby('district')[col].median().astype(int)
    for col in cat_f:
        stats[col] = df.groupby('district')[col].agg(lambda x: x.mode()[0] if len(x.mode()) > 0 else 'Не указано')
    return stats


# ==================== FSM СОСТОЯНИЯ ====================
class UploadForm(StatesGroup):
    waiting_for_file = State()
    waiting_for_cat_features = State()
    waiting_for_description = State()


# ==================== КЛАВИАТУРЫ ====================
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🏠 Оценить квартиру")],
        [KeyboardButton(text="ℹ️ О боте")]
    ], resize_keyboard=True)


def cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True)


CATEGORIES = {
    'district': ['ЦАО', 'ЮАО', 'САО', 'ЗАО', 'СВАО', 'ЮЗАО', 'ВАО', 'ЮВАО', 'СЗАО', 'НАО', 'ТАО', 'ЗелАО'],
    'flat_type': ['Вторичка', 'Новостройка', 'Новостройка/Апартаменты', 'Вторичка/Апартаменты', 'Вторичка/Пентхаус',
                  'Новостройка/Пентхаус'],
    'renovation_type': ['Без ремонта', 'Дизайнерский', 'Косметический', 'Не указано', 'Евроремонт'],
    'parking': ['Нет', 'Наземная', 'Подземная', 'Многоуровневая', 'Открытая', 'На крыше'],
    'house_type': ['Кирпичный', 'Монолитный', 'Монолитно-кирпичный', 'Не указано', 'Панельный', 'Блочный', 'Сталинский',
                   'Каркасный', 'Деревянный']
}

FEATURE_MAPPING = {
    'Количество комнат': 'rooms',
    'Общая площадь': 'area_total',
    'Жилая площадь': 'area_living',
    'Площадь кухни': 'area_kitchen',
    'Этаж': 'floor',
    'Всего этажей': 'floors_total',
    'Год постройки': 'construction_year',
    'Количество метро': 'metros_count',
    'Минимальное время до метро': 'metros_min_time',
    'Высота потолков': 'ceiling_height',
    'Количество лифтов': 'elevators_int',
    'Количество ванных': 'bathroom_int',
}

CAT_MAPPING = {
    'district': 'Район',
    'flat_type': 'Тип',
    'renovation_type': 'Ремонт',
    'parking': 'Паркинг',
    'house_type': 'Дом'
}


# ==================== FEATURE ENGINEERING ====================
def create_features(user_input: dict) -> pd.DataFrame:
    """Превращает данные от пользователя в фичи для модели"""

    df = pd.DataFrame([user_input])

    current_year = datetime.now().year
    construction_year = df['construction_year'].iloc[0]
    if pd.notna(construction_year) and 1800 < construction_year < current_year + 10:
        df['building_age'] = current_year - construction_year
    else:
        df['building_age'] = 0

    rooms = df['rooms'].iloc[0]
    area_total = df['area_total'].iloc[0]
    if pd.notna(rooms) and rooms > 0:
        df['area_per_room'] = area_total / rooms
    else:
        df['area_per_room'] = area_total

    floors_total = df['floors_total'].iloc[0]
    floor = df['floor'].iloc[0]
    if pd.notna(floors_total) and floors_total > 0:
        df['floor_ratio'] = floor / floors_total
    else:
        df['floor_ratio'] = 0

    area_kitchen = df['area_kitchen'].iloc[0]
    if pd.notna(area_kitchen) and pd.notna(area_total) and area_total > 0:
        df['kitchen_ratio'] = area_kitchen / area_total
    else:
        df['kitchen_ratio'] = 0

    area_living = df['area_living'].iloc[0]
    if pd.notna(area_living) and pd.notna(area_total) and area_total > 0:
        df['living_ratio'] = area_living / area_total
    else:
        df['living_ratio'] = 0

    df['has_parking'] = (df['parking'] != 'Нет').astype(int)

    df['has_elevator'] = (df['elevators_int'] > 0).astype(int)

    df['is_new_building'] = (df['building_age'] <= 5).astype(int)

    emb_reduced = np.zeros((1, 100))
    if text_model is not None and pca_model is not None and 'offer_description' in df.columns:
        try:
            description = user_input.get('offer_description', '')
            if description and str(description).strip():
                emb = text_model.encode([str(description)],
                                        show_progress_bar=False,
                                        convert_to_numpy=True)

                emb_reduced = pca_model.transform(emb)
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при создании эмбеддингов: {e}")
    emb_df = pd.DataFrame(
        emb_reduced,
        columns=[f'emb_{i}' for i in range(emb_reduced.shape[1])],
        index=df.index
    )

    return pd.concat([df, emb_df], axis=1)


def get_feature_columns():
    """Возвращает список колонок в порядке, ожидаемом моделью"""
    base_numeric = [
        'rooms', 'area_total', 'area_living', 'area_kitchen', 'floor', 'floors_total',
        'construction_year', 'metros_count', 'metros_min_time', 'building_age',
        'area_per_room', 'ceiling_height', 'floor_ratio', 'kitchen_ratio', 'living_ratio',
        'has_parking', 'has_elevator', 'is_new_building',
        'elevators_int', 'bathroom_int',
    ]

    cat_features = ['district', 'flat_type', 'renovation_type', 'parking', 'house_type']

    emb_features = [f'emb_{i}' for i in range(100)]

    return base_numeric + cat_features + emb_features


# ==================== ПРЕДСКАЗАНИЕ ====================
def format_price(price: float) -> str:
    """Форматирует цену в читаемый вид"""
    return f"{price:,.0f} ₽".replace(",", " ")


def get_confidence_message(r2_score: float = 0.93) -> str:
    """Возвращает сообщение об уверенности модели"""
    if r2_score > 0.9:
        return "✨ Высокая уверенность в результате"
    elif r2_score > 0.8:
        return "📊 Средняя уверенность в результате"
    else:
        return "🔍 Оценка приблизительная"


def predict_price(user_input: dict) -> dict:
    """
    Главная функция предсказания.
    Возвращает dict с ценой, доверием и дополнительными данными.
    """
    global catboost_model, pca_model, text_model

    if catboost_model is None:
        return {"error": "Модель не загружена"}

    try:
        features_df = create_features(user_input)
        feature_columns = get_feature_columns()

        for col in feature_columns:
            if col not in features_df.columns:
                if col in ['district', 'flat_type', 'renovation_type', 'parking', 'house_type']:
                    features_df[col] = 'Не указано'
                elif col.startswith('emb_'):
                    features_df[col] = 0.0
                else:
                    features_df[col] = 0.0

        features_df = features_df[feature_columns]

        cat_features = ['district', 'flat_type', 'renovation_type', 'parking', 'house_type']
        cat_features = [c for c in cat_features if c in features_df.columns]

        pool = Pool(features_df, cat_features=cat_features)

        log_price = catboost_model.predict(pool)[0]
        price = np.expm1(log_price)

        price = max(1_000_000, min(price, 500_000_000))

        return {
            "success": True,
            "price": round(price),
            "price_formatted": format_price(price),
            "confidence": "high" if price > 5_000_000 else "medium",
            "message": get_confidence_message()
        }

    except Exception as e:
        logger.error(f"❌ Ошибка предсказания: {e}")
        return {"error": f"Ошибка модели: {str(e)}"}


def format_prediction_result(result: dict, user_input: dict) -> str:
    """Форматирует результат предсказания в красивый текст"""

    if not result.get("success"):
        return f"❌ {result.get('error', 'Неизвестная ошибка')}"

    summary = []
    if user_input.get('rooms'):
        summary.append(f"🛏 {int(user_input['rooms'])} комн.")
    if user_input.get('area_total'):
        summary.append(f"📐 {user_input['area_total']} м²")
    if user_input.get('district'):
        summary.append(f"📍 {user_input['district']}")

    return f"""
🏠 <b>Результат оценки квартиры</b>

{' | '.join(summary)}

💰 <b>{result['price_formatted']}</b>

{result['message']}

📈 <i>Модель проанализировала 25 000+ объявлений, учитывая район, метраж, этаж, ремонт и описание.</i>

💡 <b>Совет:</b> для более точной оценки добавьте:
• детали о ремонте
• информацию о инфраструктуре

🔄 <i>Хотите оценить другую квартиру? Нажмите "🏠 Оценить квартиру"</i>
    """.strip()


# ==================== ОБРАБОТЧИКИ БОТА ====================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот для оценки квартир в Москве.\n\n"
        "🔹 <b>🏠 Оценить квартиру</b> — загрузите Excel с параметрами\n"
        "🔹 <b>ℹ️ О боте</b> — информация о модели",
        reply_markup=main_kb(),
        parse_mode="HTML"
    )


@dp.message(F.text == "ℹ️ О боте")
async def about(message: Message):
    await message.answer(
        "🤖 <b>О боте</b>\n\n"
        "🧠 <b>Технологии:</b>\n"
        "• CatBoost для градиентного бустинга\n"
        "• BGE-M3 для текстовых эмбеддингов\n"
        "• PCA для сокращения размерности\n\n"
        "📊 <b>Метрики:</b>\n"
        "• R² ≈ 0.93\n"
        "• MAPE ≈ 13%\n"
        "• Обучено на 25 000+ объявлениях",
        parse_mode="HTML",
        reply_markup=main_kb()
    )


@dp.message(F.text == "🏠 Оценить квартиру")
async def start_evaluation(message: Message, state: FSMContext):
    """Отправляем шаблон и ждем файл"""
    await state.set_state(UploadForm.waiting_for_file)

    template = FSInputFile("/content/drive/MyDrive/PriceVision/Шаблон.xlsx")
    await bot.send_document(
        message.chat.id,
        template,
        caption="📋 <b>Заполните шаблон и отправьте файл:</b>\n\n"
                "✅ <b>Обязательно:</b>\n"
                "• Количество комнат\n"
                "• Общая площадь (м²)\n"
                "• Жилая площадь (м²)\n"
                "• Этаж / Всего этажей\n\n"
                "Чем больше данных вы заполните, тем точнее будет оценка!\n\n"
                "📤 Жду ваш файл 👇",
        parse_mode="HTML", reply_markup=cancel_kb()
    )


@dp.message(F.document, UploadForm.waiting_for_file)
async def process_excel(message: Message, state: FSMContext):
    try:
        file = await bot.get_file(message.document.file_id)
        file_content = await bot.download_file(file.file_path)

        df_excel = pd.read_excel(io.BytesIO(file_content.read()), header=None)

        features = {}
        for i in range(len(df_excel)):
            name = str(df_excel.iloc[i, 0]).strip() if pd.notna(df_excel.iloc[i, 0]) else None
            value = df_excel.iloc[i, 1] if len(df_excel.columns) > 1 and pd.notna(df_excel.iloc[i, 1]) else None
            if name and name in FEATURE_MAPPING:
                features[FEATURE_MAPPING[name]] = value

        REQUIRED = ['rooms', 'area_total', 'area_living', 'floor', 'floors_total']
        missing = [k for k in REQUIRED if k not in features or pd.isna(features[k])]

        if missing:
            req_names = {
                'rooms': 'Количество комнат', 'area_total': 'Общая площадь',
                'area_living': 'Жилая площадь', 'floor': 'Этаж', 'floors_total': 'Всего этажей'
            }
            missing_names = [req_names.get(k, k) for k in missing]
            await message.answer(
                f"❌ <b>Обязательные числовые поля:</b>\n\n"
                f"• {', '.join(missing_names)}\n\n"
                f"📋 Шаблон Excel:\n"
                f"Количество комнат | 2\n"
                f"Общая площадь | 55.5\n"
                f"Жилая площадь | 35.0\n"
                f"Этаж | 7\nВсего этажей | 25\n\n"
                f"🔄 Отправьте файл 👇",
                parse_mode="HTML", reply_markup=cancel_kb()
            )
            return

        numeric_cols = list(FEATURE_MAPPING.values())
        for col in numeric_cols:
            if col in features and not pd.isna(features[col]):
                features[col] = float(features[col])

        imputed = []
        NUM_OPTIONAL = ['area_kitchen', 'construction_year', 'ceiling_height',
                        'metros_count', 'metros_min_time', 'elevators_int', 'bathroom_int']

        await state.update_data(raw_features=features)

        preview = f"✅ <b>Числовые данные приняты:</b>\n"
        preview += f"• 🛏 {int(features['rooms'])} комнат\n"
        preview += f"• 📐 {features['area_total']} м² (жилая: {features['area_living']})\n"
        preview += f"• 🏢 {int(features['floor'])}/{int(features['floors_total'])}"
        cat_preview = f"📍 <b>Выберите Район и категории:</b>"

        await message.answer(preview, parse_mode="HTML")
        await message.answer(cat_preview, parse_mode="HTML", reply_markup=await cat_keyboard({}))
        await state.set_state(UploadForm.waiting_for_cat_features)

    except Exception as e:
        logger.error(f"Ошибка Excel: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}\n🔄 Excel: 'Поле | Значение'",
                             parse_mode="HTML", reply_markup=cancel_kb())
        await state.clear()


async def cat_keyboard(current_cats: dict = None) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру для выбора категорий"""
    if current_cats is None:
        current_cats = {}

    keyboard = []
    cat_display = {
        'district': '📍 Район',
        'flat_type': '🏠 Тип',
        'renovation_type': '🛠 Ремонт',
        'parking': '🚗 Паркинг',
        'house_type': '🏗 Дом'
    }

    for cat_name, options in CATEGORIES.items():
        selected = current_cats.get(cat_name, '—')
        keyboard.append([InlineKeyboardButton(
            text=f"{cat_display[cat_name]}: {selected}",
            callback_data=f"cat|{cat_name}"
        )])

    keyboard.append([InlineKeyboardButton(text="✅ Готово!", callback_data="cat|done")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.callback_query(F.data.startswith("cat|"), UploadForm.waiting_for_cat_features)
async def process_category(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("|")
    action = parts[1]

    if action == "done":
        data = await state.get_data()
        categories = data.get('categories', {})

        if 'district' not in categories:
            await callback.answer("❌ Выберите Район! Это обязательный параметр", show_alert=True)
            return

        district = categories['district']
        raw_features = data.get('raw_features', {})
        features = raw_features.copy()
        imputed = []

        NUM_OPTIONAL = ['area_kitchen', 'construction_year', 'ceiling_height',
                        'metros_count', 'metros_min_time', 'elevators_int', 'bathroom_int']
        for col in NUM_OPTIONAL:
            if col not in features or pd.isna(features[col]):
                try:
                    median_val = df_stats[col][district]
                    features[col] = float(median_val)
                    imputed.append(f"{col}={median_val:.1f}")
                except (KeyError, IndexError):
                    features[col] = 0.0
                    imputed.append(f"{col}=0.0")

        user_categories = {k: categories[k] for k in data.get('categories', {})}

        for cat in ['flat_type', 'renovation_type', 'parking', 'house_type']:
            if cat not in categories:
                try:
                    categories[cat] = df_stats[cat][district]
                except (KeyError, IndexError):
                    categories[cat] = 'Не указано'

        await state.update_data(features=features, categories=categories, user_categories=user_categories,
                                imputed=imputed, district=district)

        cats_preview = "\n".join([f"• {CAT_MAPPING.get(k, k)}: {v}" for k, v in user_categories.items()])

        await callback.message.edit_text(
            f"✅ <b>Ваши параметры:</b>\n{cats_preview}\n\n"
            f"📝 <b>Описание квартиры (можно пропустить):</b> "
            f"<i>Ремонт, вид из окна, планировка, инфраструктура...</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭️ Пропустить", callback_data="skip_desc")],
                [InlineKeyboardButton(text="🔄 Изменить категории", callback_data="cat|back")]
            ])
        )
        await state.set_state(UploadForm.waiting_for_description)
        await callback.answer("✅ Готово!")
        return

    if action == "back":
        data = await state.get_data()
        cats = data.get('categories', {})
        kb = await cat_keyboard(cats)
        await callback.message.edit_text("📋 <b>Выберите категории:</b>",
                                         reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    cat_type = action
    options_kb = []
    for option in CATEGORIES.get(cat_type, []):
        options_kb.append([InlineKeyboardButton(
            text=option,
            callback_data=f"select|{cat_type}|{option}"
        )])
    options_kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="cat|back")])

    cat_name = CAT_MAPPING.get(cat_type, cat_type)
    await callback.message.edit_text(
        f"🏠 <b>Выберите {cat_name.lower()}:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=options_kb),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "skip_desc", UploadForm.waiting_for_description)
async def skip_description(callback: CallbackQuery, state: FSMContext):
    """Пропуск описания"""
    data = await state.get_data()
    features = data.get('features', {})
    categories = data.get('categories', {})
    user_input = {**features, **categories, 'offer_description': ''}

    await callback.message.edit_text("🔄 Предсказываю цену...")
    result = predict_price(user_input)

    summary = format_prediction_result(result, user_input)
    await callback.message.answer(summary, parse_mode="HTML", reply_markup=main_kb())
    await state.clear()


@dp.callback_query(F.data == "cat|back", UploadForm.waiting_for_description)
async def back_to_categories(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору категорий"""
    await state.set_state(UploadForm.waiting_for_cat_features)

    data = await state.get_data()
    categories = data.get('user_categories', {})

    await callback.message.edit_text(
        "📋 <b>Выберите категории:</b>",
        reply_markup=await cat_keyboard(categories),
        parse_mode="HTML"
    )
    await callback.answer("🔄 Изменение категорий")


@dp.callback_query(F.data.startswith("select|"))
async def select_category(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("|")
    cat_type, value = parts[1], parts[2]

    data = await state.get_data()
    cats = data.get('categories', {})
    cats[cat_type] = value
    await state.update_data(categories=cats)

    kb = await cat_keyboard(cats)
    await callback.message.edit_text(f"✅ {CAT_MAPPING.get(cat_type, cat_type)} = {value}",
                                     reply_markup=kb)
    await callback.answer(f"✅ Выбрано: {value}")


@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено. Выберите действие:", reply_markup=main_kb())


@dp.message(UploadForm.waiting_for_description)
async def save_description_and_predict(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("❌ Отменено! 🎉", reply_markup=main_kb())
        return

    description = message.text.strip() or ""
    data = await state.get_data()

    features = data.get('features', {})
    categories = data.get('categories', {})
    user_input = {**features, **categories, 'offer_description': description}

    await message.answer("🔄 Предсказываю цену...")
    result = predict_price(user_input)
    await message.answer(format_prediction_result(result, user_input),
                         parse_mode="HTML", reply_markup=main_kb())
    await state.clear()


# ==================== ЗАПУСК ====================
async def main():
    if not load_models():
        logger.error("❌ Не удалось загрузить модели. Бот не запустится.")
        return

    print("🤖 Бот запущен! Ожидаю сообщения...")
    await dp.start_polling(bot)
