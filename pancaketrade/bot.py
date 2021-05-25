"""Bot class."""
import time
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Defaults, Updater
from web3 import Web3

from pancaketrade.conversations import (
    AddOrderConversation,
    AddTokenConversation,
    BuySellConversation,
    EditTokenConversation,
    RemoveOrderConversation,
    RemoveTokenConversation,
    SellAllConversation,
)
from pancaketrade.network import Network
from pancaketrade.persistence import db
from pancaketrade.utils.config import Config
from pancaketrade.utils.db import get_token_watchers, init_db
from pancaketrade.utils.generic import chat_message, check_chat_id, format_token_amount, get_tokens_keyboard_layout
from pancaketrade.watchers import OrderWatcher, TokenWatcher


class TradeBot:
    """Bot class."""

    def __init__(self, config: Config):
        self.config = config
        self.db = db
        init_db()
        self.net = Network(
            rpc=self.config.bsc_rpc,
            wallet=self.config.wallet,
            min_pool_size_bnb=self.config.min_pool_size_bnb,
            secrets=self.config.secrets,
        )
        defaults = Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True, timeout=120)
        # persistence = PicklePersistence(filename='botpersistence')
        self.updater = Updater(token=config.secrets.telegram_token, persistence=None, defaults=defaults)
        self.dispatcher = self.updater.dispatcher
        self.convos = {
            'addtoken': AddTokenConversation(parent=self, config=self.config),
            'edittoken': EditTokenConversation(parent=self, config=self.config),
            'removetoken': RemoveTokenConversation(parent=self, config=self.config),
            'addorder': AddOrderConversation(parent=self, config=self.config),
            'removeorder': RemoveOrderConversation(parent=self, config=self.config),
            'sellall': SellAllConversation(parent=self, config=self.config),
            'buysell': BuySellConversation(parent=self, config=self.config),
        }
        self.setup_telegram()
        self.watchers: Dict[str, TokenWatcher] = get_token_watchers(
            net=self.net, dispatcher=self.dispatcher, config=self.config
        )
        self.status_scheduler = BackgroundScheduler(
            job_defaults={
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': 20,
            }
        )
        self.start_status_update()
        self.last_status_message_id: Optional[int] = None
        self.prompts_select_token = {
            'sellall': 'Sell full blance now for which token?',
            'addorder': 'Add order to which token?',
            'removeorder': 'Delete order for which token?',
            'buysell': 'Buy or sell now which token?',
            'address': 'Get address for which token?',
            'edittoken': 'Edit which token icon and slippage?',
            'removetoken': 'Which token do you want to remove?',
        }

    def setup_telegram(self):
        self.dispatcher.add_handler(CommandHandler('start', self.command_start))
        self.dispatcher.add_handler(CommandHandler('status', self.command_status))
        self.dispatcher.add_handler(CommandHandler('sellall', self.command_show_all_tokens))
        self.dispatcher.add_handler(CommandHandler('addorder', self.command_show_all_tokens))
        self.dispatcher.add_handler(CommandHandler('removeorder', self.command_show_all_tokens))
        self.dispatcher.add_handler(CommandHandler('buysell', self.command_show_all_tokens))
        self.dispatcher.add_handler(CommandHandler('address', self.command_show_all_tokens))
        self.dispatcher.add_handler(CommandHandler('edittoken', self.command_show_all_tokens))
        self.dispatcher.add_handler(CommandHandler('removetoken', self.command_show_all_tokens))
        self.dispatcher.add_handler(CommandHandler('order', self.command_order))
        self.dispatcher.add_handler(CallbackQueryHandler(self.command_address, pattern='^address:0x[a-fA-F0-9]{40}$'))
        self.dispatcher.add_handler(
            CallbackQueryHandler(
                self.command_show_all_tokens, pattern='^addorder$|^removeorder$|^buysell$|^sellall$|^address$'
            )
        )
        self.dispatcher.add_handler(CallbackQueryHandler(self.cancel_command, pattern='^canceltokenchoice$'))
        for convo in self.convos.values():
            self.dispatcher.add_handler(convo.handler)
        commands = [
            ('status', 'display all tokens and their price, orders'),
            ('buysell', 'buy or sell a token now'),
            ('addorder', 'add order to one of the tokens'),
            ('removeorder', 'delete order for one of the tokens'),
            ('order', 'display order information, pass the order ID as argument'),
            ('addtoken', 'add a token that you want to trade'),
            ('removetoken', 'remove a token that you added'),
            ('edittoken', 'edit token emoji and slippage'),
            ('address', 'get the contract address for a token'),
            ('cancel', 'cancel current operation'),
        ]
        self.dispatcher.bot.set_my_commands(commands=commands)
        self.dispatcher.add_error_handler(self.error_handler)

    def start_status_update(self):
        if not self.config.update_messages:
            return
        trigger = IntervalTrigger(seconds=30)
        self.status_scheduler.add_job(self.update_status, trigger=trigger)
        self.status_scheduler.start()

    def start(self):
        try:
            self.dispatcher.bot.send_message(chat_id=self.config.secrets.admin_chat_id, text='🤖 Bot started')
        except Exception:  # chat doesn't exist yet, do nothing
            logger.info('Chat with user doesn\'t exist yet.')
        logger.info('Bot started')
        self.updater.start_polling()
        self.updater.idle()

    @check_chat_id
    def command_start(self, update: Update, context: CallbackContext):
        chat_message(
            update,
            context,
            text='Hi! You can start adding tokens that you want to trade with the '
            + '<a href="/addtoken">/addtoken</a> command.',
            edit=False,
        )

    @check_chat_id
    def command_status(self, update: Update, context: CallbackContext):
        self.pause_status_update(True)  # prevent running an update while we are changing the last message id
        sorted_tokens = sorted(self.watchers.values(), key=lambda token: token.symbol.lower())
        for token in sorted_tokens:
            status = self.get_token_status(token)
            msg = chat_message(update, context, text=status, edit=False)
            if msg is not None:
                self.watchers[token.address].last_status_message_id = msg.message_id
        message, buttons = self.get_summary_message()
        reply_markup = InlineKeyboardMarkup(buttons)
        stat_msg = chat_message(
            update,
            context,
            text=message,
            reply_markup=reply_markup,
            edit=False,
        )
        if stat_msg is not None:
            self.last_status_message_id = stat_msg.message_id
        time.sleep(1)  # make sure the message go received by the telegram API
        self.pause_status_update(False)  # resume update job

    @check_chat_id
    def command_order(self, update: Update, context: CallbackContext):
        error_msg = 'You need to provide the order ID number as argument to this command, like <code>/order 12</code>.'
        if context.args is None:
            chat_message(update, context, text=error_msg, edit=False)
            return
        try:
            order_id = int(context.args[0])
        except Exception:
            chat_message(update, context, text=error_msg, edit=False)
            return
        order: Optional[OrderWatcher] = None
        for token in self.watchers.values():
            for o in token.orders:
                if o.order_record.id != order_id:
                    continue
                order = o
        if not order:
            chat_message(update, context, text='⛔️ Could not find order with this ID.', edit=False)
            return
        chat_message(update, context, text=order.long_repr(), edit=False)

    @check_chat_id
    def command_address(self, update: Update, context: CallbackContext):
        assert update.callback_query
        query = update.callback_query
        assert query.data
        token_address = query.data.split(':')[1]
        if not Web3.isChecksumAddress(token_address):
            chat_message(update, context, text='⛔️ Invalid token address.', edit=self.config.update_messages)
            return
        token = self.watchers[token_address]
        chat_message(
            update, context, text=f'{token.name}\n<code>{token_address}</code>', edit=self.config.update_messages
        )

    @check_chat_id
    def command_show_all_tokens(self, update: Update, context: CallbackContext):
        if update.message:
            assert update.message.text
            command = update.message.text.strip()[1:]
            try:
                msg = self.prompts_select_token[command]
            except KeyError:
                chat_message(update, context, text='⛔️ Invalid command.', edit=False)
                return
            buttons_layout = get_tokens_keyboard_layout(self.watchers, callback_prefix=command)
        else:  # callback query from button
            assert update.callback_query
            query = update.callback_query
            assert query.data
            try:
                msg = self.prompts_select_token[query.data]
            except KeyError:
                chat_message(update, context, text='⛔️ Invalid command.', edit=False)
                return
            buttons_layout = get_tokens_keyboard_layout(self.watchers, callback_prefix=query.data)
        reply_markup = InlineKeyboardMarkup(buttons_layout)
        chat_message(
            update,
            context,
            text=msg,
            reply_markup=reply_markup,
            edit=False,
        )

    @check_chat_id
    def cancel_command(self, update: Update, _: CallbackContext):
        assert update.callback_query and update.effective_chat
        query = update.callback_query
        query.delete_message()

    def update_status(self):
        if self.last_status_message_id is None:
            return  # we probably did not call status since start
        sorted_tokens = sorted(self.watchers.values(), key=lambda token: token.symbol.lower())
        for token in sorted_tokens:
            if token.last_status_message_id is None:
                continue
            status = self.get_token_status(token)
            self.dispatcher.bot.edit_message_text(
                status,
                chat_id=self.config.secrets.admin_chat_id,
                message_id=token.last_status_message_id,
            )
        message, buttons = self.get_summary_message()
        reply_markup = InlineKeyboardMarkup(buttons)
        self.dispatcher.bot.edit_message_text(
            message,
            chat_id=self.config.secrets.admin_chat_id,
            message_id=self.last_status_message_id,
            reply_markup=reply_markup,
        )

    def get_token_status(self, token: TokenWatcher) -> str:
        token_balance = self.net.get_token_balance(token_address=token.address)
        token_balance_bnb = self.net.get_token_balance_bnb(token_address=token.address, balance=token_balance)
        token_balance_usd = self.net.get_token_balance_usd(token_address=token.address, balance=token_balance)
        token_price, _ = self.net.get_token_price(token_address=token.address, token_decimals=token.decimals, sell=True)
        token_price_usd = self.net.get_token_price_usd(
            token_address=token.address, token_decimals=token.decimals, sell=True
        )
        effective_buy_price = ''
        if token.effective_buy_price:
            price_diff_percent = ((token_price / token.effective_buy_price) - Decimal(1)) * Decimal(100)
            effective_buy_price = (
                f'<b>At buy (after tax)</b>: {token.effective_buy_price:.3g} BNB/token '
                + f'(now {price_diff_percent:+.1f}%)\n'
            )
        orders_sorted = sorted(
            token.orders, key=lambda o: o.limit_price if o.limit_price else Decimal(1e12), reverse=True
        )  # if no limit price (market price) display first (big artificial value)
        orders = [str(order) for order in orders_sorted]
        message = (
            f'<b>{token.name}</b>: {format_token_amount(token_balance)}        '
            + f'<a href="https://poocoin.app/tokens/{token.address}">Chart</a>\n'
            + f'<b>Value</b>: <code>{token_balance_bnb:.3g}</code> BNB (${token_balance_usd:.2f})\n'
            + f'<b>Price</b>: <code>{token_price:.3g}</code> BNB/token (${token_price_usd:.3g})\n'
            + effective_buy_price
            + '<b>Orders</b>: (underlined = tracking trailing stop loss)\n'
            + '\n'.join(orders)
        )
        return message

    def get_summary_message(self) -> Tuple[str, List[List[InlineKeyboardButton]]]:
        balance_bnb = self.net.get_bnb_balance()
        price_bnb = self.net.get_bnb_price()
        msg = (
            f'<b>Wallet</b>: {balance_bnb:.4f} BNB (${balance_bnb * price_bnb:.2f})\n'
            + 'Which action do you want to perform next?'
        )
        return msg, self.get_global_keyboard()

    def get_global_keyboard(self) -> List[List[InlineKeyboardButton]]:
        buttons = [
            [
                InlineKeyboardButton('➖ Delete order', callback_data='removeorder'),
                InlineKeyboardButton('➕ Create order', callback_data='addorder'),
            ],
            [
                InlineKeyboardButton('❗️ Sell all!', callback_data='sellall'),
                InlineKeyboardButton('💰 Buy/Sell now', callback_data='buysell'),
            ],
            [
                InlineKeyboardButton('📇 Get address', callback_data='address'),
            ],
        ]
        return buttons

    def error_handler(self, update: Update, context: CallbackContext) -> None:
        logger.error('Exception while handling an update')
        logger.error(context.error)
        chat_message(update, context, text=f'⛔️ Exception while handling an update\n{context.error}', edit=False)

    def pause_status_update(self, pause: bool = True):
        for job in self.status_scheduler.get_jobs():
            # prevent running an update while we are changing the last message id
            if pause:
                job.pause()
            else:
                job.resume()
