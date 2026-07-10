from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.core import auth as auth_core

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(req: LoginRequest):
    try:
        auth_core.verify_login(req.username, req.password)
    except auth_core.InvalidCredentialsError:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth_core.create_access_token(req.username)
    return {"access_token": token, "token_type": "bearer"}
