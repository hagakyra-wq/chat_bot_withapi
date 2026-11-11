"""Заглушка для прежнего модуля дуэлей и счёта монет."""


def is_coin_leaderboard_request(*args, **kwargs):
    return False


def handle_balance_request(*args, **kwargs):
    return False


def handle_coin_leaderboard_request(*args, **kwargs):
    return False


def handle_duel_challenge(*args, **kwargs):
    return False
