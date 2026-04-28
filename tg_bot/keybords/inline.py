from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_example_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Пример заполнения", callback_data="example")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])
    return keyboard