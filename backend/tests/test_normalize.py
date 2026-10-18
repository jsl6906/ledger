from datetime import date
from decimal import Decimal

from ledger.services.normalize import fingerprint, normalize_merchant


def test_normalize_merchant_strips_store_numbers_and_noise():
    assert normalize_merchant("KROGER #412 SPRINGFIELD IL") == "kroger springfield"
    assert normalize_merchant("AMAZON.COM*2K4LM1 AMZN.COM/BILL WA") == normalize_merchant("AMAZON.COM*9ZZ AMZN.COM/BILL WA")
    assert normalize_merchant("POS DEBIT TRADER JOE'S #702") == "trader joe's"
    assert normalize_merchant("") is None
    a = "Verizon Wireless Payments~ Future Amount: 212.73 ~ Tran: Achdw"
    b = "Verizon Wireless Payments~ Future Amount: 1,216.37 ~ Tran: Achdw"
    assert normalize_merchant(a) == normalize_merchant(b) == "verizon wireless payments future amount tran achdw"


def test_normalize_merchant_drops_posting_dates_and_expands_bank_shorthand():
    deposit = "BKOFAMERICA MOBILE 04/24 3704819734 DEPOSIT *MOBILE DC"
    assert normalize_merchant(deposit) == "bank of america mobile deposit"
    assert normalize_merchant(deposit) == normalize_merchant("BKOFAMERICA MOBILE 11/03 3709912345 DEPOSIT *MOBILE DC")
    atm = "BKOFAMERICA ATM 04/24 #000001234 WITHDRWL SPRINGFIELD IL"
    assert normalize_merchant(atm) == "bank of america atm withdrawal springfield"
    assert normalize_merchant("CHECKCARD 0424 KROGER #412 SPRINGFIELD IL 24431064115000123456789") == "kroger springfield"
    assert normalize_merchant("NETFLIX.COM 10/18/26 CA") == "netflix com"
    assert normalize_merchant("BP #8812") == "bp"


def test_default_name_keeps_minor_words_lowercase():
    from ledger.services.merchants import default_name

    assert default_name("bank of america mobile deposit") == "Bank of America Mobile Deposit"
    assert default_name("the home depot") == "The Home Depot"
    assert default_name("trader joe's") == "Trader Joe's"
    assert default_name(None) is None


def test_fingerprint_stable_across_description_noise():
    a = fingerprint(1, date(2026, 9, 26), Decimal("-142.18"), "KROGER #412 SPRINGFIELD IL")
    b = fingerprint(1, date(2026, 9, 26), Decimal("-142.180"), "KROGER #413 SPRINGFIELD IL")
    c = fingerprint(2, date(2026, 9, 26), Decimal("-142.18"), "KROGER #412 SPRINGFIELD IL")
    assert a == b
    assert a != c
