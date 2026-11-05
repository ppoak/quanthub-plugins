"""EastMoney API constants."""

LIST_URL = "https://emzuhelist.eastmoney.com/zhlinfo"
TRADE_URL = "https://moniapi.eastmoney.com/zuheV64/JS.aspx"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
SIM_QUERY_URL = "https://simqry2.eastmoney.com/qry_tzzh_v2"
SIM_OPER_URL = "https://simoper.eastmoney.com/oper_tzzh_v2"
MYFAVOR_URL = "https://myfavor.eastmoney.com/v4/webouter"
MYFAVOR_APPKEY = "e9166c7e9cdfad3aa3fd7d93b757e9b1"
PASSPORT_LOGIN_URL = (
    "https://passport2.eastmoney.com/pub/login?"
    "backurl=https%3A%2F%2Fgroup.eastmoney.com%2FMyCombin.html"
)

AUTH_ERROR_PATTERNS = ("token", "login", "auth", "unauthorized", "expired")
SECRET_PARAM_KEYS = {"utToken", "ctToken", "pi"}
ORDER_ID_KEYS = ("wth", "orderId", "order_id", "wtbh", "entrustId", "entrust_id", "id")
QUOTE_FIELDS = (
    "f11,f12,f13,f14,f15,f16,f17,f18,f19,f20,f31,f32,f33,f34,f35,f36,f37,f38,f39,f40,"
    "f120,f121,f122,f174,f175,f59,f163,f43,f57,f58,f169,f170,f46,f44,f51,"
    "f168,f47,f164,f116,f60,f45,f52,f50,f48,f167,f117,f71,f161,f49,f530,"
    "f135,f136,f137,f138,f139,f141,f142,f144,f145,f147,f148,f140,f143,f146,"
    "f149,f55,f62,f162,f92,f173,f104,f105,f84,f85,f183,f184,f185,f186,f187,"
    "f188,f189,f190,f191,f192,f107,f111,f86,f177,f78,f110,f262,f263,f264,f267,"
    "f268,f255,f256,f257,f258,f127,f199,f128,f198,f259,f260,f261,f171,f277,f278,"
    "f279,f288,f152,f250,f251,f252,f253,f254,f269,f270,f271,f272,f273,f274,f275,"
    "f276,f265,f266,f289,f290,f286,f285,f292,f293,f294,f295"
)
