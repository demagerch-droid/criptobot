import logging
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = "8330326273:AAEuWSwkqi7ypz1LZL4LXRr2jSMpKjGc36k"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)


@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Нажми меня ✅", callback_data="test_button"))
    await message.answer("Тестовый бот запущен.\nНажми на кнопку ниже:", reply_markup=kb)


@dp.callback_query_handler(lambda c: True)
async def cb_any(call: types.CallbackQuery):
    logger.info(f"CALLBACK DATA = {call.data}")
    await call.answer("Кнопка работает ✅", show_alert=True)
    await call.message.answer(f"Пришёл callback: <code>{call.data}</code>")


if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        allowed_updates=["message", "callback_query"],
    )
