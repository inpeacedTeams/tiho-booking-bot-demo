import hashlib
import re
from datetime import datetime

DUMMY_PHONES={"70000000000","71111111111","71234567890","79999999999","78005553535"}
URL_RE=re.compile(r"(?:https?://|www\.|\.ru\b|\.com\b|t\.me/)",re.I)


def normalize_phone(value:str)->str:
    digits="".join(character for character in value if character.isdigit())
    if len(digits)==10:
        digits="7"+digits
    elif len(digits)==11 and digits.startswith("8"):
        digits="7"+digits[1:]
    if len(digits)!=11 or not digits.startswith("7"):
        raise ValueError("Введите российский номер в формате +7 999 123-45-67")
    if digits in DUMMY_PHONES or len(set(digits[1:]))<4:
        raise ValueError("Этот номер выглядит ненастоящим")
    return "+"+digits


def validate_name(value:str)->str:
    name=" ".join(value.strip().split())
    if len(name)<2 or len(name)>80 or URL_RE.search(name):
        raise ValueError("Проверьте имя")
    if sum(character.isalpha() for character in name)<2:
        raise ValueError("Имя должно содержать хотя бы две буквы")
    return name


def hash_ip(value:str)->str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def client_ip(request)->str:
    forwarded=request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or ""
    return (forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown"))


def validate_origin(request):
    origin=request.headers.get("origin")
    if origin and request.url.hostname not in origin:
        raise ValueError("Запрос с чужого сайта отклонён")
