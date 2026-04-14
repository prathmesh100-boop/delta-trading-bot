from delta_bot.orchestrator import parse_symbols_arg
from main import build_parser


def test_parse_symbols_arg_deduplicates_and_normalizes():
    symbols = parse_symbols_arg(" btcusd,ETHUSD, btcusd , SOLUSD ")

    assert symbols == ["BTCUSD", "ETHUSD", "SOLUSD"]


def test_parser_supports_trade_portfolio_command():
    parser = build_parser()

    args = parser.parse_args(["trade-portfolio", "--symbols", "BTCUSD,ETHUSD", "--capital", "100"])

    assert args.command == "trade-portfolio"
    assert args.symbols == "BTCUSD,ETHUSD"
    assert args.capital == 100.0
