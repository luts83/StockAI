import os
import httpx
from datetime import datetime, timedelta
from jose import jwt, JWTError
from dotenv import load_dotenv

load_dotenv()

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SECRET_KEY           = os.getenv("SECRET_KEY", "stockai-secret-key")
ALGORITHM            = "HS256"
TOKEN_EXPIRE_DAYS    = 30

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO  = "https://www.googleapis.com/oauth2/v2/userinfo"

def get_redirect_uri(request_base_url: str) -> str:
    """환경에 따라 redirect URI 자동 선택"""
    if "localhost" in request_base_url or "127.0.0.1" in request_base_url:
        return "http://127.0.0.1:8000/auth/callback"
    return "https://web-production-3b251.up.railway.app/auth/callback"

def get_google_auth_url(redirect_uri: str) -> str:
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{GOOGLE_AUTH_URL}?{query}"

async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
    """Google 인증 코드 → access token 교환"""
    async with httpx.AsyncClient() as client:
        res = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        })
        return res.json()

async def get_google_userinfo(access_token: str) -> dict:
    """Google access token → 사용자 정보"""
    async with httpx.AsyncClient() as client:
        res = await client.get(
            GOOGLE_USERINFO,
            headers={"Authorization": f"Bearer {access_token}"}
        )
        return res.json()

def create_jwt(user_id: str, email: str, name: str, picture: str) -> str:
    """JWT 토큰 생성"""
    payload = {
        "sub":     user_id,
        "email":   email,
        "name":    name,
        "picture": picture,
        "exp":     datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_jwt(token: str) -> dict | None:
    """JWT 토큰 검증 및 디코딩"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
